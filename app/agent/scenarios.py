"""场景定义：每个场景 = 一套提示词 + 一个工具子集 + 步数/超时限制。

工具子集不是越多越好。工具越多，模型越容易乱试、token 越贵、响应越慢。
按场景裁剪是最有效的成本控制手段。

但裁剪有一条硬边界：**提示词里写了「必调」的工具，必须出现在该场景清单里**。
改造前 `get_sentiment_policy`、`get_value_position`、`get_industry_valuation`、
`get_sentiment_history`、`run_factor_backtest`、`evaluate_factor`、`get_alert_rules`
七个工具已注册却不在任何场景里，而 `prompts.py` 里明确要求
「给任何买入/加仓建议前必调 get_sentiment_policy」「判断贵不贵必调
get_industry_valuation」——运行时会直接拒绝模型调用，纪律形同虚设。

现在按职责把它们分配到需要的场景；此外 `runtime.py` 还有一层 preflight：
当模型给出的结论涉及买入/加仓却没调情绪政策时，系统强制补调一次再让它收尾。
"""

from __future__ import annotations

SCENARIOS = {
    "copilot": {
        "id": "copilot",
        "name": "分析顾问",
        "desc": "自然语言提问，Agent 自己调数据、自己算、自己下判断",
        "trigger": "用户提问",
        "max_steps": 12,
        "timeout": 240,
        "tools": [
            "get_system_clock", "get_market_status", "get_market_sentiment", "get_account_status", "get_position_summary",
            "list_favorites", "get_sell_target", "get_spot", "get_kline", "get_minute", "get_indices",
            "get_money_flow", "get_chip_distribution", "get_company_info",
            "get_trade_history", "run_backtest", "list_strategies",
            "update_account_cash", "get_trend_strategy", "get_settings", "run_monitor",
            "record_trade", "list_trades", "sync_position",
            "get_trade_stats", "get_trade_diagnosis", "sync_trade_ledger",
            "get_time_stop_report", "waive_time_stop",
            "get_sentiment_policy", "get_sentiment_history", "get_industry_valuation",
            "get_value_position", "run_factor_backtest", "evaluate_factor", "get_alert_rules",
            "get_allocation_advice", "suggest_pool_classification",
        ],
    },
    "sell": {
        "id": "sell",
        "name": "卖出决策",
        "desc": "按投资风格给出三档目标价、止损位与分批卖出方案",
        "trigger": "用户提问 / 持仓临近目标",
        "max_steps": 12,
        "timeout": 300,
        "tools": [
            "get_system_clock", "get_market_status", "get_market_sentiment",
            "get_sell_target", "list_favorites",
            "get_account_status", "get_position_summary", "get_kline", "get_money_flow",
            "get_chip_distribution", "get_company_info", "get_spot", "get_settings",
            "update_account_cash", "record_trade", "list_trades", "sync_position",
            "get_trade_stats", "get_trade_diagnosis",
            "get_time_stop_report", "waive_time_stop",
            "get_sentiment_policy", "get_sentiment_history", "get_industry_valuation",
            "get_value_position",
            "get_allocation_advice", "suggest_pool_classification",
        ],
    },
    "review": {
        "id": "review",
        "name": "信号复核",
        "desc": "监控信号推送到钉钉之前，先判一次真假",
        "trigger": "监控命中后（由 MonitorScheduler 自动调用）",
        "max_steps": 6,
        "timeout": 90,
        "tools": [
            "get_system_clock", "get_market_status", "get_kline", "get_minute",
            "get_indices", "get_money_flow", "get_company_info", "get_spot",
            "get_sentiment_policy",
        ],
    },
    "daily": {
        "id": "daily",
        "name": "盘后复盘",
        "desc": "收盘后自动生成复盘日报，含归因与明日预案",
        "trigger": "交易日 15:08（由 MonitorScheduler 自动调用）",
        "max_steps": 9,
        "timeout": 180,
        "tools": [
            "get_system_clock", "get_market_status", "get_market_sentiment", "get_account_status", "get_position_summary",
            "get_indices", "get_kline", "get_spot", "get_company_info", "get_sell_target",
            "get_sentiment_policy", "get_sentiment_history", "get_industry_valuation",
            "list_trades", "get_trade_stats", "get_trade_diagnosis", "sync_trade_ledger",
            "get_time_stop_report",
            "get_allocation_advice", "suggest_pool_classification",
        ],
    },
    "doctor": {
        "id": "doctor",
        "name": "策略诊断",
        "desc": "回测跑完不是终点，Agent 告诉你为什么亏、怎么改",
        "trigger": "回测结果不理想时（由回测接口投递，调度器消费）",
        "max_steps": 8,
        "timeout": 180,
        "tools": [
            "get_system_clock", "run_backtest", "get_kline", "get_chip_distribution", "get_spot",
            "run_factor_backtest", "evaluate_factor", "list_strategies",
        ],
    },
    "risk": {
        "id": "risk",
        "name": "持仓风险",
        "desc": "组合视角看集中度、行业暴露与止损距离",
        "trigger": "每日 09:05（由 MonitorScheduler 自动调用）",
        "max_steps": 8,
        "timeout": 120,
        "tools": [
            "get_system_clock", "get_market_sentiment", "get_account_status", "get_position_summary", "get_company_info",
            "get_kline", "get_settings", "get_sell_target",
            "get_sentiment_policy", "get_sentiment_history", "get_industry_valuation",
            "get_value_position", "get_trade_stats", "get_trade_diagnosis",
            "get_time_stop_report", "waive_time_stop",
            "get_allocation_advice", "suggest_pool_classification",
        ],
    },
}


def get_scenario(name: str) -> dict:
    return SCENARIOS.get(name) or SCENARIOS["copilot"]


def validate_scenario_tools(registry_names: set[str]) -> dict:
    """一致性自检：场景引用了未注册的工具 / 已注册工具完全不可达，都要报错。

    供测试与运维自检使用，防止「加了工具却忘了接场景」再次发生。
    """
    unknown = {name: sorted(set(cfg["tools"]) - registry_names)
               for name, cfg in SCENARIOS.items() if set(cfg["tools"]) - registry_names}
    reachable: set[str] = set()
    for cfg in SCENARIOS.values():
        reachable.update(cfg["tools"])
    return {
        "ok": not unknown and not (registry_names - reachable),
        "unknown_tools": unknown,
        "unreachable_tools": sorted(registry_names - reachable),
    }
