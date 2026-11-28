"""A 股交易时段与交易日历判断。

基于服务器本地「系统时间」(datetime.now())。A 股规则：
- 周一至周五为交易日，周末(周六/周日)休市（即使国务院调休上班，周末也不开市）；
- 法定节假日休市：从 ``config/market_holidays.json`` 读取（来源：沪深交易所年度休市公告），
  未提供配置文件时使用内置日历兜底；
- 上午盘 09:30-11:30，下午盘 13:00-15:00；
- 午休 11:30-13:00 默认按休市处理（即不监控、不提醒）。

日历维护：每年 12 月底交易所公布次年休市安排后，更新 config/market_holidays.json 即可，
无需改代码；日历覆盖范围外的日期按「周末休市 + 平日开市」近似，并在 market_clock() 中带提示。
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# A 股交易时段（本地时间，24 小时制）
MORNING_OPEN = datetime.time(9, 30)
MORNING_CLOSE = datetime.time(11, 30)
AFTERNOON_OPEN = datetime.time(13, 0)
AFTERNOON_CLOSE = datetime.time(15, 0)

# 是否把午休也当作休市（默认 True：11:30-13:00 不监控）
TREAT_LUNCH_AS_CLOSED = True

# 内置节假日日历（兜底用；与 config/market_holidays.json 同源：交易所年度休市公告）
# 注意：只列周一至周五的休市日即可，周末由 weekday() 判断。
_BUILTIN_HOLIDAYS: List[Dict[str, str]] = [
    # 2026 年（上交所 上证公告〔2025〕45号）
    {"date": "2026-01-01", "name": "元旦"},
    {"date": "2026-01-02", "name": "元旦"},
    {"date": "2026-02-16", "name": "春节"},
    {"date": "2026-02-17", "name": "春节"},
    {"date": "2026-02-18", "name": "春节"},
    {"date": "2026-02-19", "name": "春节"},
    {"date": "2026-02-20", "name": "春节"},
    {"date": "2026-02-23", "name": "春节"},
    {"date": "2026-04-06", "name": "清明节"},
    {"date": "2026-05-01", "name": "劳动节"},
    {"date": "2026-05-04", "name": "劳动节"},
    {"date": "2026-05-05", "name": "劳动节"},
    {"date": "2026-06-19", "name": "端午节"},
    {"date": "2026-09-25", "name": "中秋节"},
    {"date": "2026-10-01", "name": "国庆节"},
    {"date": "2026-10-02", "name": "国庆节"},
    {"date": "2026-10-05", "name": "国庆节"},
    {"date": "2026-10-06", "name": "国庆节"},
    {"date": "2026-10-07", "name": "国庆节"},
    # 2027 年（官方安排公布后请更新为完整公告）
    {"date": "2027-01-01", "name": "元旦"},
]

_CONFIG_HOLIDAYS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "market_holidays.json"

_holiday_cache: Dict[str, str] = {}
_holiday_cache_mtime: Optional[float] = None
_holiday_source: str = "builtin"
_holiday_coverage_end: Optional[str] = None


def _load_holidays() -> Dict[str, str]:
    """读取节假日日历（date -> 节日名）。配置文件按 mtime 热更新。"""
    global _holiday_cache, _holiday_cache_mtime, _holiday_source, _holiday_coverage_end
    mtime: Optional[float] = None
    try:
        mtime = _CONFIG_HOLIDAYS_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if _holiday_cache and _holiday_cache_mtime == mtime:
        return _holiday_cache
    entries: List[Dict[str, str]] = []
    if mtime is not None:
        try:
            data = json.loads(_CONFIG_HOLIDAYS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                entries = data.get("holidays") or []
                _holiday_source = "config/market_holidays.json"
            elif isinstance(data, list):
                entries = data
                _holiday_source = "config/market_holidays.json"
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 market_holidays.json 失败，使用内置日历: %r", exc)
    if not entries:
        entries = _BUILTIN_HOLIDAYS
        _holiday_source = "builtin"
    mapping: Dict[str, str] = {}
    for item in entries:
        date = str(item.get("date") or "").strip()
        if date:
            mapping[date] = str(item.get("name") or "节假日").strip() or "节假日"
    _holiday_cache = mapping
    _holiday_cache_mtime = mtime
    _holiday_coverage_end = max(mapping.keys()) if mapping else None
    return _holiday_cache


def now_local() -> datetime.datetime:
    return datetime.datetime.now()


def is_weekend(dt: datetime.datetime | None = None) -> bool:
    dt = dt or now_local()
    # weekday():  Monday=0 ... Saturday=5, Sunday=6
    return dt.weekday() >= 5


def holiday_name(dt: datetime.datetime | None = None) -> Optional[str]:
    """若指定日期为法定节假日休市，返回节日名，否则 None。"""
    d = (dt or now_local()).date()
    return _load_holidays().get(d.isoformat())


def is_trading_day(dt: datetime.datetime | None = None) -> bool:
    """是否交易日（周一至周五且非法定节假日；周末即使调休上班也不开市）。"""
    d = (dt or now_local()).date()
    return d.weekday() < 5 and d.isoformat() not in _load_holidays()


def next_trading_day(dt: datetime.datetime | None = None, max_scan: int = 30) -> Optional[datetime.date]:
    """从指定时间起往后找下一个交易日（不含当天）。找不到返回 None。"""
    d = (dt or now_local()).date() + datetime.timedelta(days=1)
    holidays = _load_holidays()
    for _ in range(max_scan):
        if d.weekday() < 5 and d.isoformat() not in holidays:
            return d
        d += datetime.timedelta(days=1)
    return None


def is_trading_now(
    dt: datetime.datetime | None = None,
    treat_lunch_as_closed: bool = TREAT_LUNCH_AS_CLOSED,
) -> bool:
    """当前是否为开盘交易时段。"""
    d = dt or now_local()
    if not is_trading_day(d):
        return False
    t = d.time()
    in_morning = MORNING_OPEN <= t <= MORNING_CLOSE
    in_afternoon = AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE
    if treat_lunch_as_closed:
        return in_morning or in_afternoon
    # 若午休也算开盘，则 09:30-15:00 连续视为开盘
    return MORNING_OPEN <= t <= AFTERNOON_CLOSE


def trading_status(dt: datetime.datetime | None = None) -> str:
    """返回当前交易状态的可读标识，便于日志/排查。"""
    d = dt or now_local()
    name = holiday_name(d)
    if name:
        return "holiday"  # 法定节假日休市
    if d.weekday() >= 5:
        return "weekend"  # 周末休市
    t = d.time()
    if t < MORNING_OPEN:
        return "before_open"
    if MORNING_OPEN <= t <= MORNING_CLOSE:
        return "morning_session"
    if t < AFTERNOON_OPEN:
        return "lunch_break"
    if AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE:
        return "afternoon_session"
    return "after_close"


STATUS_LABELS = {
    "weekend": "周末休市",
    "holiday": "法定节假日休市",
    "before_open": "盘前（未开盘）",
    "morning_session": "上午盘中",
    "lunch_break": "午间休市",
    "afternoon_session": "下午盘中",
    "after_close": "盘后（已收盘）",
}

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def market_clock(dt: datetime.datetime | None = None) -> Dict[str, Any]:
    """系统时钟 + 交易日历的完整快照（供 Agent 工具与接口使用）。"""
    d = dt or now_local()
    status = trading_status(d)
    name = holiday_name(d)
    is_trading_day_ = is_trading_day(d)
    next_day = next_trading_day(d)
    holidays = _load_holidays()
    return {
        "now": d.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": WEEKDAY_NAMES[d.weekday()],
        "is_trading_day": is_trading_day_,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "holiday_name": name,
        "next_trading_day": next_day.isoformat() if next_day else None,
        "sessions": "上午盘 09:30-11:30，下午盘 13:00-15:00（午休 11:30-13:00 休市）",
        "calendar_source": _holiday_source,
        "calendar_coverage_end": _holiday_coverage_end,
        "calendar_note": (
            f"节假日日历覆盖至 {_holiday_coverage_end}，之后日期按周末/平日近似判断，"
            "请更新 config/market_holidays.json"
        ) if _holiday_coverage_end else "节假日日历为空，仅按周末判断",
        "is_trading_now": is_trading_now(d),
    }
