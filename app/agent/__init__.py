"""Agent 子包：轻量 ReAct 智能体层。

对外暴露：
- ``agent_runtime``：运行时单例
- ``TOOL_SPECS`` / ``execute_tool``：工具注册表
- ``SCENARIOS``：场景定义
"""

from .runtime import AgentConfigError, agent_runtime, sse_pack
from .scenarios import SCENARIOS, get_scenario
from .tools import TOOL_MAP, TOOL_SPECS, describe_tools, execute_tool

__all__ = [
    "agent_runtime",
    "sse_pack",
    "AgentConfigError",
    "SCENARIOS",
    "get_scenario",
    "TOOL_SPECS",
    "TOOL_MAP",
    "execute_tool",
    "describe_tools",
]
