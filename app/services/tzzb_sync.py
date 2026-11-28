"""同花顺投资账本 → 本地交易台账同步。

数据契约（2026-11-26 对真实返回实测，不是照文档猜的）
------------------------------------------------------
``queryMoneyHistory`` 返回 ``{page, count, maxPage, list:[...]}``，逐条字段：

    stockCode   证券代码（'002558'，无市场后缀）
    stockName   证券名称
    entryDate   成交日期（'2026-10-30'，已是 ISO 格式）
    entryPrice  成交价（字符串 '19.2000'）
    entryCount  成交数量（字符串 '500'）
    entryMoney  成交金额（字符串 '9600.0000'）
    opName      业务名称：买入 / 卖出 / 派息 / 缴税
    feeTotal    总费用（字符串）——**以它为准**，不要再把分项加起来，
                commission 与 transferFee 之和与它并不相等（会重复计费）
    commission / transferFee   分项，仅留档，不参与计算

``queryStockPosition`` 返回 ``{totalAsset, totalLiability, holdings:[...]}``，
持仓逐条含 ``holdQuantity``（持股数）、``costPrice``（成本价）、
``holdDays``、``positionRatio`` 等。

为什么同步采用「按来源全量替换」而不是「按 id 增量合并」
--------------------------------------------------------
流水本身没有稳定的成交编号。同一天、同一只票、同样的价格和数量可能真的成交
多笔，只能靠「该指纹在本批内的第几次出现」区分。而这个序号依赖排序，两次同步
之间只要账本侧插入或修改了一笔，序号就会整体错位 → id 全变 → 增量合并会把
同一批成交重复入库。

因为每次都能把全量拉下来（当前 199 条），全量替换最干净：先剔除本地所有
``import_source == 'tzzb'`` 的记录，再整体写入。手工记账与持仓纠正的记录
（没有这个标记）原样保留。副作用是账本侧改了历史，同步后本地也跟着改——这正是想要的。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..core.secid import normalize_secid
from .trade_import import to_date, to_float, to_shares  # 统一解析口径，避免两套脏数据清洗逻辑

logger = logging.getLogger(__name__)

SOURCE_TAG = "tzzb"
_MAX_PAGES = 60          # 防御性上限：单账户一年约 2 页，60 页足够且能防服务端 maxPage 异常导致死循环
_PAGE_SIZE = 100

# 这些业务不是「买卖股票」：派息/缴税属于现金流事件，不入买卖台账。
# 但会统计出来给用户看，避免「流水条数对不上」的困惑。
_INCOME_OPS = ("派息", "红利", "股息", "利息")
_TAX_OPS = ("缴税", "代扣", "红利税")


def _side_of(op_name: str) -> Optional[str]:
    text = str(op_name or "").strip()
    if text in ("买入", "证券买入", "担保品买入", "融资买入"):
        return "buy"
    if text in ("卖出", "证券卖出", "担保品卖出", "融券卖出"):
        return "sell"
    if "买" in text and "卖" not in text:
        return "buy"
    if "卖" in text and "买" not in text:
        return "sell"
    return None


# ---------------- 取数 ----------------

def list_stock_accounts() -> List[Dict[str, Any]]:
    """拿股票账户列表。``scope`` 必须是 'stock'——不传会被工具的参数校验直接拒绝。"""
    from .tzzb_client import tzzb_client

    data = tzzb_client.call_tool("promptAccountSelection", {"scope": "stock"})
    accounts: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        for key, param in (("fund_key", "fundKey"), ("rzrq_fund_key", "rzrqFundKey"),
                           ("manual_id", "manualId")):
            for item in (data.get(key) or []):
                if isinstance(item, dict) and item.get("id"):
                    accounts.append({
                        "id": str(item["id"]),
                        "name": item.get("name") or "",
                        "param": item.get("param") or param,
                        "group": key,
                        "last_sync": item.get("lastSyncTime") or "",
                    })
    return accounts


def fetch_money_history(fund_key: str, start_date: str, end_date: str,
                        rzrq_fund_key: str = "", manual_id: str = "",
                        page_size: int = _PAGE_SIZE) -> Tuple[List[Dict[str, Any]], int]:
    """把指定区间内的资金流水全部翻页拉完。返回 (明细, 页数)。"""
    from .tzzb_client import tzzb_client

    start = str(start_date or "").replace("-", "")
    end = str(end_date or "").replace("-", "")
    rows: List[Dict[str, Any]] = []
    page = 1
    max_page = 1

    while page <= max_page and page <= _MAX_PAGES:
        payload = tzzb_client.call_tool("queryMoneyHistory", {
            "page": str(page),
            "count": str(page_size),
            "startDate": start,
            "endDate": end,
            "fundKey": fund_key or "",
            "rzrqFundKey": rzrq_fund_key or "",
            "manualId": manual_id or "",
        })
        if not isinstance(payload, dict):
            break
        batch = payload.get("list") or []
        rows.extend([r for r in batch if isinstance(r, dict)])
        try:
            max_page = int(payload.get("maxPage") or 1)
        except (TypeError, ValueError):
            max_page = 1
        if not batch:
            break
        page += 1

    return rows, page - 1


def fetch_positions(fund_key: str, rzrq_fund_key: str = "",
                    manual_id: str = "") -> Dict[str, Dict[str, Any]]:
    """真实持仓（账本口径）。这是持仓的唯一权威，流水只用来算成本和已实现盈亏。"""
    from .tzzb_client import tzzb_client

    payload = tzzb_client.call_tool("queryStockPosition", {
        "fundKey": fund_key or "",
        "rzrqFundKey": rzrq_fund_key or "",
        "manualId": manual_id or "",
    })
    out: Dict[str, Dict[str, Any]] = {}
    holdings = (payload or {}).get("holdings") if isinstance(payload, dict) else None
    for item in (holdings or []):
        if not isinstance(item, dict):
            continue
        sec = normalize_secid(str(item.get("stockCode") or ""))
        if not sec:
            continue
        out[sec] = {
            "name": item.get("stockName") or "",
            "shares": int(to_shares(item.get("holdQuantity")) or 0),
            "cost": round(float(to_float(item.get("costPrice")) or 0.0), 4),
            "price": to_float(item.get("currentPrice")),
            "hold_days": item.get("holdDays"),
            "position_ratio": item.get("positionRatio"),
            "market_value": to_float(item.get("marketValue")),
        }
    summary = {}
    if isinstance(payload, dict):
        summary = {
            "total_asset": to_float(payload.get("totalAsset")),
            "total_liability": to_float(payload.get("totalLiability")),
        }
    return {"positions": out, "summary": summary, "raw_count": len(holdings or [])}


# ---------------- 转换 ----------------

def to_records(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """流水明细 → trade_ledger 标准 record + 跳过明细。"""
    from .trade_import import _make_id

    skipped: List[Dict[str, Any]] = []
    income = 0
    tax = 0

    # 先按一个确定性键排序，保证「同指纹第几次出现」在两次同步之间稳定，
    # 否则同一个成交在两次同步里可能拿到不同的 id。
    def sort_key(row: Dict[str, Any]):
        return (
            str(row.get("entryDate") or ""),
            str(row.get("stockCode") or ""),
            str(row.get("opName") or ""),
            str(to_float(row.get("entryPrice")) or 0),
            str(to_shares(row.get("entryCount")) or 0),
        )

    ordered = sorted([r for r in rows if isinstance(r, dict)], key=sort_key)
    occurrence: Dict[str, int] = {}
    records: List[Dict[str, Any]] = []

    for row in ordered:
        op_name = str(row.get("opName") or "").strip()
        if any(k in op_name for k in _INCOME_OPS):
            income += 1
            continue
        if any(k in op_name for k in _TAX_OPS):
            tax += 1
            continue

        side = _side_of(op_name)
        if not side:
            skipped.append({"op": op_name, "code": row.get("stockCode"), "reason": "非买卖业务"})
            continue

        trade_date = to_date(row.get("entryDate"))
        if not trade_date:
            skipped.append({"op": op_name, "code": row.get("stockCode"),
                            "reason": f"日期无法识别：{row.get('entryDate')}"})
            continue

        sec = normalize_secid(str(row.get("stockCode") or ""))
        if not sec:
            skipped.append({"op": op_name, "code": row.get("stockCode"),
                            "reason": "代码无法归一（非沪深北 A 股？）"})
            continue

        shares = to_shares(row.get("entryCount"))
        price = to_float(row.get("entryPrice"))
        amount = to_float(row.get("entryMoney"))
        if not shares:
            skipped.append({"op": op_name, "code": row.get("stockCode"), "reason": "数量为空"})
            continue
        if price is None and amount:
            price = round(amount / shares, 4)
        if price is None:
            skipped.append({"op": op_name, "code": row.get("stockCode"), "reason": "价格为空"})
            continue
        if amount is None:
            amount = round(price * shares, 2)

        fee_total = to_float(row.get("feeTotal"))
        commission = to_float(row.get("commission")) or 0.0
        transfer = to_float(row.get("transferFee")) or 0.0
        if fee_total is None:
            # 账本没给总费用时退回估算，并标记，避免把 0 费用当成真的零成本
            from ..core import decision_rules
            est = decision_rules.calc_trade_fee(side, abs(amount))
            fee_detail = est
            fee_total = est["total"]
        else:
            fee_detail = {
                "total": round(fee_total, 2),
                "commission": round(commission, 2),
                "transfer": round(transfer, 2),
                "stamp": None,
                "estimated": False,
                "note": "总费用取自账本 feeTotal；分项之和与它不相等，不参与计算",
            }

        fingerprint = f"{trade_date}|{sec}|{side}|{shares}|{round(float(price), 4)}"
        seq = occurrence.get(fingerprint, 0)
        occurrence[fingerprint] = seq + 1

        records.append({
            "id": _make_id(trade_date, sec, side, shares, round(float(price), 4), seq),
            "created_at": f"{trade_date} 15:00:00",
            "trade_date": trade_date,
            "secID": sec,
            "name": str(row.get("stockName") or ""),
            "side": side,
            "shares": shares,
            "price": round(float(price), 4),
            "price_estimated": False,
            "amount": round(float(amount), 2),
            "fee": round(float(fee_total), 2),
            "fee_detail": fee_detail,
            "realized_pnl": None,
            "note": f"同花顺账本同步（{op_name}）",
            "import_source": SOURCE_TAG,
        })

    return {"records": records, "skipped": skipped, "income_count": income, "tax_count": tax}


# ---------------- 主流程 ----------------

def sync(preview: bool = True,
         start_date: Optional[str] = None,
         end_date: Optional[str] = None,
         fund_key: Optional[str] = None,
         apply_positions: bool = True,
         clear_missing: bool = False) -> Dict[str, Any]:
    """拉取 → 转换 → 重放 → (落库)。

    ``preview=True`` 只算不写。持仓以账本为权威；账本里没有的本地持仓默认**不清零**
    （``clear_missing=True`` 才清），避免因为账本漏同步一只票就把本地仓位抹掉。
    """
    from . import trade_ledger as ledger_mod
    from .trade_import import replay
    from .tzzb_client import tzzb_client

    status = tzzb_client.status()
    if not status.get("authorized"):
        return {"ok": False, "error": "尚未授权投资账本，请先运行 app/tools/cli/tzzb_auth.py",
                "status": status}

    accounts = list_stock_accounts()
    if not accounts:
        return {"ok": False, "error": "账本里没有股票账户。请先在手机 App 里完成账户同步。"}

    target = None
    if fund_key:
        target = next((a for a in accounts if a["id"] == str(fund_key)), None)
    if target is None:
        target = next((a for a in accounts if a["group"] == "fund_key"), accounts[0])
    if target["group"] != "fund_key":
        return {"ok": False,
                "error": f"当前选中的是「{target['name']}」（{target['group']}），"
                         f"本条链路只处理股票账户。",
                "accounts": accounts}

    today = datetime.date.today()
    end = (end_date or today.isoformat()).replace("/", "-")
    start = (start_date or datetime.date(today.year, 1, 1).isoformat()).replace("/", "-")

    try:
        rows, pages = fetch_money_history(target["id"], start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tzzb 拉取流水失败 error=%r", exc)
        return {"ok": False, "error": f"拉取资金流水失败：{exc}"}

    converted = to_records(rows)
    records = converted["records"]
    if not records:
        return {"ok": False,
                "error": f"区间 {start} ~ {end} 内没有买卖记录"
                         f"（原始 {len(rows)} 条，派息 {converted['income_count']}、"
                         f"缴税 {converted['tax_count']} 条已跳过）",
                "raw_count": len(rows)}

    try:
        pos_data = fetch_positions(target["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("tzzb 拉取持仓失败 error=%r", exc)
        pos_data = {"positions": {}, "summary": {}, "raw_count": 0,
                    "_error": str(exc)}

    replayed = replay(records)
    truth = pos_data["positions"]
    current = {str(it.get("secID") or ""): it
               for it in (getattr(ledger_mod.trade_ledger._svc(), "_favorite_cache", []) or [])}

    # 账本 vs 本地：逐只对比，让用户看到会改什么
    diff: List[Dict[str, Any]] = []
    for sec in sorted(set(truth) | set(current)):
        t = truth.get(sec) or {}
        c = current.get(sec) or {}
        t_shares = int(t.get("shares") or 0)
        c_shares = int(float(c.get("shares") or 0))
        if t_shares == 0 and sec not in truth:
            if not clear_missing:
                continue
        if t_shares == c_shares and abs(float(t.get("cost") or 0) - float(c.get("cost") or 0)) < 0.005:
            continue
        diff.append({
            "secID": sec,
            "name": t.get("name") or c.get("name") or sec,
            "in_ledger": sec in truth,
            "shares_now": c_shares,
            "shares_ledger": t_shares,
            "cost_now": round(float(c.get("cost") or 0), 4),
            "cost_ledger": round(float(t.get("cost") or 0), 4),
            "hold_days": t.get("hold_days"),
        })

    result: Dict[str, Any] = {
        "ok": True,
        "preview": preview,
        "account": {"id": target["id"], "name": target["name"], "last_sync": target["last_sync"]},
        "range": [start, end],
        "pages": pages,
        "raw_rows": len(rows),
        "parsed": len(records),
        "skipped": converted["skipped"][:20],
        "income_count": converted["income_count"],
        "tax_count": converted["tax_count"],
        "realized_pnl": replayed["realized_pnl"],
        "realized_count": replayed["realized_count"],
        "net_cash_flow": replayed["net_cash_flow"],
        "replayed_positions": replayed["positions"],
        "ledger_positions": truth,
        "position_diff": diff,
        "warnings": replayed["warnings"][:20],
        "orphan_sells": replayed["orphan_sells"][:20],
        "ledger_summary": pos_data.get("summary") or {},
        "records_preview": records[:10],
    }
    if pos_data.get("_error"):
        result["warnings"].append(f"持仓拉取失败，本次未校正持仓：{pos_data['_error']}")

    if preview:
        return result

    # ---- 落库 ----
    with ledger_mod._LEDGER_LOCK:
        existing = ledger_mod.trade_ledger._load()
        kept = [r for r in existing if str(r.get("import_source") or "") != SOURCE_TAG]
        removed = len(existing) - len(kept)
        merged = kept + records
        merged.sort(key=lambda r: (str(r.get("trade_date") or ""), str(r.get("created_at") or "")))
        ledger_mod.trade_ledger._save(merged)

    applied: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    if apply_positions and truth:
        svc = ledger_mod.trade_ledger._svc()
        for sec, pos in truth.items():
            shares = int(pos.get("shares") or 0)
            cost = round(float(pos.get("cost") or 0), 4)
            try:
                if sec in current:
                    svc.update_favorite_position(sec, shares=shares, cost=cost)
                else:
                    # 新增自选会顺带拉公司信息；ETF 没有公司档案，这一步可能失败。
                    # 不能让它把整批持仓同步打断，记下来单独看。
                    svc.add_favorite(sec, shares=shares, cost=cost, name=pos.get("name"))
                applied.append({"secID": sec, "name": pos.get("name"),
                                "shares": shares, "cost": cost})
            except Exception as exc:  # noqa: BLE001
                logger.warning("tzzb 写持仓失败 sec=%s error=%r", sec, exc)
                failed.append({"secID": sec, "name": pos.get("name"), "error": str(exc)[:200]})

        if clear_missing:
            cleared = []
            for sec, item in current.items():
                if sec not in truth and int(float(item.get("shares") or 0)) > 0:
                    try:
                        svc.update_favorite_position(sec, shares=0, cost=0.0)
                        cleared.append(sec)
                    except Exception as exc:  # noqa: BLE001
                        failed.append({"secID": sec, "error": str(exc)[:200]})
            if cleared:
                result["cleared"] = cleared

    result.update({
        "preview": False,
        "replaced": removed,
        "written": len(records),
        "total_after": len(merged),
        "positions_applied": applied,
        "positions_failed": failed,
    })
    return result
