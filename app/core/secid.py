"""股票代码归一化（单一真源）。

为什么需要
----------
同一只票在不同地方写法不一样：

- 自选表里存的是规范形式 ``000001.SZ``（``favorites.secID``）
- 因子选股器的「全市场」池返回的是纯 6 位数字 ``000001``
  （来自 ``ak.stock_info_a_code_name()``）
- 沪深300 / 中证500 成分股返回 ``000001.SZ`` / ``601036.SH``
- 用户在表单里可能手填 ``sh600000`` / ``sz.300750``

任何「是不是同一只票」的比较（是否已在自选、加自选去重、点开详情）
都必须先过这里，否则会出现「明明在自选里，界面却显示未添加」。

归一化后的形式统一为 ``digits.MARKET``（MARKET 大写：SH / SZ / BJ）。
"""
from __future__ import annotations

import re
from typing import Dict, Optional

# 按 6 位数字推断市场（只在前缀里没写明市场时才用）。
# 6 → 沪（600/601/603/605/688），0/3 → 深（000/002/300/301），
# 92 → 北交所新代码段（920xxx，2024 年起启用），
# 4/8 → 北交所旧代码段（43/83/87），9（其余）→ 沪（900xxx 沪B）。
# ⚠️ 不要把 9 开头一律当沪市：全市场代码表里 9 开头全部是 920xxx（北交所），
# 没有 900xxx。当沪市存进自选表会变成 920000.SH，永远查不到行情。
_MARKET_BY_PREFIX = {
    "6": "sh",
    "0": "sz",
    "3": "sz",
    "4": "bj",
    "8": "bj",
    "9": "sh",
}

# 基金 / ETF / LOF 的两位前缀。1 和 5 开头不能按首位一刀切：
# 11xxxx 是沪市可转债、12xxxx 是深市可转债，都不是基金，混进来会被当成
# 一只取不到行情的「股票」塞进自选。所以只认明确的 ETF/LOF 段。
# 实际遇到的是账本流水里的 159934（深市 ETF），不加这段会整批跳过。
_FUND_PREFIX_SH = ("50", "51", "52", "56", "58")
_FUND_PREFIX_SZ = ("15", "16", "18")


def split_secid(code: str) -> Dict[str, str]:
    """拆成 ``{'digits': '000001', 'market': 'sz'}``；无法识别抛 ValueError。

    保持历史行为（stock_service._normalize_stock_code 的原始语义），
    抽出来只是为了让它有一个不依赖服务实例的单一真源。
    """
    raw = (code or "").strip().lower().replace("sse", "sh").replace("szse", "sz")
    m = re.search(r"(sh|sz|bj)[\.\-]?(\d{6})", raw)
    if m:
        market, digits = m.group(1), m.group(2)
    else:
        digits = re.sub(r"\D", "", raw)
        market = _infer_market(digits)
    if len(digits) != 6 or not market:
        raise ValueError(f"Unsupported stock code format: {code}")
    return {"digits": digits, "market": market}


def _infer_market(digits: str) -> str:
    """按 6 位数字推断市场前缀。"""
    if len(digits) < 6:
        return ""
    # 北交所新代码段 920xxx：2024 年起启用，全市场代码表里的 9 开头全是它。
    if digits.startswith("92"):
        return "bj"
    two = digits[:2]
    if two in _FUND_PREFIX_SH:
        return "sh"
    if two in _FUND_PREFIX_SZ:
        return "sz"
    return _MARKET_BY_PREFIX.get(digits[:1], "")


def normalize_secid(code: str) -> Optional[str]:
    """归一成 ``digits.MARKET``；无法识别返回 None（而不是抛异常）。

    选股器/前端批量比对场景用这个：一只票解析不出来时，
    不应该让整页渲染失败，跳过即可。
    """
    try:
        parsed = split_secid(code)
    except Exception:  # noqa: BLE001
        return None
    return f"{parsed['digits']}.{parsed['market'].upper()}"
