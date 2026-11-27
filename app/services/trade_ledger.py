"""交易记账：把用户口语报的成交，落成真实持仓 + 真实现金。

为什么必须有这一层，而不是让用户去改"持股数/成本"两个数字
----------------------------------------------------
1. 只改数字看不出发生过什么。用户问"这个月我做了哪些操作、赚了多少已经落袋的"，
   没有流水就只能靠回忆。
2. 卖出时现金只回收**成交额**（不是持仓成本）。所以「本金 − 持仓成本」这个估算公式
   一卖出就会系统性偏大，满仓的人会被算成还有弹药，然后收到不该有的买入提醒。
   逐笔记账是唯一能让「可用现金」保持真实的方式。
3. 加仓要重算摊薄成本、减仓要保持原成本不变，这两个语义只有记账能区分：
   直接改"成本"字段的人为操作，经常把减仓当成"成本变了"。

口径约定
--------
- 买入：新成本 = (原持股数×原成本 + 成交额 + 费用) / 新持股数（费用计入成本）
- 卖出：成本不变，只减股数；清仓后成本归零。已实现盈亏 = (卖价 − 成本)×股数 − 费用
- 费用：佣金万分之2.5（最低 5 元，双向）、印花税 0.05%（仅卖出）、过户费 0.001%（双向）
- 现金：买入 −(成交额+费用)，卖出 +(成交额−费用)
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 费率唯一事实源在 config/decision_rules.json，经 app.core.decision_rules 读取。
# 这里保留模块级别名只为兼容既有引用；计算一律走 calc_trade_fee()，
# 回测撮合调的是同一个函数，两个口径不会再漂移。
from ..core import decision_rules  # noqa: E402

_fee_rules = decision_rules.fee_rules()
COMMISSION_RATE = float(_fee_rules.get("commission_rate", 0.00025))
COMMISSION_MIN = float(_fee_rules.get("commission_min", 5.0))
STAMP_RATE = float(_fee_rules.get("stamp_rate", 0.0005))
TRANSFER_RATE = float(_fee_rules.get("transfer_rate", 0.00001))
LOT_SIZE = int(_fee_rules.get("lot_size", 100))

_LEDGER_LOCK = threading.Lock()
# 流水保留上限。原为 2000，做历史导入时不够用：三年活跃交易很容易破 2000 笔，
# _save() 会静默截断最早的记录（=丢掉最老的历史仓位来龙去脉）。
# 提到 20000：单条 payload 约 600B，满量约 12MB，_load() 全量 json 解析在几十毫秒级，
# 可接受；真正的瓶颈是 sqlite 单表写入，而写入是整批替换的，不是每笔一次。
_MAX_RECORDS = 20000

_SIDE_ALIASES = {
    "buy": "buy", "b": "buy", "买入": "buy", "买": "buy", "加仓": "buy", "建仓": "buy",
    "补仓": "buy", "增持": "buy", "buy_in": "buy",
    "sell": "sell", "s": "sell", "卖出": "sell", "卖": "sell", "减仓": "sell",
    "清仓": "sell", "减持": "sell", "止盈": "sell", "止损": "sell",
}


def normalize_side(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return _SIDE_ALIASES.get(text)


def _f(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _round2(value: Optional[float]) -> Optional[float]:
    return round(float(value), 2) if value is not None else None


def calc_fee(side: str, amount: float, fee: Optional[float] = None) -> Dict[str, float]:
    """估算一笔交易的总费用。用户给了实际手续费就直接用，不再叠加。

    唯一实现在 `app.core.decision_rules.calc_trade_fee`，回测撮合调用同一函数。
    """
    return decision_rules.calc_trade_fee(side, amount, fee)


class TradeLedger:
    """交易流水 + 持仓/现金同步。config 目录由 stock_service 提供，测试可注入。"""

    def __init__(self, config_dir: Optional[str] = None, svc: Any = None):
        self._config_dir = config_dir
        # 测试注入专用：不注入时用全局 stock_service 单例。
        # 工具层测试必须同时注入 config_dir + svc，否则 record_trade 会写进生产库。
        self._svc_override = svc

    # ---------- 基础设施 ----------
    def _svc(self):
        if self._svc_override is not None:
            return self._svc_override
        from .stock_service import stock_service
        return stock_service

    def _path(self):
        from pathlib import Path
        if self._config_dir:
            return Path(self._config_dir) / "trade_log.json"
        return Path(self._svc()._config_dir) / "trade_log.json"

    def _use_file(self) -> bool:
        """测试可注入 config_dir 走文件；生产走数据库（多用户按 current_user 隔离）。"""
        return bool(self._config_dir)

    def _load(self) -> List[Dict[str, Any]]:
        if not self._use_file():
            from ..core import context as user_context
            from ..db import store as db_store
            uid = user_context.resolve_user_id()
            # 仓储按时间倒序返回（给接口用），记账内部按时间正序追加，这里翻回来
            return list(reversed(db_store.list_trades(uid, limit=_MAX_RECORDS)))
        import json
        path = self._path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("trade_log_read_failed error=%r", exc)
            return []
        return data if isinstance(data, list) else []

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        if not self._use_file():
            from ..core import context as user_context
            from ..db import store as db_store
            uid = user_context.resolve_user_id()
            db_store.replace_trades(uid, rows[-_MAX_RECORDS:])
            return
        import json
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = rows[-_MAX_RECORDS:]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ---------- 标的解析 ----------
    def resolve_stock(self, ref: str) -> Dict[str, str]:
        """支持 6 位代码 / 带市场后缀 / 股票名称（自选优先，其次全市场列表）。"""
        svc = self._svc()
        text = str(ref or "").strip()
        if not text:
            raise ValueError("缺少股票代码或名称")

        normalized = None
        try:
            normalized = svc._normalize_stock_code(text)
        except Exception:
            normalized = None
        if normalized:
            sec_id = f"{normalized['digits']}.{normalized['market'].upper()}"
            return {"secID": sec_id, "digits": normalized["digits"], "name": svc._get_cached_stock_name(normalized["digits"]) or sec_id}

        # 名称：先在本机股票列表缓存里找（code → name 的反查）
        candidates: List[Dict[str, str]] = []
        cache = getattr(svc, "_stock_list_cache", None) or []
        if not cache:
            try:
                svc._refresh_stock_list_cache()
                cache = getattr(svc, "_stock_list_cache", None) or []
            except Exception:
                cache = []
        exact, fuzzy = [], []
        for item in cache:
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or "").strip()
            if not name or not code:
                continue
            if name == text:
                exact.append((code, name))
            elif text in name or name in text:
                fuzzy.append((code, name))
        for code, name in exact + fuzzy:
            try:
                norm = svc._normalize_stock_code(code)
            except Exception:
                continue
            candidates.append({"secID": f"{norm['digits']}.{norm['market'].upper()}",
                               "digits": norm["digits"], "name": name})
        if candidates:
            return candidates[0]
        raise ValueError(f"识别不出「{text}」是哪只股票，请直接给 6 位代码")

    # ---------- 持仓读写 ----------
    def _find_item(self, sec_id: str) -> Optional[Dict[str, Any]]:
        svc = self._svc()
        for item in getattr(svc, "_favorite_cache", []) or []:
            if str(item.get("secID") or "").upper() == sec_id.upper():
                return item
        return None

    def _current_price(self, sec_id: str) -> Optional[float]:
        svc = self._svc()
        try:
            spot = svc.get_stock_spot_info(sec_id) or {}
            return _f(spot.get("price"))
        except Exception:
            return None

    # ---------- 主流程 ----------
    def _pool_gate(self, sec_id: str, pool: Any, held_shares: int,
                   new_shares: int, price: float) -> Dict[str, Any]:
        """买入前的四池闸门：模拟成交后的四池状态，按池规则判断能不能放行。

        分两层判断，因为它们的前提不同：

        - **与池无关**（现金下限）：新建仓的票还没归池，池规则无从判断，
          但"买完现金不够"任何池都成立，必须照样拦。
        - **与池相关**（单票上限 / 持仓只数 / 加仓规则）：只有已归池的票才判，
          未归池的给出 warning 而不是硬拦——否则用户新建仓会被无理由挡住。

        占比一律复用 ``allocation_service.compute_allocation`` 计算，不在这里
        重写一遍权重算法，否则"页面显示的占比"和"闸门判断的占比"迟早漂移。
        卖出不走这里：仓位只会变小，不存在越过上限的问题。
        """
        from . import allocation_service as alloc

        svc = self._svc()
        try:
            items = svc.get_favorites_list(include_quotes=True) or []
            total = float((svc.get_user_settings() or {}).get("total_capital") or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("池闸门读取持仓失败，放行 error=%r", exc)
            return {"blocked": False, "warning": f"池规则校验已跳过（读取持仓失败：{exc}）"}
        if total <= 0:
            return {"blocked": False, "warning": "总资金未设置，无法按占比校验池规则"}

        holdings: List[Dict[str, Any]] = []
        found = False
        for it in items:
            sec = str(it.get("secID") or "")
            shares = int(_f(it.get("shares")) or 0)
            if sec == sec_id:
                shares = new_shares
                found = True
            holdings.append({
                "secID": sec,
                "name": it.get("name"),
                "shares": shares,
                "cost": it.get("cost"),
                "pool": it.get("pool"),
                "price": price if sec == sec_id else it.get("price"),
                "industry": it.get("industry") or "",
            })
        if not found:
            # 买一只还不在自选里的票：快照里必须把它算进去，
            # 否则成交后的占比/现金不会变化，闸门形同虚设。
            holdings.append({
                "secID": sec_id, "name": "", "shares": new_shares, "cost": price,
                "pool": None, "price": price, "industry": "",
            })

        after = alloc.compute_allocation(holdings, total)
        if not after.get("ok"):
            return {"blocked": False, "warning": f"池规则校验已跳过：{after.get('error')}"}

        cash_after_pct = (after.get("cash") or {}).get("actual_pct")
        violations: List[Dict[str, Any]] = []
        warning = ""

        key = alloc.normalize_pool(pool)
        row = next((p for p in after.get("pools") or [] if p.get("pool") == key), None) if key else None
        if key and row:
            post_weight = max(0, new_shares) * price / total * 100.0
            verdict = alloc.validate_pool_trade(
                key,
                post_weight_pct=post_weight,
                post_positions=int(row.get("count") or 0),
                is_add=held_shares > 0,
                cash_after_pct=cash_after_pct,
            )
            violations.extend(verdict.get("violations") or [])
        else:
            # 未归池：只能判与池无关的那条
            cash_item = alloc.cash_floor_violation(cash_after_pct)
            if cash_item:
                violations.append(cash_item)
            warning = "该票未归入四池，已跳过池规则校验；建议先在四池页分类"

        if violations:
            return {
                "blocked": True,
                "pool": key,
                "violations": violations,
                "codes": [v.get("code") for v in violations],
                "issues": [v.get("message") for v in violations],
            }
        return {"blocked": False, "warning": warning}

    def apply_trade(self, code: str, side: str, shares: Any, price: Any = None,
                    fee: Any = None, trade_date: Optional[str] = None,
                    note: str = "", dry_run: bool = False,
                    force: bool = False) -> Dict[str, Any]:
        """记一笔成交并同步持仓与现金。返回结构化结果（ok=False 表示未写入）。

        ``force=True`` 用于明确越过四池规则（闸门默认拦截违规买入）。
        """
        side_norm = normalize_side(side)
        if not side_norm:
            return {"ok": False, "error": "side 只能是 buy(买入) 或 sell(卖出)"}

        try:
            shares_int = int(round(float(shares)))
        except Exception:
            return {"ok": False, "error": f"股数无法识别：{shares}"}
        if shares_int <= 0:
            return {"ok": False, "error": "股数必须大于 0"}
        if side_norm == "buy" and shares_int % LOT_SIZE != 0:
            return {"ok": False, "error": f"买入必须是 {LOT_SIZE} 股整数倍（报的是 {shares_int} 股）"}

        try:
            stock = self.resolve_stock(code)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        sec_id = stock["secID"]

        item = self._find_item(sec_id)
        held_shares = int(_f((item or {}).get("shares")) or 0)
        held_cost = float(_f((item or {}).get("cost")) or 0.0)

        price_value = _f(price)
        price_estimated = False
        if price_value is None or price_value <= 0:
            price_value = self._current_price(sec_id)
            price_estimated = True
        if not price_value or price_value <= 0:
            return {"ok": False, "error": f"{stock['name']} 取不到成交价，请补一句成交价（例如 27.35）"}

        if side_norm == "sell":
            if held_shares <= 0:
                return {"ok": False, "error": f"{stock['name']}（{sec_id}）当前记录持股为 0，没有可卖的仓位。"
                                              f"如果是记错了持仓，请先告诉我实际持股数与成本。"}
            if shares_int > held_shares:
                return {"ok": False, "error": f"卖出 {shares_int} 股超过记录的持股 {held_shares} 股。"
                                              f"如果实际持股更多，请先纠正确认股数。"}

        amount = round(shares_int * price_value, 2)
        fee_info = calc_fee(side_norm, amount, _f(fee))
        total_fee = fee_info["total"]

        # 成交后的持仓
        if side_norm == "buy":
            new_shares = held_shares + shares_int
            base_cost = held_cost * held_shares
            new_cost = (base_cost + amount + total_fee) / new_shares if new_shares else 0.0
            realized = None
            cash_delta = -(amount + total_fee)
        else:
            new_shares = held_shares - shares_int
            new_cost = held_cost if new_shares > 0 else 0.0
            realized = round((price_value - held_cost) * shares_int - total_fee, 2) if held_cost else None
            cash_delta = amount - total_fee

        # 四池闸门：买入前按池规则校验；卖出永远放行（仓位只会变小）。
        # 三道入口（手工记账 / 对话记账 / 建议单执行）都汇到这里，只装一道闸就全覆盖。
        gate_block: Dict[str, Any] = {"blocked": False}
        if side_norm == "buy":
            gate_block = self._pool_gate(
                sec_id=sec_id,
                pool=(item or {}).get("pool"),
                held_shares=held_shares,
                new_shares=new_shares,
                price=price_value,
            )
            if gate_block.get("blocked") and not force:
                return {
                    "ok": False,
                    "gate": "pool_rules",
                    "codes": gate_block.get("codes") or [],
                    "issues": gate_block.get("issues") or [],
                    "error": "这笔买入违反四池规则：" + "；".join(gate_block.get("issues") or [])
                             + "。确认要越过规则，请加 force=true 重报。",
                }

        gate_warning = str(gate_block.get("warning") or "")
        # force 越过时必须把「违反了什么」写进 record，否则刷新页面就查无此事：
        # trades.payload 存的是整条 record 的 JSON，加字段不需要建表或迁移。
        override: Optional[Dict[str, Any]] = None
        if gate_block.get("blocked") and force:
            override = {
                "pool": gate_block.get("pool") or "",
                "codes": gate_block.get("codes") or [],
                "violations": gate_block.get("violations") or [],
                "issues": gate_block.get("issues") or [],
            }
            gate_warning = "已按你的要求越过四池规则：" + "；".join(override["issues"])

        # 现金
        account = self._svc()._cash_detail()
        cash_before = _f(account.get("value"))
        cash_source = str(account.get("source") or "unknown")
        cash_after = round(cash_before + cash_delta, 2) if cash_before is not None else None
        cash_warning = ""
        if cash_after is not None and cash_after < 0:
            cash_after = 0.0
            cash_warning = "按可用资金扣减后不足 0，已按下限 0 处理；请核对可用现金是否填准"
        if cash_before is None:
            cash_warning = "账户可用资金未知，本次未改现金；建议在设置页填一次真实可用余额"

        # 防重复：同一笔（标的/方向/股数/价格）短时间内重复上报，直接拦掉
        now = datetime.datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        day = str(trade_date or now.strftime("%Y-%m-%d")).strip()[:10]
        rows = self._load()
        for rec in reversed(rows[-20:]):
            if (rec.get("secID") == sec_id and rec.get("side") == side_norm
                    and int(rec.get("shares") or 0) == shares_int
                    and abs(float(rec.get("price") or 0) - price_value) < 1e-6):
                try:
                    prev = datetime.datetime.strptime(str(rec.get("created_at") or ""), "%Y-%m-%d %H:%M:%S")
                    if (now - prev).total_seconds() < 300:
                        return {"ok": False, "skeptical": True,
                                "error": f"这笔（{stock['name']} {shares_int} 股 @{price_value}）5 分钟前刚记过，"
                                         f"疑似重复上报。如果确实成交了两次，请告诉我可以记两笔。"}
                except Exception:
                    pass

        record = {
            "id": f"T{now.strftime('%Y%m%d%H%M%S')}{shares_int % 97:02d}",
            "created_at": stamp,
            "trade_date": day,
            "secID": sec_id,
            "name": stock["name"],
            "side": side_norm,
            "shares": shares_int,
            "price": round(float(price_value), 3),
            "price_estimated": price_estimated,
            "amount": amount,
            "fee": total_fee,
            "fee_detail": fee_info,
            "realized_pnl": realized,
            "shares_before": held_shares,
            "cost_before": round(held_cost, 4),
            "shares_after": new_shares,
            "cost_after": round(new_cost, 4),
            "cash_before": cash_before,
            "cash_after": cash_after,
            "cash_source": cash_source,
            "note": str(note or "").strip(),
        }
        if override:
            record["rule_override"] = override

        if dry_run:
            record["dry_run"] = True
            return {"ok": True, "dry_run": True, "record": record,
                    "rule_override": override,
                    "warnings": [w for w in [cash_warning, gate_warning] if w]}

        with _LEDGER_LOCK:
            svc = self._svc()
            if item is None:
                if new_shares > 0:
                    svc.add_favorite(sec_id, shares=new_shares, cost=round(new_cost, 4))
            else:
                svc.update_favorite_position(sec_id, shares=new_shares, cost=round(new_cost, 4))
            if cash_after is not None and cash_before is not None:
                svc.save_user_settings({"available_cash": cash_after, "cash_updated_at": stamp})
            rows.append(record)
            self._save(rows)

        warnings = [w for w in [cash_warning, gate_warning] if w]
        if price_estimated:
            warnings.append(f"没给成交价，已按现价 {price_value} 记；实际成交价不同的话告诉我改")
        if side_norm == "sell" and new_shares == 0:
            warnings.append("已清仓，成本归零；该标的仍留在自选里方便观察")

        return {"ok": True, "record": record, "rule_override": override, "warnings": warnings}

    def _pnl_by_adherence(self, rows: List[Dict[str, Any]], cutoff: str) -> Dict[str, Any]:
        """守规 vs 越权：窗口内已落袋的已实现盈亏对比（纯读，不做任何写操作）。

        口径（随响应返回，可复核）：
        - 买入按其 ``rule_override`` 归入 越权/守规 两组，费用计入成本（与台账同口径）；
        - 卖出按当时仓位里两组股数的占比，把该笔已实现盈亏分摊到两组。
          这是加权平均口径的自然分解：两组之和恒等于台账该笔的 ``realized_pnl``，
          用户可以拿流水自己核对；
        - ``sync_position``（持仓纠正/送转）会把成本重置成无法归因的混合值，
          重置后的剩余持仓归入 ``unattributed``，单列展示、不参与对比——不猜归属；
        - 卖出按 ``trade_date`` 计入窗口（落袋时刻）；买入不限窗口（违规标记跟随成本走）；
        - 不含分红送转的现金收益（系统没有分红数据源，硬算会错）。
        """
        groups = {"violated": {}, "compliant": {}, "unattributed": {}}
        for key in groups:
            groups[key] = {"shares": 0.0, "cost": 0.0, "pnl": 0.0, "slices": 0, "wins": 0}

        # 桶名 → 展示组名：台账内部叫 clean（守规），对外叫 compliant。
        _bucket_to_group = {"violated": "violated", "clean": "compliant", "unknown": "unattributed"}

        state: Dict[str, Dict[str, List[float]]] = {}
        # 顺序必须用台账落库顺序（_load 两条路径都保证时间正序）：
        # 同一秒的多笔成交按 created_at 排序会互相颠倒，id 字典序也不可靠。
        for row in rows:
            if not isinstance(row, dict):
                continue
            side = str(row.get("side") or "")
            sec_id = str(row.get("secID") or "")
            if not sec_id:
                continue
            buckets = state.setdefault(sec_id, {"violated": [0.0, 0.0], "clean": [0.0, 0.0],
                                                "unknown": [0.0, 0.0]})
            shares = int(_f(row.get("shares")) or 0)

            if side == "buy":
                bucket = "violated" if isinstance(row.get("rule_override"), dict) and row.get("rule_override") else "clean"
                buckets[bucket][0] += shares
                buckets[bucket][1] += float(_f(row.get("amount")) or 0.0) + float(_f(row.get("fee")) or 0.0)
                continue

            if side == "adjust":
                # 持仓纠正/送转：成本被重置成混合值，剩余持仓全部转入"无法归因"。
                after_shares = float(_f(row.get("shares_after")) or 0.0)
                after_cost = float(_f(row.get("cost_after")) or 0.0)
                buckets["unknown"] = [after_shares, after_shares * after_cost]
                buckets["violated"] = [0.0, 0.0]
                buckets["clean"] = [0.0, 0.0]
                continue

            if side != "sell" or shares <= 0:
                continue

            total = sum(b[0] for b in buckets.values())
            price = _f(row.get("price"))
            fee = float(_f(row.get("fee")) or 0.0)
            day = str(row.get("trade_date") or row.get("created_at") or "")[:10]
            in_window = bool(day) and day >= cutoff
            if not total or price is None:
                continue

            for key, (b_shares, b_cost) in buckets.items():
                if b_shares <= 0:
                    continue
                frac = b_shares / total
                slice_shares = shares * frac
                if slice_shares <= 0:
                    continue
                cost_per_share = b_cost / b_shares
                slice_cost = cost_per_share * slice_shares
                slice_pnl = (price - cost_per_share) * slice_shares - fee * frac
                if in_window and slice_cost > 0:
                    stat = groups[_bucket_to_group[key]]
                    stat["shares"] += slice_shares
                    stat["cost"] += slice_cost
                    stat["pnl"] += slice_pnl
                    stat["slices"] += 1
                    stat["wins"] += 1 if slice_pnl > 0 else 0

        def _slot(name: str) -> Dict[str, Any]:
            stat = groups[name]
            if stat["slices"] <= 0:
                return None
            return {
                "closed_shares": int(round(stat["shares"])),
                "slice_count": stat["slices"],
                "realized_pnl": _round2(stat["pnl"]),
                "pnl_pct": _round2(stat["pnl"] / stat["cost"] * 100.0) if stat["cost"] > 0 else None,
                "win_rate_pct": _round2(stat["wins"] * 100.0 / stat["slices"]),
            }

        violated = _slot("violated")
        compliant = _slot("compliant")
        return {
            "scope": "按窗口内卖出的已实现盈亏对比；买入按越权标记归组，卖出按当时仓位两组股数占比分摊"
                     "（两组之和=台账 realized_pnl，可复核）；持仓纠正后的剩余持仓归入 unattributed；不含分红送转",
            "sufficient": bool(violated and compliant),
            "violated": violated,
            "compliant": compliant,
            "unattributed": _slot("unattributed"),
        }

    def discipline_summary(self, days: int = 30, recent: int = 8) -> Dict[str, Any]:
        """纪律执行复盘：把落盘的 rule_override 聚合成可复核的指标（纯读）。

        口径（接口会原样返回，避免遵守率变成无法核对的数字）：
        - 分母 = 时间窗内**台账中已记录的买入笔数**；
        - 被闸门 409 拦下、未形成成交的请求不计入（它们没有落库）；
        - 窗口内无买入记录时 ``compliance_pct`` 返回 None，不编造 100%。
        """
        from . import allocation_service as alloc

        # 注意不能用 `days or 30`：0 是合法入参，会被吞成默认值。
        try:
            days = int(days if days is not None else 30)
            recent = int(recent if recent is not None else 8)
        except (TypeError, ValueError):
            days, recent = 30, 8
        days = max(1, min(days, 365))
        recent = max(0, min(recent, 50))
        cutoff = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
        rows = self._load()

        buy_count = 0
        overrides: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("side") or "") != "buy":
                continue
            day = str(row.get("trade_date") or row.get("created_at") or "")[:10]
            if not day or day < cutoff:
                continue
            buy_count += 1
            hit = row.get("rule_override")
            if isinstance(hit, dict) and hit:
                overrides.append((row, hit))

        by_code: Dict[str, Dict[str, Any]] = {}
        by_pool: Dict[str, int] = {}
        details: List[Dict[str, Any]] = []
        for row, hit in overrides:
            codes = [str(c) for c in (hit.get("codes") or []) if c]
            if not codes:
                # 兼容旧数据：只有文案没有 code 的历史记录，不猜分类
                codes = ["unclassified"]
            for code in codes:
                slot = by_code.setdefault(code, {"code": code, "label": alloc.violation_label(code),
                                                 "count": 0, "last_message": ""})
                slot["count"] += 1
                for item in (hit.get("violations") or []):
                    if str(item.get("code")) == code:
                        slot["last_message"] = str(item.get("message") or "")
                        break
            pool = str(hit.get("pool") or "").strip()
            pool_label = alloc.pool_label(pool) if pool else "未分类"
            by_pool[pool_label] = by_pool.get(pool_label, 0) + 1
            details.append({
                "id": row.get("id"), "trade_date": row.get("trade_date"),
                "created_at": row.get("created_at"), "secID": row.get("secID"),
                "name": row.get("name"), "pool": pool_label,
                "codes": codes,
                "labels": [alloc.violation_label(c) for c in codes],
                "issues": hit.get("issues") or [],
            })

        details.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        override_count = len(overrides)
        return {
            "ok": True,
            "days": days,
            "since": cutoff,
            "scope": "分母为窗口内台账已记录的买入笔数；被 409 拦下未成交的请求不计入",
            "buy_count": buy_count,
            "override_count": override_count,
            "compliance_pct": round((buy_count - override_count) * 100.0 / buy_count, 2) if buy_count else None,
            "by_code": sorted(by_code.values(), key=lambda x: (-x["count"], x["code"])),
            "by_pool": sorted([{"pool": k, "count": v} for k, v in by_pool.items()],
                              key=lambda x: (-x["count"], x["pool"])),
            "recent": details[:recent],
            "pnl_by_adherence": self._pnl_by_adherence(rows, cutoff),
        }

    def sync_position(self, code: str, shares: Any, cost: Any = None, note: str = "") -> Dict[str, Any]:
        """直接纠正持股数/成本（送转股、分红再投、或之前记错了）。不产生现金变动。"""
        try:
            shares_int = int(round(float(shares)))
        except Exception:
            return {"ok": False, "error": f"股数无法识别：{shares}"}
        if shares_int < 0:
            return {"ok": False, "error": "股数不能为负"}
        try:
            stock = self.resolve_stock(code)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        cost_value = _f(cost)
        svc = self._svc()
        item = self._find_item(stock["secID"])
        before = {"shares": int(_f((item or {}).get("shares")) or 0),
                  "cost": float(_f((item or {}).get("cost")) or 0.0)}
        if item is None and shares_int == 0:
            return {"ok": False, "error": f"{stock['name']} 不在自选里，无需纠正"}
        if item is None:
            svc.add_favorite(stock["secID"], shares=shares_int, cost=round(cost_value or 0.0, 4))
        else:
            svc.update_favorite_position(stock["secID"], shares=shares_int,
                                         cost=round(cost_value, 4) if cost_value is not None else None)
        after = {"shares": shares_int,
                 "cost": (round(cost_value, 4) if cost_value is not None else before["cost"])}

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": f"S{now.replace('-', '').replace(':', '').replace(' ', '')}",
            "created_at": now,
            "trade_date": now[:10],
            "secID": stock["secID"],
            "name": stock["name"],
            "side": "adjust",
            "shares": shares_int,
            "price": None,
            "amount": None,
            "fee": 0.0,
            "realized_pnl": None,
            "shares_before": before["shares"],
            "cost_before": before["cost"],
            "shares_after": after["shares"],
            "cost_after": after["cost"],
            "note": str(note or "持仓纠正").strip(),
        }
        with _LEDGER_LOCK:
            rows = self._load()
            rows.append(record)
            self._save(rows)
        return {"ok": True, "record": record, "before": before, "after": after}

    # ---------- 查询 ----------
    def list_trades(self, limit: int = 20, code: Optional[str] = None,
                    since: Optional[str] = None, side: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self._load()
        sec_filter = None
        if code:
            try:
                sec_filter = self.resolve_stock(code)["secID"]
            except ValueError:
                sec_filter = str(code or "").upper()
        side_norm = normalize_side(side) if side else None
        out = []
        for rec in rows:
            if sec_filter and str(rec.get("secID") or "").upper() != sec_filter:
                continue
            if side_norm and rec.get("side") != side_norm:
                continue
            if since and str(rec.get("trade_date") or "") < str(since):
                continue
            out.append(rec)
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        try:
            limit_int = max(1, int(limit))
        except Exception:
            limit_int = 20
        return out[:limit_int]

    def summary(self, since: Optional[str] = None) -> Dict[str, Any]:
        """已实现盈亏汇总（只统计 sell 记录里有成本的）。"""
        rows = self.list_trades(limit=_MAX_RECORDS, since=since)
        sells = [r for r in rows if r.get("side") == "sell" and r.get("realized_pnl") is not None]
        realized = round(sum(float(r.get("realized_pnl") or 0) for r in sells), 2)
        fees = round(sum(float(r.get("fee") or 0) for r in rows), 2)
        buys = [r for r in rows if r.get("side") == "buy"]
        return {
            "total_records": len(rows),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "realized_pnl": realized if sells else None,
            "total_fee": fees,
            "since": since or (min((str(r.get("trade_date") or "") for r in rows), default="") or None),
        }


trade_ledger = TradeLedger()
