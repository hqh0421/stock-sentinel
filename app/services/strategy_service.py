"""监控信号表达式判定（薄封装）。

表达式的归一化、安全解析与求值全部收敛到 `app.core.expr`（唯一事实源）。
改造前这里与 `backtest_service._make_env` 各有一套 eval，且 `__builtins__` 置空
并不能挡住属性链逃逸——表达式由任意登录用户保存，属于真实攻击面。

本类只保留调用方约定的返回结构，不再复制任何求值逻辑。
"""

from typing import Any, Dict, List, Optional

from ..core.expr import check_expr, eval_signal_expr, normalize_expr


class StrategyService:
    def __init__(self, host: Any) -> None:
        self.host = host

    def normalize_expr(self, expr: str) -> str:
        return normalize_expr(expr)

    def validate_expr(self, expr: str) -> tuple:
        """保存策略前校验；返回 (是否合法, 错误说明)。"""
        return check_expr(expr)

    def eval_signal(self, code: str, entry_expr: str, exit_expr: str,
                    kline_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        data = kline_data if kline_data is not None else self.host.get_k_line_data(
            code, frequency="d", caller="strategy_eval"
        )
        if len(data) < 2:
            return {"ok": False, "entry_hit": False, "exit_hit": False,
                    "error": "insufficient_kline_data", "kline_count": len(data)}
        idx = len(data) - 1
        return {
            "ok": True,
            "entry_hit": eval_signal_expr(entry_expr or "", data, idx),
            "exit_hit": eval_signal_expr(exit_expr or "", data, idx),
            "error": "",
            "kline_count": len(data),
        }
