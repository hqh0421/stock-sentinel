"""历史交易流水导入：解析 → 归一化 → 内存重放 → 预览 → 一次性落库。

为什么必须另写一条路径，而不是逐条调 ``trade_ledger.apply_trade()``
------------------------------------------------------------------
逐笔调 apply_trade 会踩四个既有设计，历史批量场景下全部是硬伤：

1. **四池闸门按「当时持仓」判断**（``_pool_gate``）。历史重放时现金和仓位序号
   全都不对，几千笔里必然有一批被当成违规买入拦下，导入直接残缺。
2. **未给成交价时会逐笔调行情**（``_current_price``）。几千笔等于几千次请求，
   而主源是 ``mp.Pool(processes=1)`` 单 worker 串行队列，会把整个行情源熔断 15 分钟。
3. **5 分钟防重复检测**（扫最近 20 条）会把同一文件里的连续成交误判成重复上报。
4. **写入是 O(n²)**：``_save`` 底层是 ``replace_trades`` 整表替换，逐条追加
   等于把整表重写 n 遍。

所以这里只复用「纯计算」部分（``decision_rules.calc_trade_fee`` 与摊薄成本公式），
把闸门、行情查询、防重复全部绕开，改成一次内存重放 + 一次写库。

幂等怎么保证
------------
每条记录的 id 由「成交日期 + secID + 方向 + 股数 + 价格 + 该指纹在文件内的第几次出现」
做 sha1 摘要生成。同一笔交易无论导几次、从哪个文件导进来的，id 都一样，
落库时 ``INSERT OR REPLACE`` 自然去重；而真实的同日同价分笔成交因为 occurrence
不同，会得到不同的 id，不会被误合并。
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..core import decision_rules
from ..core.secid import normalize_secid

logger = logging.getLogger(__name__)

# ---------- 列名别名 ----------
# 券商交割单与同花顺资金流水的表头差异极大，用别名 + 精确优先 + 包含兜底。
# 长别名必须排在前面：否则「金额」会把「成交金额」抢走。
_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "date": ("发生日期", "成交日期", "交易日期", "委托日期", "清算日期", "成交时间", "日期"),
    "code": ("证券代码", "股票代码", "证券编码", "证券代号", "代码"),
    "name": ("证券名称", "股票名称", "证券简称", "名称"),
    "side": ("买卖标志", "业务名称", "交易类别", "委托类别", "买卖方向", "操作", "摘要", "业务"),
    "shares": ("成交数量", "成交股数", "发生数量", "成交笔数", "数量", "股数"),
    "price": ("成交均价", "成交价格", "成交价", "委托价格", "均价", "价格"),
    "amount": ("成交金额", "发生金额", "清算金额", "成交额", "金额"),
    "commission": ("净佣金", "手续费", "佣金", "费用"),
    "stamp": ("印花税",),
    "transfer": ("过户费",),
    "other_fee": ("其他费", "杂费", "附加费", "证管费", "规费", "经手费"),
}

# 这些业务不是「买卖股票」，一律跳过（否则红利入账会被当成买入）
_SKIP_KEYWORDS = (
    "撤单", "撤消", "撤销", "废单", "红利", "红股", "股息", "利息", "分红",
    "转托管", "申购", "中签", "配号", "配股", "送股", "转增", "拆股", "拆分",
    "新股", "回购", "逆回购", "结息", "银行", "划转", "转账", "托收", "代扣",
    "税", "费用", "担保品划转", "质押", "解押",
)

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")

# 导入记录的 id 前缀，便于与手工记账（T 前缀）、持仓纠正（S 前缀）区分
_ID_PREFIX = "I"


# ============================ 文件读取 ============================

def decode_bytes(raw: bytes) -> Tuple[str, str]:
    """字节 → 文本。返回 (文本, 编码名)。

    utf-8 有严格字节校验，非法序列必然抛错，所以「先试 utf-8 成功」是可信的；
    gb18030 几乎不会解码失败（会把任意字节映射成某个汉字），因此只能放在后面兜底。
    """
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(有损)"


def _pick_delimiter(text: str) -> str:
    """在前几行里数逗号 / 制表符 / 分号，取出现次数最多的那个。"""
    head = "\n".join(text.splitlines()[:5])
    candidates = {",": head.count(","), "\t": head.count("\t"), ";": head.count(";")}
    best = max(candidates, key=lambda k: candidates[k])
    return best if candidates[best] > 0 else ","


def _read_rows(raw: bytes, filename: str) -> Tuple[List[List[str]], str, str]:
    """把文件读成二维字符串表。返回 (行列表, 编码, 格式说明)。"""
    lower = (filename or "").lower()

    if lower.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise ValueError("读取 Excel 需要 openpyxl，当前环境未安装") from exc
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        rows: List[List[str]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in row])
        wb.close()
        return rows, "excel", "xlsx"

    if lower.endswith(".xls"):
        raise ValueError(
            "不支持旧版 .xls 二进制格式（需要 xlrd）。"
            "请用 Excel 打开后「另存为」.xlsx 或 .csv 再导入。"
        )

    text, encoding = decode_bytes(raw)
    delim = _pick_delimiter(text)
    fmt = {",": "csv", "\t": "tsv", ";": "csv(;)"}[delim]
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [[("" if c is None else str(c)) for c in row] for row in reader]
    return rows, encoding, fmt


# ============================ 表头与列映射 ============================

def _clean_cell(value: Any) -> str:
    return re.sub(r"\s+", "", str(value if value is not None else ""))


def _map_columns(header: List[str]) -> Dict[str, int]:
    """表头 → {字段名: 列下标}。精确匹配优先，再用「别名包含于列名」兜底。"""
    norm = [_clean_cell(c) for c in header]
    mapping: Dict[str, int] = {}

    # 1) 精确匹配
    for idx, col in enumerate(norm):
        if not col:
            continue
        for field, aliases in _COLUMN_ALIASES.items():
            if field in mapping:
                continue
            if col in aliases:
                mapping[field] = idx

    # 2) 包含匹配：长别名优先，避免「金额」抢「成交金额」这种短词误配
    pairs: List[Tuple[int, str, int]] = []
    taken_cols = set(mapping.values())
    for field, aliases in _COLUMN_ALIASES.items():
        if field in mapping:
            continue
        for alias in aliases:
            for idx, col in enumerate(norm):
                if not col or idx in taken_cols:
                    continue
                if alias in col:
                    pairs.append((len(alias), field, idx))
    pairs.sort(key=lambda p: -p[0])
    for _, field, idx in pairs:
        if field in mapping or idx in set(mapping.values()):
            continue
        mapping[field] = idx

    return mapping


def _find_header(rows: List[List[str]], max_scan: int = 40) -> Tuple[int, Dict[str, int]]:
    """券商对账单前面常有若干行标题（账号、期间、说明），要往后找真正的表头行。

    判定标准：该行能映射出「代码」且能映射出「数量或金额」。
    """
    for i, row in enumerate(rows[:max_scan]):
        if not row:
            continue
        mapping = _map_columns(row)
        if "code" in mapping and ("shares" in mapping or "amount" in mapping):
            return i, mapping
    return -1, {}


# ============================ 值解析 ============================

_NUM_CLEAN = re.compile(r"[,\s¥￥元股]")


def to_float(value: Any) -> Optional[float]:
    """把 '1,000.00' / '¥27.35' / '1000股' 这类脏数据转成 float。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _NUM_CLEAN.sub("", str(value).replace("\u3000", ""))
    if text in ("", "-", "--", "/", "—"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_shares(value: Any) -> Optional[int]:
    num = to_float(value)
    if num is None:
        return None
    return int(round(abs(num)))


def to_date(value: Any) -> Optional[str]:
    """各种日期写法归一成 'YYYY-MM-DD'。"""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if not text:
        return None
    text = text.split(" ")[0].split("T")[0]
    m = re.match(r"^(\d{4})[-/.]?(\d{1,2})[-/.]?(\d{1,2})$", text)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d"):
        try:
            return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def detect_side(value: Any) -> Optional[str]:
    """业务描述 → 'buy' / 'sell' / None。

    含「买」不含「卖」→ 买；含「卖」不含「买」→ 卖。
    撤单、红利、股息、申购、送股、转账这类非买卖业务返回 None（跳过）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    low = text.lower()
    if low in ("buy", "b", "买入", "证券买入"):
        return "buy"
    if low in ("sell", "s", "卖出", "证券卖出"):
        return "sell"

    # 撤单必须最优先：'买入撤单' 同时含买和卖是不可能的，但含「撤」就要丢
    if any(k in text for k in _SKIP_KEYWORDS):
        return None

    has_buy = "买" in text or "buy" in low
    has_sell = "卖" in text or "sell" in low
    if has_buy and not has_sell:
        return "buy"
    if has_sell and not has_buy:
        return "sell"
    return None


# ============================ 解析成标准记录 ============================

def _make_id(trade_date: str, sec_id: str, side: str, shares: int,
             price: Optional[float], occurrence: int) -> str:
    """内容指纹 → 稳定 id。同一笔交易重复导入得到同一个 id，落库时天然去重。"""
    payload = f"{trade_date}|{sec_id}|{side}|{shares}|{(price or 0):.4f}|{occurrence}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
    return f"{_ID_PREFIX}{trade_date.replace('-', '')}{digest}"


def parse_rows(rows: List[List[str]], source_label: str = "") -> Dict[str, Any]:
    """二维表 → 标准记录列表 + 跳过明细。

    返回的 records 尚未做成本/现金重放，只有成交事实。
    """
    header_idx, mapping = _find_header(rows)
    if header_idx < 0:
        return {
            "ok": False,
            "error": "找不到表头：前 40 行里没有同时包含「证券代码」和「数量/金额」的行。"
                     "请确认这是交割单或资金流水，且没有被截断表头。",
            "records": [],
            "skipped": [],
        }

    missing = [f for f in ("date", "code", "side", "shares") if f not in mapping]
    if missing:
        return {
            "ok": False,
            "error": f"表头缺少必需列：{'、'.join(missing)}。识别到的列映射：{mapping}",
            "records": [],
            "skipped": [],
        }

    def cell(row: List[str], field: str) -> str:
        idx = mapping.get(field)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx] if row[idx] is not None else "").strip()

    records: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    occurrence_counter: Dict[str, int] = {}

    for offset, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not row or not any(str(c).strip() for c in row):
            continue

        raw_date = cell(row, "date")
        raw_code = cell(row, "code")
        raw_side = cell(row, "side")
        raw_shares = cell(row, "shares")
        raw_name = cell(row, "name")

        # 表尾的「合计」「小计」行会混进来，直接丢
        joined = "".join(str(c) for c in row)
        if "合计" in joined or "总计" in joined or "小计" in joined:
            continue

        trade_date = to_date(raw_date)
        if not trade_date:
            skipped.append({"row": offset, "reason": f"日期无法识别：{raw_date}", "raw": joined[:80]})
            continue

        side = detect_side(raw_side)
        if not side:
            if raw_shares or raw_code:
                skipped.append({"row": offset, "reason": f"非买卖业务：{raw_side or '(空)'}", "raw": joined[:80]})
            continue

        shares = to_shares(raw_shares)
        if not shares:
            skipped.append({"row": offset, "reason": f"数量无法识别：{raw_shares}", "raw": joined[:80]})
            continue

        sec_id = normalize_secid(raw_code)
        if not sec_id:
            skipped.append({
                "row": offset,
                "reason": f"代码无法归一（非沪深北 A 股？）：{raw_code}",
                "raw": joined[:80],
            })
            continue

        price = to_float(cell(row, "price"))
        amount = to_float(cell(row, "amount"))
        if price is None and amount:
            price = round(amount / shares, 4) if shares else None
        if amount is None and price is not None:
            amount = round(price * shares, 2)
        if price is None:
            skipped.append({"row": offset, "reason": "既没有成交价也没有成交金额", "raw": joined[:80]})
            continue

        # 费用：分项优先，缺失则标记为估算
        fee_parts = {
            "commission": to_float(cell(row, "commission")) or 0.0,
            "stamp": to_float(cell(row, "stamp")) or 0.0,
            "transfer": to_float(cell(row, "transfer")) or 0.0,
            "other": to_float(cell(row, "other_fee")) or 0.0,
        }
        declared_fee = sum(fee_parts.values())
        if declared_fee > 0:
            fee_detail = {
                "total": round(declared_fee, 2),
                "commission": round(fee_parts["commission"] + fee_parts["other"], 2),
                "stamp": round(fee_parts["stamp"], 2),
                "transfer": round(fee_parts["transfer"], 2),
                "estimated": False,
            }
        else:
            fee_detail = decision_rules.calc_trade_fee(side, abs(amount or 0.0))
            fee_detail["estimated"] = True

        fingerprint = f"{trade_date}|{sec_id}|{side}|{shares}|{round(float(price), 4)}"
        occurrence = occurrence_counter.get(fingerprint, 0)
        occurrence_counter[fingerprint] = occurrence + 1

        records.append({
            "id": _make_id(trade_date, sec_id, side, shares, round(float(price), 4), occurrence),
            "created_at": f"{trade_date} 15:00:00",
            "trade_date": trade_date,
            "secID": sec_id,
            "name": raw_name or "",
            "side": side,
            "shares": shares,
            "price": round(float(price), 4),
            "price_estimated": False,
            "amount": round(float(amount or price * shares), 2),
            "fee": fee_detail["total"],
            "fee_detail": fee_detail,
            "realized_pnl": None,
            "note": "历史导入",
            "import_source": source_label,
            "import_row": offset,
        })

    # 同一日期内保持文件原始顺序（csv 读取顺序稳定），id 只做兜底二级排序
    records.sort(key=lambda r: (r["trade_date"], r.get("import_row") or 0))
    return {"ok": True, "header_row": header_idx + 1, "mapping": mapping,
            "records": records, "skipped": skipped}


# ============================ 重放 ============================

def replay(records: List[Dict[str, Any]],
           baseline: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """按成交时间正序重放，算出期末持仓、摊薄成本、已实现盈亏与净现金流。

    口径与 ``trade_ledger`` 完全一致（费用计入买入成本、卖出成本不变），
    只是不做四池闸门、不查行情、不写库。

    ``baseline`` 是期初持仓 ``{secID: {"shares": n, "cost": c}}``，
    用于「流水不从建仓开始」的场景——否则前面那些卖出会因为持股为 0 而算不出已实现盈亏。
    """
    state: Dict[str, Dict[str, Any]] = {}
    for sec, pos in (baseline or {}).items():
        state[sec] = {
            "shares": int(pos.get("shares") or 0),
            "cost": float(pos.get("cost") or 0.0),
            "name": str(pos.get("name") or ""),
        }

    warnings: List[str] = []
    orphan_sells: List[Dict[str, Any]] = []
    net_cash = 0.0
    realized_total = 0.0
    realized_count = 0

    for rec in sorted(records, key=lambda r: (str(r.get("trade_date") or ""), str(r.get("id") or ""))):
        sec = rec["secID"]
        slot = state.setdefault(sec, {"shares": 0, "cost": 0.0, "name": rec.get("name") or ""})
        if not slot["name"] and rec.get("name"):
            slot["name"] = rec["name"]

        held = int(slot["shares"])
        held_cost = float(slot["cost"])
        side = rec["side"]
        shares = int(rec["shares"])

        rec["shares_before"] = held
        rec["cost_before"] = round(held_cost, 4)

        if side == "buy":
            new_shares = held + shares
            new_cost = (held_cost * held + rec["amount"] + rec["fee"]) / new_shares if new_shares else 0.0
            net_cash -= (rec["amount"] + rec["fee"])
            slot["shares"] = new_shares
            slot["cost"] = new_cost
        else:
            if shares > held:
                orphan_sells.append({
                    "trade_date": rec["trade_date"], "secID": sec, "name": rec.get("name"),
                    "shares": shares, "held": held,
                    "note": "卖出股数超过当时的推算持仓",
                })
                warnings.append(
                    f"{rec['trade_date']} {rec.get('name') or sec} 卖出 {shares} 股，"
                    f"但此前推算只有 {held} 股——流水可能没从建仓开始，建议提供期初持仓"
                )
                shares = held if held > 0 else 0
                rec["shares"] = shares
            new_shares = held - shares
            new_cost = held_cost if new_shares > 0 else 0.0
            if held_cost > 0 and shares > 0:
                rec["realized_pnl"] = round((rec["price"] - held_cost) * shares - rec["fee"], 2)
                realized_total += rec["realized_pnl"]
                realized_count += 1
            else:
                rec["realized_pnl"] = None
                if shares > 0:
                    warnings.append(
                        f"{rec['trade_date']} {rec.get('name') or sec} 卖出无成本记录，"
                        f"已实现盈亏无法计算（只需补期初成本即可修正）"
                    )
            net_cash += (rec["amount"] - rec["fee"])
            slot["shares"] = new_shares
            slot["cost"] = new_cost

        rec["shares_after"] = int(slot["shares"])
        rec["cost_after"] = round(float(slot["cost"]), 4)
        rec["cash_before"] = None
        rec["cash_after"] = None
        rec["cash_source"] = "import"

    positions = {
        sec: {"shares": int(p["shares"]), "cost": round(float(p["cost"]), 4), "name": p["name"]}
        for sec, p in state.items()
        if int(p["shares"]) > 0
    }
    closed = [sec for sec, p in state.items() if int(p["shares"]) <= 0]

    return {
        "positions": positions,
        "closed": closed,
        "net_cash_flow": round(net_cash, 2),
        "realized_pnl": round(realized_total, 2) if realized_count else None,
        "realized_count": realized_count,
        "warnings": warnings,
        "orphan_sells": orphan_sells,
    }


# ============================ 对外入口 ============================

def analyze(raw: bytes, filename: str,
            baseline: Optional[Dict[str, Dict[str, Any]]] = None,
            current_positions: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """预览：解析 + 重放 + 与当前持仓对比，不写任何东西。

    ``current_positions`` 传当前 ``favorites`` 的持仓快照，
    用来并排展示「导入后会变成什么」和「现在是什么」，差异由用户自己判断。
    """
    try:
        rows, encoding, fmt = _read_rows(raw, filename)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("流水文件读取失败 file=%s error=%r", filename, exc)
        return {"ok": False, "error": f"读取失败：{exc}"}

    parsed = parse_rows(rows, source_label=fmt)
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error"), "encoding": encoding, "format": fmt}

    records = parsed["records"]
    if not records:
        return {
            "ok": False,
            "error": "没有解析出任何买卖记录，请检查文件是否选错（例如导成了持仓表而不是交割单）",
            "encoding": encoding, "format": fmt,
            "skipped": parsed["skipped"][:20],
        }

    result = replay(records, baseline)

    current = current_positions or {}
    diff: List[Dict[str, Any]] = []
    all_keys = set(result["positions"]) | set(current)
    for sec in sorted(all_keys):
        after = result["positions"].get(sec) or {}
        now = current.get(sec) or {}
        after_shares = int(after.get("shares") or 0)
        now_shares = int(now.get("shares") or 0)
        if after_shares == now_shares and abs(float(after.get("cost") or 0) - float(now.get("cost") or 0)) < 0.005:
            continue
        diff.append({
            "secID": sec,
            "name": after.get("name") or now.get("name") or sec,
            "shares_now": now_shares,
            "shares_after": after_shares,
            "shares_delta": after_shares - now_shares,
            "cost_now": round(float(now.get("cost") or 0), 4),
            "cost_after": round(float(after.get("cost") or 0), 4),
        })

    dates = [r["trade_date"] for r in records]
    return {
        "ok": True,
        "encoding": encoding,
        "format": fmt,
        "header_row": parsed.get("header_row"),
        "column_mapping": parsed.get("mapping"),
        "date_range": [min(dates), max(dates)],
        "parsed_count": len(records),
        "skipped_count": len(parsed["skipped"]),
        "skipped": parsed["skipped"][:20],
        "records_preview": records[:20],
        "positions": result["positions"],
        "closed": result["closed"],
        "position_diff": diff,
        "net_cash_flow": result["net_cash_flow"],
        "realized_pnl": result["realized_pnl"],
        "realized_count": result["realized_count"],
        "warnings": result["warnings"][:20],
        "orphan_sells": result["orphan_sells"][:20],
        "fee_estimated_count": sum(1 for r in records if (r.get("fee_detail") or {}).get("estimated")),
    }


def commit(records: List[Dict[str, Any]], baseline: Optional[Dict[str, Dict[str, Any]]] = None,
           apply_positions: bool = True) -> Dict[str, Any]:
    """正式落库：合并旧流水（同 id 覆盖）→ 整表写回 → 同步持仓。

    ``apply_positions=False`` 时只导流水、不动持仓（用于「流水只是参考」的场景）。
    """
    from .trade_ledger import _LEDGER_LOCK, _MAX_RECORDS, trade_ledger

    if not records:
        return {"ok": False, "error": "没有可导入的记录"}

    result = replay(records, baseline)

    with _LEDGER_LOCK:
        existing = trade_ledger._load()
        merged: Dict[str, Dict[str, Any]] = {str(r.get("id")): r for r in existing if r.get("id")}
        before_count = len(merged)
        for rec in records:
            merged[str(rec["id"])] = rec
        rows = sorted(merged.values(), key=lambda r: (str(r.get("trade_date") or ""),
                                                     str(r.get("created_at") or "")))
        truncated = max(0, len(rows) - _MAX_RECORDS)
        trade_ledger._save(rows)

        applied: List[Dict[str, Any]] = []
        if apply_positions:
            from .stock_service import stock_service
            svc = stock_service
            current = {str(it.get("secID") or ""): it for it in (getattr(svc, "_favorite_cache", []) or [])}
            for sec, pos in result["positions"].items():
                shares = int(pos["shares"])
                cost = round(float(pos["cost"]), 4)
                if sec in current:
                    svc.update_favorite_position(sec, shares=shares, cost=cost)
                else:
                    svc.add_favorite(sec, shares=shares, cost=cost, name=pos.get("name"))
                applied.append({"secID": sec, "shares": shares, "cost": cost,
                                "name": pos.get("name") or sec})

    warnings = list(result["warnings"])
    if truncated:
        warnings.append(f"流水总数超出保留上限 {_MAX_RECORDS} 条，最早的 {truncated} 条已被截断")

    return {
        "ok": True,
        "imported": len(records),
        "total_before": before_count,
        "total_after": len(rows) - truncated,
        "deduplicated": max(0, before_count + len(records) - len(merged)),
        "positions_applied": applied,
        "closed": result["closed"],
        "net_cash_flow": result["net_cash_flow"],
        "realized_pnl": result["realized_pnl"],
        "warnings": warnings,
    }
