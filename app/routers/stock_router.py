import datetime
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..schemas import (
    EquGetRequest,
    FavoriteRequest,
    SettingsRequest,
)
from ..core.auth import is_admin
from ..core.context import current_user_id, resolve_user_id
from ..core import decision_rules
from ..services import allocation_service, pool_classifier, proposal_service
from ..services.stock_service import stock_service
from ..services.trade_ledger import trade_ledger
from ..services.backtest_service import (
    DEFAULT_FEE_RATE,
    DEFAULT_SLIPPAGE_PCT,
    DEFAULT_STAMP_TAX_RATE,
    backtest_service,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger("app.stock_router")


@router.get("/stock/market_indices")
def get_market_indices():
    return stock_service.get_market_indices()


@router.post("/equ_get")
def equ_get(request: EquGetRequest):
    if not request.secID:
        return {"data": []}
    return {"data": stock_service.search_stocks(str(request.secID))}


@router.get("/stock/spot")
def get_spot(code: str):
    return stock_service.get_stock_spot_info(code)


@router.get("/stock/kline")
def get_kline(code: str, start_date: str = None, end_date: str = None, frequency: str = "d"):
    logger.info(
        "api_kline_request code=%s frequency=%s start_date=%s end_date=%s",
        code,
        frequency,
        start_date,
        end_date,
    )
    return stock_service.get_k_line_payload(code, start_date, end_date, frequency)


@router.get("/stock/strategy/list")
def list_strategies():
    return {"data": stock_service.list_strategies()}


@router.post("/stock/strategy/save")
def save_strategy(payload: Dict[str, Any]):
    from ..core.expr import check_expr

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not (payload.get("entry_expr") or "").strip():
        raise HTTPException(status_code=400, detail="entry_expr is required")
    # 表达式由任意登录用户写入且会被服务端求值：保存前必须过 AST 白名单校验，
    # 拒绝属性访问/下标/推导式/lambda 与超长超复杂表达式（详见 app/core/expr.py）。
    for key, label in (("entry_expr", "入场"), ("exit_expr", "出场")):
        valid, reason = check_expr(str(payload.get(key) or ""))
        if not valid:
            raise HTTPException(status_code=400, detail=f"{label}表达式不合法：{reason}")
    # 透传完整因子字段（描述/回测参数/最近回测指标），由服务层做缺省与类型收敛
    stock_service.save_factor(payload)
    return {"ok": True}


@router.post("/stock/strategy/delete")
def delete_strategy(payload: Dict[str, Any]):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    stock_service.delete_strategy(name)
    return {"ok": True}


@router.get("/stock/trend_strategies")
def get_trend_strategies():
    return {"data": stock_service.list_trade_strategy_configs()}


@router.post("/stock/trend_strategies/{horizon}")
def save_trend_strategy(horizon: str, payload: Dict[str, Any]):
    try:
        return {"data": stock_service.save_trade_strategy_config(horizon, payload)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/stock/realtime_minute")
def get_realtime_minute(code: str, period: str = "1d"):
    return stock_service.get_realtime_minute_data(code, period=period)


@router.get("/stock/trade_history")
def get_trade_history(code: str, count: int = 10):
    return {"data": stock_service.get_trade_history(code, count=count)}


@router.get("/stock/favorites")
def get_favorites(refresh: bool = False, quotes: bool = True):
    return {"data": stock_service.get_favorites_list(force_refresh=refresh, include_quotes=quotes)}


@router.get("/stock/favorites/spot")
def get_favorites_spot(refresh: bool = False):
    return {"data": stock_service.get_favorites_spot(force_refresh=refresh)}


@router.get("/stock/trades")
def list_trades(limit: int = 20, code: str = "", since: str = "", side: str = ""):
    """交易流水。前端用来展示「最近交易」与已实现盈亏，也是对话记账的留痕。"""
    items = trade_ledger.list_trades(limit=limit, code=code or None, since=since or None, side=side or None)
    return {"data": items, "summary": trade_ledger.summary(since=since or None)}


@router.post("/stock/trade")
def record_trade(payload: Dict[str, Any], request: Request = None):
    """手工记一笔成交（与对话记账同一套逻辑，保证两条入口口径一致）。

    买入会先过四池闸门：超单票上限 / 超持仓数 / 违规加仓 / 现金破下限会被拦下。
    确实要越过，带 ``force=true`` 重报：违规事实会写进 record.rule_override 落盘，
    并复用统一审计明细记下越过了哪几项，不只读在响应里。
    """
    result = trade_ledger.apply_trade(
        code=str(payload.get("code") or ""),
        side=str(payload.get("side") or ""),
        shares=payload.get("shares"),
        price=payload.get("price"),
        fee=payload.get("fee"),
        trade_date=payload.get("date") or payload.get("trade_date"),
        note=str(payload.get("note") or ""),
        force=bool(payload.get("force")),
    )
    if not result.get("ok"):
        if result.get("gate") == "pool_rules":
            # 用 409 而不是 400：这是"规则不允许"，不是"请求格式错"，
            # 且要带出 issues 让前端能逐条展示、再决定是否强制记账。
            return JSONResponse(status_code=409, content={
                "ok": False,
                "gate": "pool_rules",
                "issues": result.get("issues") or [],
                "error": {"code": "pool_rule_violation",
                          "message": result.get("error") or "这笔买入违反四池规则"},
                "hint": "确认要越过规则，请在同一个请求里加 force=true",
            })
        raise HTTPException(status_code=400, detail=result.get("error") or "记账失败")
    override = result.get("rule_override")
    if isinstance(override, dict) and override and request is not None:
        labels: List[str] = []
        for item in (override.get("violations") or []):
            label = str(item.get("label") or "")
            if label and label not in labels:
                labels.append(label)
        if not labels:
            labels = [allocation_service.violation_label(c) for c in (override.get("codes") or [])]
        sec_id = str((result.get("record") or {}).get("secID") or "")
        _set_audit_detail(request, {"gate": {"pool_rules": {
            "from": "按四池规则执行",
            "to": f"{sec_id} 越过：{'/'.join(labels) or '未分类规则'}",
        }}})
    snapshot = stock_service.get_account_snapshot(use_cache=False)
    return {"ok": True, "data": result.get("record"), "rule_override": override,
            "warnings": result.get("warnings") or [], "account": snapshot}


@router.get("/stock/discipline")
def get_discipline(days: int = 30):
    """纪律执行复盘：遵守率 / 违规次数 / 按规则与按池分布 / 最近明细。

    纯读聚合，数据源只有交易台账里已落盘的 ``rule_override``；
    不新增表、调度或推送。口径随响应一并返回，便于复核。
    """
    return trade_ledger.discipline_summary(days=days)


@router.get("/stock/is_favorite")
def is_fav(secID: str):
    return {"is_favorite": stock_service.is_favorite(secID)}


@router.post("/stock/favorites/add")
def add_fav(request: FavoriteRequest):
    stock_service.add_favorite(
        request.secID,
        request.shares or 0,
        request.cost or 0.0,
        request.horizon or "long",
        request.buy_date,
        request.name,
        group=request.group or "",
        note=request.note or "",
        pool=request.pool or "",
    )
    return {"status": "success"}


@router.post("/stock/favorites/update_position")
def update_fav_pos(request: FavoriteRequest):
    stock_service.update_favorite_position(
        request.secID,
        request.shares,
        request.cost,
        request.horizon,
        request.buy_date,
        group=request.group,
        note=request.note,
        pool=request.pool,
    )
    return {"status": "success"}


@router.post("/stock/favorites/remove")
def remove_fav(request: FavoriteRequest):
    stock_service.remove_favorite(request.secID)
    return {"status": "success"}


@router.get("/stock/monitor")
def monitor_stocks(refresh: bool = False, notify_mode: str = "auto", ignore_trading_hours: bool = False):
    started = time.time()
    logger.info("api_monitor_request refresh=%s notify_mode=%s ignore_trading_hours=%s", refresh, notify_mode, ignore_trading_hours)
    data = stock_service.monitor_favorites(force_refresh=refresh, respect_trading_hours=not ignore_trading_hours)
    if any(item.get("monitor_busy") for item in data):
        notify_result = {"ok": False, "sent": False, "reason": "monitor_busy"}
    else:
        notify_result = stock_service.notify_monitor_results(data, mode=notify_mode)
    logger.info(
        "api_monitor_response refresh=%s notify_mode=%s rows=%s notify=%s elapsed_ms=%.2f",
        refresh,
        notify_mode,
        len(data),
        notify_result.get("reason") or notify_result.get("sent"),
        (time.time() - started) * 1000.0,
    )
    return {"data": data, "notify": notify_result}


@router.post("/stock/backtest")
def run_backtest(payload: Dict[str, Any]):
    """基于日线的表达式策略回测。

    请求体字段（均可选，除 code）：
      code / start_date / end_date / capital /
      entry_expr / exit_expr / strategy_name /
      benchmark(hs300|zz500|sh|sz|none) /
      fee_rate / stamp_tax_rate / slippage_pct / position_pct
    """
    code = str(payload.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    started = time.time()

    def _num(key: str, default: float) -> float:
        value = payload.get(key)
        try:
            return float(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    result = backtest_service.run(
        code=code,
        start_date=str(payload.get("start_date") or "").strip() or None,
        end_date=str(payload.get("end_date") or "").strip() or None,
        capital=_num("capital", 100000.0),
        entry_expr=str(payload.get("entry_expr") or ""),
        exit_expr=str(payload.get("exit_expr") or ""),
        benchmark=str(payload.get("benchmark") or "hs300"),
        fee_rate=_num("fee_rate", DEFAULT_FEE_RATE),
        stamp_tax_rate=_num("stamp_tax_rate", DEFAULT_STAMP_TAX_RATE),
        slippage_pct=_num("slippage_pct", DEFAULT_SLIPPAGE_PCT),
        position_pct=_num("position_pct", 1.0),
        strategy_name=str(payload.get("strategy_name") or ""),
    )
    logger.info(
        "api_backtest_response code=%s ok=%s return=%s elapsed_ms=%.2f",
        code,
        result.get("ok"),
        result.get("return_pct"),
        (time.time() - started) * 1000.0,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or result.get("error") or "backtest failed")
    # 「回测完成后」的 doctor trigger：结果不理想才投递，调度器下一个 tick 消费并推送诊断。
    # 入队失败不影响回测响应。
    try:
        from ..services.agent_schedule_service import enqueue_backtest_review

        enqueue_backtest_review(result)
    except Exception as exc:  # noqa: BLE001
        logger.debug("backtest_doctor_enqueue_failed error=%r", exc)
    return result


@router.get("/stock/chip_distribution")
def get_chip_dist(code: str):
    return stock_service.get_chip_distribution(code)


@router.get("/stock/money_flow")
def get_money_flow(code: str):
    return stock_service.get_money_flow(code)


@router.get("/stock/settings")
def get_settings():
    return stock_service.get_user_settings()


@router.get("/stock/account")
def get_account():
    """账户资金快照：市值 / 可用现金 / 总资产 / 真实仓位 / 弹药状态。"""
    return stock_service.get_account_snapshot()


@router.get("/stock/sentiment_policy")
def get_sentiment_policy():
    """市场情绪 → 仓位与现金纪律：目标仓位区间 vs 实际仓位。

    用户理念：情绪低落时留有现金、开始建仓；情绪疯狂时要卖出。
    """
    from ..services.sentiment_position_service import (
        build_sentiment_position_policy,
        format_sentiment_policy,
    )

    try:
        sentiment = stock_service.get_market_sentiment() or {}
        market = stock_service.get_market_regime() or {}
        account = stock_service.get_account_snapshot() or {}
    except Exception as exc:  # noqa: BLE001
        logger.exception("情绪策略获取失败")
        raise HTTPException(status_code=500, detail=f"情绪策略获取失败：{exc}")

    policy = build_sentiment_position_policy(
        sentiment=sentiment,
        market=market,
        account={"position_ratio": account.get("position_ratio")},
    )

    # 周期位置：当前温度在历史中的分位 + 阶段持续天数
    cycle: Dict[str, Any] = {}
    try:
        from ..services.sentiment_history_service import sentiment_history_service

        history = sentiment_history_service.get_history_view(current=sentiment)
        cycle = history.get("cycle") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentiment_cycle_failed error=%r", exc)

    return {
        "ok": bool(policy.get("ok")),
        "policy": policy,
        "text": format_sentiment_policy(policy),
        "cycle": cycle,
        "sentiment": {
            "phase": sentiment.get("phase"),
            "phase_label": sentiment.get("phase_label"),
            "temperature": sentiment.get("temperature"),
            "as_of": sentiment.get("as_of"),
            "detail": sentiment.get("detail"),
        },
        "account": {
            "position_ratio": account.get("position_ratio"),
            "cash_ratio": account.get("cash_ratio"),
            "available_cash": account.get("available_cash"),
            "total_assets": account.get("total_assets"),
        },
    }


@router.get("/stock/sentiment_history")
def get_sentiment_history():
    """情绪历史曲线与周期位置：温度分位 / 阶段持续天数 / 转折点。"""
    from ..services.sentiment_history_service import sentiment_history_service

    try:
        current = stock_service.get_market_sentiment() or {}
        view = sentiment_history_service.get_history_view(current=current)
        if not view.get("ok"):
            return {"ok": False, "error": view.get("error"), "points": [], "cycle": {}}
        return {
            "ok": True,
            "points": view.get("points") or [],
            "count": view.get("count"),
            "cycle": view.get("cycle") or {},
            "phase_days": view.get("phase_days") or {},
            "text": sentiment_history_service.format_history_view(view),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("情绪历史获取失败")
        raise HTTPException(status_code=500, detail=f"情绪历史获取失败：{exc}")


@router.get("/stock/industry_valuation")
def get_industry_valuation(code: str):
    """行业估值中枢：这只票在**自己行业**里贵不贵（不跨行业比较 PE/PB）。"""
    try:
        context = stock_service.get_valuation_context(code) or {}
    except Exception as exc:  # noqa: BLE001
        logger.exception("行业估值获取失败")
        raise HTTPException(status_code=500, detail=f"行业估值获取失败：{exc}")

    from ..services.industry_valuation_service import assess_industry_relative

    industry = context.get("industry_valuation") or {}
    valuation = context.get("valuation") or {}
    relative = assess_industry_relative(
        valuation.get("pe"), valuation.get("pb"), industry,
    )
    return {
        "ok": True,
        "code": code,
        "industry_code": context.get("industry_code"),
        "industry_name": context.get("industry_name"),
        "industry_stats": {
            k: v for k, v in industry.items()
            if k not in ("peer_pe_list", "peer_pb_list")
        },
        "relative": relative,
    }


@router.get("/portfolio/performance")
def portfolio_performance(days: int = 180):
    """账户绩效：净值曲线、最大回撤、胜率、盈亏比、沪深300 对比。

    全部由本地流水 + 日线计算，不依赖模型；模型不可用时也应有数。
    """
    from ..services.performance_service import performance_service

    try:
        return performance_service.analyze(days=days)
    except Exception as exc:  # noqa: BLE001
        logger.exception("绩效分析失败")
        raise HTTPException(status_code=500, detail=f"绩效分析失败：{exc}")


@router.post("/stock/settings")
def save_settings(request: SettingsRequest):
    payload = {"total_capital": request.total_capital}
    # 可用资金：只有用户真的填了才写，并打上时间戳。
    # 这样「0 元」= 满仓，而不是「没填→退化成估算」——满仓的人现金本来就是 0。
    if request.available_cash is not None:
        payload["available_cash"] = max(0.0, float(request.available_cash))
        payload["cash_updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 投资风格：决定 Agent 卖出目标价的算法口径（个股 horizon 优先于该全局值）
    if request.investment_style is not None:
        style = str(request.investment_style or "").strip().lower()
        if style in ("long", "mid_long", "swing"):
            payload["investment_style"] = style
    if request.monitor_enabled is not None:
        payload["monitor_enabled"] = bool(request.monitor_enabled)
    if request.monitor_interval_sec is not None:
        payload["monitor_interval_sec"] = int(request.monitor_interval_sec)
    stock_service.save_user_settings(payload)
    return {"status": "success"}


@router.get("/stock/monitor_settings")
def get_monitor_settings():
    return stock_service.get_monitor_settings()


@router.post("/stock/monitor_settings")
def save_monitor_settings(payload: Dict[str, Any]):
    update: Dict[str, Any] = {}
    if payload.get("monitor_enabled") is not None:
        update["monitor_enabled"] = bool(payload.get("monitor_enabled"))
    if payload.get("monitor_interval_sec") is not None:
        update["monitor_interval_sec"] = int(payload.get("monitor_interval_sec"))
    if payload.get("monitor_kline_frequency") is not None:
        update["monitor_kline_frequency"] = str(payload.get("monitor_kline_frequency") or "d").strip().lower()
    if payload.get("dingtalk_enabled") is not None:
        update["dingtalk_enabled"] = bool(payload.get("dingtalk_enabled"))
    if payload.get("dingtalk_webhook_url") is not None:
        update["dingtalk_webhook_url"] = str(payload.get("dingtalk_webhook_url") or "").strip()
    if payload.get("dingtalk_secret") is not None:
        update["dingtalk_secret"] = str(payload.get("dingtalk_secret") or "").strip()
    if payload.get("xueqiu_cookie") is not None:
        update["xueqiu_cookie"] = str(payload.get("xueqiu_cookie") or "").strip()
    if payload.get("xueqiu_user_agent") is not None:
        update["xueqiu_user_agent"] = str(payload.get("xueqiu_user_agent") or "").strip()
    # 雪球 Cookie 是实例级共享凭证：一个用户改坏，所有人都取不到行情。
    if ("xueqiu_cookie" in update or "xueqiu_user_agent" in update) and not is_admin(current_user_id()):
        raise HTTPException(status_code=403, detail="雪球 Cookie 为实例级共享凭证，仅管理员可修改")
    if update:
        stock_service.save_user_settings(update)
    return stock_service.get_monitor_settings()


@router.get("/stock/monitor_strategy")
def get_monitor_strategy():
    settings = stock_service.get_user_settings()
    return {"name": settings.get("monitor_strategy") or ""}


@router.post("/stock/monitor_strategy")
def set_monitor_strategy(payload: Dict[str, Any]):
    stock_service.save_user_settings({"monitor_strategy": str(payload.get("name") or "").strip()})
    return {"ok": True}


@router.get("/stock/alert_rules")
def get_alert_rules():
    return stock_service.get_alert_rules()


@router.post("/stock/alert_rules")
def save_alert_rules(payload: Dict[str, Any]):
    return stock_service.save_alert_rules(payload)


@router.get("/stock/prefer_source")
def get_prefer_source():
    return {"prefer_source": stock_service.get_user_settings().get("prefer_source", "xueqiu")}


@router.post("/stock/prefer_source")
def set_prefer_source(payload: Dict[str, Any]):
    src = str(payload.get("prefer_source") or "").strip().lower()
    if src not in ("amazingdata", "xueqiu"):
        raise HTTPException(status_code=400, detail="prefer_source must be 'amazingdata' or 'xueqiu'")
    # 主源是实例级能力（星耀要专用账号、且共享行情缓存），不是个人偏好：
    # 普通用户改它会波及所有人的取数路径，因此收敛为管理员独占。
    if not is_admin(current_user_id()):
        raise HTTPException(status_code=403, detail="行情主源为实例级配置，仅管理员可修改")
    # 同时更新内存中的开关，否则要重启才生效
    stock_service.set_prefer_source(src)
    return {"prefer_source": src}


@router.post("/stock/notify/dingtalk/test")
def test_dingtalk_notify(payload: Optional[Dict[str, Any]] = None):
    body = payload or {}
    include_snapshot = bool(body.get("include_monitor_snapshot", False))
    monitor_data: List[Dict[str, Any]] = []
    if include_snapshot:
        monitor_data = stock_service.monitor_favorites(force_refresh=False)
    result = stock_service.notify_monitor_results(monitor_data, force_send=True)
    result["include_monitor_snapshot"] = include_snapshot
    return result


# ---------------------------------------------------------------- 运行自检 / 新增能力

_KNOWN_SOURCES = ("amazingdata", "tencent", "xueqiu", "eastmoney", "akshare", "baostock", "sina")


@router.get("/stock/source_health")
def source_health():
    """数据源健康快照：冷却中的源会标出剩余秒数，供运维自检面板使用。"""
    try:
        snapshot = stock_service.source_health_snapshot() or {}
    except Exception as exc:  # noqa: BLE001
        snapshot = {}
        logger.debug("source_health_snapshot_failed error=%r", exc)
    sources = []
    for name in _KNOWN_SOURCES:
        state = snapshot.get(name) or {}
        cooling = bool(state.get("cooldown"))
        sources.append({
            "name": name,
            "healthy": not cooling,
            "cooldown": cooling,
            "remaining_sec": state.get("remaining_sec", 0),
        })
    return {
        "sources": sources,
        "cooling_count": sum(1 for s in sources if s["cooldown"]),
        "prefer_source": str(stock_service.get_user_settings().get("prefer_source") or ""),
        "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/stock/decision_rules")
def get_decision_rules():
    """决策规则只读摘要（不含任何凭证），供前端核对口径。"""
    from ..core import decision_rules

    summary = decision_rules.rules_summary()
    summary["path"] = decision_rules.rules_path().name
    summary["fees"] = decision_rules.fee_rules()
    summary["risk"] = decision_rules.risk_rules()
    summary["monitor"] = decision_rules.monitor_rules()
    summary["expr"] = decision_rules.expr_rules()
    return summary


@router.post("/stock/factor_validation")
def factor_validation(payload: Dict[str, Any]):
    """IC / IR、分层回测、样本外 walk-forward：验证因子是不是只拟合了历史。"""
    from ..services.factor_validation_service import validate_saved_factor

    code = str(payload.get("code") or "").strip()
    factor_name = str(payload.get("factor_name") or "").strip()
    if not code or not factor_name:
        raise HTTPException(status_code=400, detail="code 与 factor_name 均不能为空")
    try:
        horizon_days = int(payload.get("horizon_days") or 20)
    except (TypeError, ValueError):
        horizon_days = 20
    return validate_saved_factor(code, factor_name, horizon_days)


@router.post("/stock/risk_guard")
def risk_guard(payload: Dict[str, Any]):
    """组合级风控：相对位置、波动率目标、回撤护栏、集中度。"""
    from ..services.risk_guard_service import assess

    code = str(payload.get("code") or "").strip()
    kline: List[Dict[str, Any]] = []
    if code:
        today = datetime.datetime.now()
        start = (today - datetime.timedelta(days=500)).strftime("%Y-%m-%d")
        try:
            kline = stock_service.get_k_line_data(code, start, today.strftime("%Y-%m-%d"),
                                                  "d", caller="risk_guard") or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("risk_guard_kline_failed code=%s error=%r", code, exc)
    equity = payload.get("equity") if isinstance(payload.get("equity"), list) else []
    positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
    return {"ok": True, "code": code, **assess(kline, equity, positions)}


# ---------------------------------------------------------------------------
# 四池配置 / 组合分配 / 建议单（两道确认）
# ---------------------------------------------------------------------------

def _allocation_holdings() -> tuple[List[Dict[str, Any]], float]:
    """把自选持仓 + 用户总资金组装成 allocation_service 的输入。

    形状由 allocation_service.holdings_from_items 统一，告警与复盘共用同一份。
    """
    return allocation_service.holdings_from_items(
        stock_service.get_favorites_list(include_quotes=True) or [],
        stock_service.get_user_settings() or {},
    )


@router.get("/stock/pools")
def get_pools():
    """四池规则 + 现金规则 + 全局风控阈值（只读）。"""
    return {
        "pools": allocation_service.pool_rules_summary(),
        "cash": decision_rules.cash_rules(),
        "global_risk": decision_rules.global_risk(),
    }


_AUDIT_DETAIL_MAX = 2000


def _diff_top(before: Dict[str, Any], after: Dict[str, Any],
              fields: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """比较一层字典，只返回真正变化的字段 {field: {from, to}}。"""
    keys = list(fields) if fields else sorted(set(before) | set(after))
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in keys if before.get(key) != after.get(key)
    }


def _set_audit_detail(request: Request, changes: Dict[str, Any]) -> None:
    """把规则变更明细挂到 request.state，由统一审计中间件写进 audit_log.detail。

    只记「哪一档从多少改成多少」，不记整份规则、更不记请求体——
    后者可能夹带口令等敏感字段。读取 body 也交给 FastAPI，中间件不碰流。
    """
    if not changes:
        return
    request.state.audit_detail = json.dumps(
        {"changes": changes}, ensure_ascii=False, separators=(",", ":"),
    )[:_AUDIT_DETAIL_MAX]


@router.put("/stock/pools")
def update_pools(payload: Dict[str, Any], request: Request):
    """调整四池目标占比与现金目标（写 decision_rules.json，按 mtime 热生效）。

    只开放 target_pct / min_pct / max_pct / max_single_pct / max_positions
    与现金三个字段；止盈/止损/加仓/时间止损仍属策略内容，不在此开放。
    """
    pools_patch = payload.get("pools") if isinstance(payload, dict) else None
    cash_patch = payload.get("cash") if isinstance(payload, dict) else None
    if not pools_patch and not cash_patch:
        raise HTTPException(status_code=400, detail="至少提供一个可改项：pools 或 cash")

    before_pools = decision_rules.pools()
    before_cash = decision_rules.cash_rules()
    try:
        result = allocation_service.save_pool_config(pools_patch, cash_patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    after_pools = decision_rules.pools()
    after_cash = decision_rules.cash_rules()
    changes: Dict[str, Any] = {}
    pool_changes: Dict[str, Any] = {}
    for key in before_pools:
        diff = _diff_top(before_pools.get(key) or {}, after_pools.get(key) or {},
                         allocation_service.POOL_EDITABLE_FIELDS)
        if diff:
            pool_changes[key] = diff
    if pool_changes:
        changes["pools"] = pool_changes
    cash_diff = _diff_top(before_cash, after_cash, allocation_service.CASH_EDITABLE_FIELDS)
    if cash_diff:
        changes["cash"] = cash_diff
    _set_audit_detail(request, changes)

    # 保存后立刻用新规则重算，前端一次请求就拿到最新占比
    holdings, total = _allocation_holdings()
    allocation = allocation_service.compute_allocation(holdings, total)
    if allocation.get("ok"):
        allocation["rebalance"] = allocation_service.rebalance_plan(allocation)
    allocation["plain"] = allocation_service.plain_summary(allocation, allocation.get("rebalance"))
    logger.info("四池配置已更新 pools=%s cash=%s",
                list((pools_patch or {}).keys()), bool(cash_patch))
    return {"ok": True, **result, "allocation": allocation}


@router.put("/stock/risk_switches")
def update_risk_switches(payload: Dict[str, Any], request: Request):
    """切换组合级风控的两个开关：波动率目标 / 回撤护栏。

    默认值仍为关闭（不擅自改变用户既有的仓位行为），但界面上可以随时打开；
    写入走 decision_rules.update，按 mtime 热生效，无需重启。
    """
    from ..services.risk_guard_service import MONITOR_SWITCHES, save_monitor_switches

    before = decision_rules.monitor_rules()
    try:
        after = save_monitor_switches(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    diff = _diff_top(before, after, MONITOR_SWITCHES)
    _set_audit_detail(request, {"monitor": diff} if diff else {})
    return {"ok": True, "monitor": after, "changed": diff}


@router.get("/stock/allocation")
def get_allocation():
    """当前四池实际占比 / 偏离 / 越界项 / 再平衡建议。"""
    holdings, total = _allocation_holdings()
    result = allocation_service.compute_allocation(holdings, total)
    if result.get("ok"):
        result["rebalance"] = allocation_service.rebalance_plan(result)
    # 说人话的结论与动作：前端只渲染，不在前端重写一套判断
    result["plain"] = allocation_service.plain_summary(result, result.get("rebalance"))
    return result


@router.post("/stock/allocation/classify")
def classify_pool(payload: Dict[str, Any]):
    """给持仓打四池标签；未分类持仓不参与目标池，分配会失真。"""
    sec_id = str(payload.get("secID") or "").strip()
    if not sec_id:
        raise HTTPException(status_code=400, detail="secID 不能为空")
    pool = allocation_service.normalize_pool(payload.get("pool"))
    if not pool:
        raise HTTPException(status_code=400, detail="pool 必须是 dividend/catalyst/long_track/value")
    stock_service.update_favorite_position(sec_id, pool=pool)
    return {"status": "success", "secID": sec_id, "pool": pool,
            "label": allocation_service.pool_label(pool)}


@router.post("/stock/allocation/classify/suggest")
def suggest_pool_classification(payload: Optional[Dict[str, Any]] = None):
    """四池分类建议（只读，不落库）。人工确认后再调 /classify 落库。

    参数：
      only_unclassified: 只对未分类持仓给建议（默认 true）
      fundamentals: 是否拉财务快照（默认 true；关掉更快但信号更少）
    """
    payload = payload or {}
    only_unclassified = bool(payload.get("only_unclassified", True))
    with_fundamentals = bool(payload.get("fundamentals", True))
    budget = decision_rules.pool_classify_rules()
    deadline = float(budget["deadline_sec"])
    workers = max(1, int(budget["max_workers"]))

    items = stock_service.get_favorites_list(include_quotes=True) or []
    targets: List[Dict[str, Any]] = []
    for item in items:
        sec_id = str(item.get("secID") or "").strip()
        if not sec_id:
            continue
        current_pool = allocation_service.normalize_pool(item.get("pool"))
        if only_unclassified and current_pool:
            continue
        targets.append({"item": item, "secID": sec_id, "current_pool": current_pool})

    # 估值/财报/行业都要打外部接口，串行会把请求挂成分钟级（2026-11-15 实测 306 秒）。
    # 这里并发抓 + 硬截止：超时的票按「数据缺失」降级，分类器本来就支持缺数据不硬猜。
    data: Dict[str, Dict[str, Any]] = {}
    if targets:
        futures: Dict[Any, tuple] = {}
        # 不能用 with：ThreadPoolExecutor.__exit__ 会 wait=True，把慢任务等完，
        # 硬截止就白设了。这里显式 shutdown(wait=False)，让没回来的任务自己跑完丢弃。
        pool = ThreadPoolExecutor(max_workers=min(workers, max(1, len(targets) * 3)))
        try:
            for tgt in targets:
                sec = tgt["secID"]
                slot = data.setdefault(sec, {"valuation": {}, "fundamentals": {}, "industry": ""})
                tgt["industry"] = str(tgt["item"].get("industry") or "").strip()

                def _grab(slot, key, code, fn, *args):
                    # slot/code 显式传参，避免闭包晚绑定把不同票的数据写串
                    try:
                        value = fn(code, *args)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("pool_suggest_%s_failed secid=%s error=%r", key, code, exc)
                        return
                    if isinstance(value, dict):
                        slot[key] = value

                futures[pool.submit(_grab, slot, "valuation", sec, stock_service._evaluate_valuation)] = ("valuation", sec)
                if with_fundamentals:
                    futures[pool.submit(_grab, slot, "fundamentals", sec, stock_service.get_fundamental_snapshot)] = ("fundamentals", sec)
                if not tgt["industry"]:
                    futures[pool.submit(_grab, slot, "profile", sec, stock_service.get_company_info)] = ("profile", sec)

            done, pending = _futures_wait(list(futures.keys()), timeout=deadline)
            for fut in pending:
                kind, sec = futures[fut]
                data[sec].setdefault("timed_out", set()).add(kind)
                fut.cancel()
        finally:
            pool.shutdown(wait=False)

    results: List[Dict[str, Any]] = []
    for tgt in targets:
        sec = tgt["secID"]
        slot = data.get(sec) or {}
        industry = str(tgt.get("industry") or "").strip()
        if not industry:
            profile = slot.get("profile") or {}
            industry = str(
                profile.get("industry") or profile.get("Industry")
                or profile.get("行业") or ""
            ).strip()
        timed_out = sorted(slot.get("timed_out") or ())
        suggestion = pool_classifier.classify_one(
            code=sec,
            name=str(tgt["item"].get("name") or ""),
            industry=industry,
            valuation=slot.get("valuation") or {},
            fundamentals=slot.get("fundamentals") or {},
        )
        suggestion["current_pool"] = tgt["current_pool"]
        suggestion["current_label"] = (
            allocation_service.pool_label(tgt["current_pool"]) if tgt["current_pool"] else "未分类"
        )
        # 数据没在预算内回来时，必须让调用方知道置信度被数据拖累了，而不是假装算过
        suggestion["partial"] = bool(timed_out)
        suggestion["missing_data"] = timed_out
        results.append(suggestion)

    return {
        "ok": True,
        "items": results,
        "count": len(results),
        "deadline_sec": deadline,
        "partial_count": sum(1 for r in results if r.get("partial")),
        "note": "建议仅供参考：采纳前请逐只确认；催化波段与长赛道的区别是持有意图，规则无法替你判断。",
    }


@router.get("/stock/proposals")
def list_proposals(status: str = "", limit: int = 20):
    return {"data": proposal_service.list_proposals(resolve_user_id(), status or None, limit)}


@router.post("/stock/proposals")
def create_proposal(payload: Dict[str, Any]):
    """创建建议单（proposed）；采纳与执行必须走独立端点，两次确认。"""
    kind = str(payload.get("kind") or "rebalance")
    title = str(payload.get("title") or "四池建议")
    body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    analysis = str(payload.get("analysis") or "")
    try:
        return proposal_service.create(resolve_user_id(), kind, title, body, analysis)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stock/proposals/{proposal_id}/accept")
def accept_proposal(proposal_id: int, payload: Optional[Dict[str, Any]] = None):
    """第一道确认：用户决定是否跟随 Agent 的建议。"""
    try:
        return proposal_service.accept(resolve_user_id(), proposal_id, str((payload or {}).get("note") or ""))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stock/proposals/{proposal_id}/ignore")
def ignore_proposal(proposal_id: int, payload: Optional[Dict[str, Any]] = None):
    """忽略建议：只留痕，不执行。"""
    try:
        return proposal_service.ignore(resolve_user_id(), proposal_id, str((payload or {}).get("note") or ""))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _apply_proposal(pl: Dict[str, Any]) -> Dict[str, Any]:
    """第二道确认后真正落地：只有带 trade 指令才写交易台账。"""
    trade = pl.get("trade") if isinstance(pl, dict) else None
    if not isinstance(trade, dict):
        return {"applied": False, "reason": "该建议只调整比例/计划，无交易指令，未写台账"}
    code = str(trade.get("code") or trade.get("secID") or "").strip()
    side = str(trade.get("side") or "").strip()
    shares = trade.get("shares")
    price = trade.get("price")
    if not code or not side or shares in (None, ""):
        raise ValueError("交易指令缺少 code/side/shares")
    result = trade_ledger.apply_trade(code, side, shares, price,
                                      note=str(trade.get("note") or "建议单执行"))
    if isinstance(result, dict) and not result.get("ok", True):
        raise ValueError(f"台账写入失败：{result.get('error') or result}")
    return result


@router.post("/stock/proposals/{proposal_id}/execute")
def execute_proposal(proposal_id: int, payload: Dict[str, Any]):
    """第二道确认：必须 confirm=true，且已通过第一道采纳，才允许写台账。"""
    confirm = bool(payload.get("confirm"))
    note = str(payload.get("note") or "")
    try:
        return proposal_service.execute(resolve_user_id(), proposal_id, confirm=confirm,
                                        note=note, executor=_apply_proposal)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
