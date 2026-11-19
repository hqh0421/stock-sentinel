"""策略表达式引擎（唯一事实源）。

为什么存在
----------
改造前 `strategy_service.py` 与 `backtest_service.py` 各有一套 `eval(expr, {"__builtins__": {}}, env)`，
外加 `stock_service._normalize_expr` 第三份归一化。三份实现既重复，又留下了真实的安全面：

- `__builtins__` 置空并不构成沙箱，`(1).__class__.__mro__[-1].__subclasses__()` 一类
  属性链可以逃出去；表达式由任意登录用户经 `/api/stock/strategy/save` 写入。
- 无长度、节点数、回看长度约束，`10**10**10` 之类的表达式能把进程打挂。

本模块用 **AST 白名单解释器** 取代 eval：只解析，再按允许的节点类型逐节点求值，
不调用 compile/eval/exec，属性访问、下标、推导式、lambda、字符串下标一概不进白名单。
监控信号、回测撮合、因子有效性检验都走这里，保证「同一表达式、同一结果」。

兼容性
------
保留原有语法：字段名（`close` / `MA20` / `RSI6`）、`and` / `or` / `not`、
四则运算、比较，以及 `REF/MA/CROSS/LLV/HHV/ATR/abs/max/min/round` 十个函数。
`CROSS(A,B)`、`MA(A,5)`、`REF(A,1)` 由 `normalize_expr` 自动把字段名转成字符串字面量。
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any, Dict, List, Optional, Tuple

MAX_EXPR_LEN = 500
MAX_AST_NODES = 120
MAX_LOOKBACK = 250
MAX_CALL_ARGS = 3

ALLOWED_FUNCS = frozenset({
    "REF", "MA", "CROSS", "LLV", "HHV", "ATR", "abs", "max", "min", "round",
})

_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}
_UNARYOPS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
    ast.Not: lambda a: not a,
}
_CMPOPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}

_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call,
    ast.And, ast.Or, ast.Not, ast.UAdd, ast.USub,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


class ExprError(ValueError):
    """表达式非法：语法、白名单、长度或复杂度不达标。"""


def normalize_expr(expr: str) -> str:
    """把 CROSS(A,B) / MA(A,5) / REF(A,1) 的裸字段名转成字符串字面量。

    这是全项目唯一一份归一化实现；监控、回测、因子库保存都调用它。
    """
    if not expr:
        return ""
    expr = re.sub(
        r"\bCROSS\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        r"CROSS('\1','\2')", expr,
    )
    expr = re.sub(
        r"\b(MA|REF)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*(,)",
        r"\1('\2'\3", expr,
    )
    return expr


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _clip_lookback(n: Any, default: int = 20) -> int:
    """回看窗口统一限幅，防止 MA(close, 999999999) 造成长循环。"""
    try:
        value = int(n)
    except Exception:
        value = default
    return max(1, min(MAX_LOOKBACK, value))


def build_kline_env(series: List[Dict[str, Any]], idx: int) -> Dict[str, Any]:
    """构造单根 bar 的表达式环境（唯一实现，服务监控与回测）。

    字段来自 bar 本身（如 close / MA20 / RSI6 / DIF），函数按全序列回看计算。
    """
    bar = series[idx]

    def REF(field: Any, n: Any = 1) -> Any:
        if not isinstance(field, str):
            return field
        target = idx - _clip_lookback(n, 1)
        return series[target].get(field) if 0 <= target < len(series) else None

    def MA(field: Any, n: Any = 5) -> Optional[float]:
        if not isinstance(field, str):
            return None
        start = max(0, idx - _clip_lookback(n, 5) + 1)
        vals = [_to_float(series[i].get(field)) for i in range(start, idx + 1)]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def CROSS(fast_field: Any, slow_field: Any) -> bool:
        if idx <= 0 or not isinstance(fast_field, str) or not isinstance(slow_field, str):
            return False
        curr_fast = _to_float(series[idx].get(fast_field))
        curr_slow = _to_float(series[idx].get(slow_field))
        prev_fast = _to_float(series[idx - 1].get(fast_field))
        prev_slow = _to_float(series[idx - 1].get(slow_field))
        if None in (curr_fast, curr_slow, prev_fast, prev_slow):
            return False
        return curr_fast > curr_slow and prev_fast <= prev_slow

    def LLV(field: Any, n: Any = 20) -> Optional[float]:
        if not isinstance(field, str):
            return None
        start = max(0, idx - _clip_lookback(n, 20) + 1)
        vals = [_to_float(series[i].get(field)) for i in range(start, idx + 1)]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None

    def HHV(field: Any, n: Any = 20) -> Optional[float]:
        if not isinstance(field, str):
            return None
        start = max(0, idx - _clip_lookback(n, 20) + 1)
        vals = [_to_float(series[i].get(field)) for i in range(start, idx + 1)]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def ATR(n: Any = 14) -> Optional[float]:
        lo_i = max(1, idx - _clip_lookback(n, 14) + 1)
        trs: List[float] = []
        for i in range(lo_i, idx + 1):
            hi = _to_float(series[i].get("high"))
            lo = _to_float(series[i].get("low"))
            if hi is None or lo is None:
                continue
            tr = hi - lo
            pc = _to_float(series[i - 1].get("close")) if i - 1 >= 0 else None
            if pc is not None:
                tr = max(tr, abs(hi - pc), abs(lo - pc))
            trs.append(tr)
        return sum(trs) / len(trs) if trs else None

    env: Dict[str, Any] = dict(bar)
    env.update({
        "REF": REF, "MA": MA, "CROSS": CROSS,
        "LLV": LLV, "HHV": HHV, "ATR": ATR,
        "abs": abs, "max": max, "min": min, "round": round,
    })
    return env


def parse_expr(expr: str, normalized: bool = False) -> ast.Expression:
    """解析并校验表达式；任何不在白名单里的结构都直接拒绝。"""
    text = expr if normalized else normalize_expr(expr or "")
    if len(text) > MAX_EXPR_LEN:
        raise ExprError(f"表达式过长（{len(text)} 字符，上限 {MAX_EXPR_LEN}）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"表达式语法错误：{exc.msg}") from exc

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise ExprError(f"表达式过于复杂（{len(nodes)} 个节点，上限 {MAX_AST_NODES}）")

    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"不允许的表达式结构：{type(node).__name__}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool, type(None))):
            raise ExprError("只允许数字、字符串、布尔与空值常量")
        if isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                raise ExprError("不允许赋值")
            if node.id.startswith("_"):
                raise ExprError("不允许访问内部名称")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExprError("函数名必须是白名单内的标识符")
            if node.func.id not in ALLOWED_FUNCS:
                raise ExprError(f"不允许调用函数 {node.func.id}")
            if node.keywords or len(node.args) > MAX_CALL_ARGS:
                raise ExprError("函数调用只支持最多 3 个位置参数，不支持关键字参数")
    return tree


def check_expr(expr: str) -> Tuple[bool, str]:
    """供保存策略前校验：`(是否合法, 错误说明)`。"""
    if not (expr or "").strip():
        return True, ""
    try:
        parse_expr(expr)
        return True, ""
    except ExprError as exc:
        return False, str(exc)


def evaluate(expr: str, env: Dict[str, Any]) -> Any:
    """在给定环境里求值（不走 eval）。字段名不存在时返回 None，与旧行为一致。"""
    tree = parse_expr(expr)

    def walk(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.UnaryOp):
            op = _UNARYOPS.get(type(node.op))
            if op is None:
                raise ExprError("不允许的一元运算")
            return op(walk(node.operand))
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise ExprError("不允许的二元运算（幂运算已禁用）")
            return op(walk(node.left), walk(node.right))
        if isinstance(node, ast.BoolOp):
            values = node.values
            if isinstance(node.op, ast.And):
                result: Any = True
                for item in values:
                    result = walk(item)
                    if not result:
                        return result
                return result
            if isinstance(node.op, ast.Or):
                result = False
                for item in values:
                    result = walk(item)
                    if result:
                        return result
                return result
            raise ExprError("不允许的布尔运算")
        if isinstance(node, ast.Compare):
            left = walk(node.left)
            for op_node, comparator in zip(node.ops, node.comparators):
                op = _CMPOPS.get(type(op_node))
                if op is None:
                    raise ExprError("不允许的比较运算")
                right = walk(comparator)
                if left is None or right is None:
                    return False
                if not op(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            func = env.get(node.func.id)
            if not callable(func):
                raise ExprError(f"函数 {node.func.id} 不可用")
            return func(*[walk(arg) for arg in node.args])
        raise ExprError(f"不允许的表达式结构：{type(node).__name__}")

    return walk(tree)


def eval_signal_expr(expr: str, series: List[Dict[str, Any]], idx: int) -> bool:
    """单根 bar 的信号判定；异常一律视为未触发，不让坏表达式打断监控。"""
    if not (expr or "").strip():
        return False
    try:
        return bool(evaluate(expr, build_kline_env(series, idx)))
    except (ExprError, TypeError, ValueError, ZeroDivisionError, OverflowError, IndexError):
        return False
