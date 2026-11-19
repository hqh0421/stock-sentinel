"""Agent 运行时：一个精简的 ReAct 循环。

为什么不用 LangGraph / 现成框架
------------------------------
本场景是「单智能体、线性工具调用、无状态机分支」，用 400 行就能写清楚，
引入图编排框架只会增加部署负担（服务器只有 1.8G 内存）。

对外只暴露一个同步生成器 `run()`，逐个 yield 事件，由路由层转成 SSE。
同步而非异步：LLM 与行情 SDK 都是阻塞调用，async 化要引入额外线程池，
反而更容易出错。FastAPI 的 StreamingResponse 能直接消费同步生成器（内部走线程池）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterator, List, Optional

from ..core.trading_time import is_trading_now, trading_status
from .prompts import build_system_prompt
from .scenarios import get_scenario
from .tools import describe_tools, execute_tool, get_tool

logger = logging.getLogger(__name__)

MAX_JSON_RETRY = 3
OBSERVATION_LIMIT = 2200  # 单条工具结果喂给模型的字符上限，防止上下文爆炸

# ---------------------------------------------------------------------------
# 纪律 preflight：写在 prompts.py 里的「必调」不能只靠模型自觉。
# 模型给出的结论一旦涉及买入/加仓或具体卖出价位，而它没调过对应工具，
# 系统强制补调一次并把结果塞回上下文，让它重新收尾（最多补一轮，不会死循环）。
# ---------------------------------------------------------------------------
BUY_ADVICE_KEYWORDS = ("买入", "加仓", "建仓", "补仓", "开仓", "低吸", "分批买", "进一笔", "可以买", "值得买")
SELL_PRICE_KEYWORDS = ("目标价", "止盈", "止损位", "卖出价", "减仓价", "T1", "T2", "T3")
PREFLIGHT_RULES = (
    ("get_sentiment_policy", BUY_ADVICE_KEYWORDS, "买入/加仓建议前必调情绪仓位政策"),
    ("get_sell_target", SELL_PRICE_KEYWORDS, "给出卖出价位前必调多因子目标价"),
)


def _answer_text(answer: Any) -> str:
    if not isinstance(answer, dict):
        return str(answer or "")
    parts = [str(answer.get("headline") or "")]
    for item in (answer.get("items") or []):
        if isinstance(item, dict):
            parts.extend(str(item.get(k) or "") for k in ("tag", "title", "body", "action"))
        else:
            parts.append(str(item))
    return " ".join(parts)


def missing_preflight_tools(answer: Any, called: set, tool_names: List[str]) -> List[str]:
    """根据答案内容判断还缺哪些必调工具。"""
    text = _answer_text(answer)
    missing: List[str] = []
    for tool, keywords, _reason in PREFLIGHT_RULES:
        if tool in tool_names and tool not in called and any(k in text for k in keywords):
            missing.append(tool)
    return missing


class AgentConfigError(RuntimeError):
    """模型配置缺失（没配 key 等），需要给用户明确提示而不是堆栈。"""


class AgentLLMError(RuntimeError):
    """模型调用失败，带可判定的错误码，便于前端给出针对性提示。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 进程内记录最近一次模型调用的健康状态，供 /api/agent/meta 反馈给前端。
# 这样即使用户配了 Key 但 Key 已失效，界面也能提示「Key 可能无效」而不是显示一切正常。
LAST_LLM_STATE: Dict[str, Any] = {
    "last_error_code": None,
    "last_error": None,
    "last_error_at": None,
    "last_ok_at": None,
}


def _record_llm_error(code: str, message: str) -> None:
    LAST_LLM_STATE["last_error_code"] = code
    LAST_LLM_STATE["last_error"] = message
    LAST_LLM_STATE["last_error_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def _classify_llm_error(exc: Exception) -> AgentLLMError:
    """把各家 SDK 五花八门的异常翻译成用户能看懂的一句话。"""
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "401" in text or "authentication" in low or "invalid api key" in low or "unauthorized" in low or "invalid_api_key" in low:
        return AgentLLMError("auth", "模型 API Key 无效或已过期。请到「系统设置 → 模型」更新 Key 后重试。")
    if "403" in text or "permission" in low:
        return AgentLLMError("forbidden", "模型接口拒绝访问（403）。请确认 Key 有该模型权限，或检查 Base URL 是否正确。")
    if "429" in text or "rate limit" in low or "quota" in low or "insufficient" in low:
        return AgentLLMError("rate_limit", "模型调用频率或额度超限（429）。请稍后重试，或更换模型。")
    if "timeout" in low or "timed out" in low:
        return AgentLLMError("timeout", "模型响应超时。网络不稳或模型服务繁忙，请稍后重试。")
    if "connection" in low or "network" in low or "resolve" in low or "ssl" in low:
        return AgentLLMError("network", "无法连接模型服务。请检查网络连通性和 Base URL 配置。")
    if "model" in low and ("not found" in low or "not exist" in low or "invalid" in low):
        return AgentLLMError("model_not_found", "模型名不存在或不可用。请到「系统设置 → 模型」核对模型名称。")
    return AgentLLMError("llm", f"模型调用失败：{text[:200]}")


# ---------------------------------------------------------------------------
# LLM 获取
# ---------------------------------------------------------------------------

def _build_llm():
    from ..config_loader import load_config
    from ..model_factory import get_llm

    cfg = load_config()
    if cfg is None:
        raise AgentConfigError("未读取到模型配置（config/models.json）")
    if not str(getattr(cfg, "api_key", "") or "").strip():
        raise AgentConfigError("尚未配置模型 API Key，请到「系统设置 → 模型」中填写后再使用 AI 功能")

    # 分析类任务要稳定，温度压低；不同 pydantic 版本方法名不同，逐个尝试
    for method in ("model_copy", "copy"):
        try:
            cfg = getattr(cfg, method)(update={"temperature": 0.2})
            break
        except Exception:  # noqa: BLE001
            continue

    return get_llm(cfg), cfg


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text")))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def _invoke(llm, messages, timeout: int) -> str:
    """带超时的单次调用。超时后放弃本次，让上层决定怎么收尾。"""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(llm.invoke, messages)
        try:
            result = future.result(timeout=timeout)
        except TimeoutError:
            _record_llm_error("timeout", f"模型 {timeout} 秒内未返回")
            raise AgentLLMError("timeout", f"模型 {timeout} 秒内未返回，已中止。") from None
        except Exception as exc:  # noqa: BLE001
            err = _classify_llm_error(exc)
            _record_llm_error(err.code, err.message)
            raise err from exc
    LAST_LLM_STATE["last_ok_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return _content_to_text(getattr(result, "content", result))


# ---------------------------------------------------------------------------
# JSON 解析（模型输出经常带 markdown 围栏或前后废话）
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text or not text.strip():
        return None
    raw = text.strip()

    fences = re.findall(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fences:
        raw = fences[0].strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:  # noqa: BLE001
        pass

    # 扫描最外层大括号，处理前后有解释文字的情况
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start:i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:  # noqa: BLE001
                        pass
                    break
        start = raw.find("{", start + 1)
    return None


def _normalize_answer(answer: Any) -> Dict[str, Any]:
    """把模型给的 answer 归一成 {headline, items:[{tag,title,body,action}]}。"""
    if isinstance(answer, str):
        text = answer.strip()
        if not text:
            return {"headline": "（模型没有给出结论）", "items": []}
        return {"headline": text, "items": []}

    if not isinstance(answer, dict):
        return {"headline": str(answer)[:200], "items": []}

    headline = str(answer.get("headline") or answer.get("summary") or answer.get("conclusion") or "").strip()
    raw_items = answer.get("items") or answer.get("points") or answer.get("details") or []
    items: List[Dict[str, str]] = []

    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if isinstance(raw_items, list):
        for it in raw_items[:6]:
            if isinstance(it, str):
                items.append({"tag": "", "title": "", "body": it, "action": ""})
            elif isinstance(it, dict):
                items.append({
                    "tag": str(it.get("tag") or it.get("type") or "").strip(),
                    "title": str(it.get("title") or it.get("name") or "").strip(),
                    "body": str(it.get("body") or it.get("detail") or it.get("desc") or "").strip(),
                    "action": str(it.get("action") or it.get("suggestion") or "").strip(),
                })

    if not headline:
        headline = items[0]["title"] if items and items[0]["title"] else "分析完成"
    return {"headline": headline, "items": items}


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------

class AgentRuntime:
    """单智能体 ReAct 运行时。"""

    def run(
        self,
        question: str,
        scenario: str = "copilot",
        context: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Dict[str, Any]]:
        started = time.monotonic()
        cfg = get_scenario(scenario)
        tool_names: List[str] = list(cfg["tools"])
        max_steps: int = int(cfg["max_steps"])
        timeout: int = int(cfg["timeout"])

        yield {"type": "start", "scenario": scenario, "scenario_name": cfg["name"], "question": question}

        # ---- 模型准备 ----
        try:
            llm, model_cfg = _build_llm()
        except AgentConfigError as exc:
            yield {"type": "error", "code": "config", "message": str(exc)}
            yield {"type": "done", "steps": 0, "tools": 0, "elapsed": 0, "ok": False}
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("初始化模型失败")
            yield {"type": "error", "code": "llm_init", "message": f"初始化模型失败：{exc}"}
            yield {"type": "done", "steps": 0, "tools": 0, "elapsed": 0, "ok": False}
            return

        # ---- 组装消息 ----
        system_prompt = build_system_prompt(scenario, describe_tools(tool_names))
        user_parts = [f"当前系统时间：{time.strftime('%Y-%m-%d %H:%M:%S')}，交易状态：{trading_status()}。"]
        if context:
            ctx_text = context.get("prompt") if isinstance(context, dict) else None
            if ctx_text:
                user_parts.append(str(ctx_text))
        user_parts.append(question)
        user_content = "\n\n".join(p for p in user_parts if p)

        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]

        step = 0
        tool_count = 0
        json_fail = 0
        no_action = 0
        observations: List[Dict[str, str]] = []
        answer: Optional[Dict[str, Any]] = None
        last_error: Optional[str] = None
        last_error_code: Optional[str] = None
        preflight_done = False

        while step < max_steps:
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                last_error = f"超过 {timeout} 秒仍未完成，已中止"
                last_error_code = "timeout"
                break

            step += 1
            try:
                raw = _invoke(llm, messages, timeout=max(20, int(timeout - elapsed)))
            except AgentLLMError as exc:
                logger.warning("模型调用失败 [%s] %s", exc.code, exc.message)
                last_error = exc.message
                last_error_code = exc.code
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception("模型调用出现未预期异常")
                last_error = f"{type(exc).__name__}: {exc}"
                last_error_code = "llm"
                break

            messages.append(AIMessage(content=raw))
            payload = _extract_json(raw)

            if payload is None:
                json_fail += 1
                if json_fail >= MAX_JSON_RETRY:
                    last_error = "模型连续多次未按 JSON 格式输出"
                    break
                messages.append(HumanMessage(content=(
                    "你的上一条输出无法解析为 JSON。请严格只输出一个 JSON 对象，"
                    "形如 {\"thought\": \"...\", \"action\": \"工具名\", \"args\": {}}，不要任何额外文字。"
                )))
                yield {"type": "retry", "step": step, "reason": "json_parse"}
                continue

            thought = str(payload.get("thought") or "").strip()
            action = str(payload.get("action") or "").strip()

            if thought:
                yield {"type": "thought", "step": step, "text": thought}

            if action == "final" or (not action and payload.get("answer")):
                candidate = _normalize_answer(payload.get("answer"))
                missing = missing_preflight_tools(
                    candidate, {o["name"] for o in observations}, tool_names
                ) if not preflight_done else []
                if missing:
                    # 结论里写了买入/卖出价位，却没按纪律先取证：强制补调，再让它重写结论。
                    preflight_done = True
                    for tool in missing:
                        tool_count += 1
                        yield {"type": "tool_start", "step": step, "name": tool,
                               "args": {}, "preflight": True}
                        result = execute_tool(tool, {})
                        observation = str(result.get("text") or "")
                        if len(observation) > OBSERVATION_LIMIT:
                            observation = observation[:OBSERVATION_LIMIT] + "…（结果过长已截断）"
                        observations.append({"name": tool, "text": observation})
                        yield {"type": "tool_end", "step": step, "name": tool,
                               "ok": bool(result.get("ok")), "ms": 0,
                               "source": result.get("source") or "",
                               "summary": observation, "preflight": True}
                        messages.append(HumanMessage(content=(
                            f"[系统强制补调 {tool} 的执行结果]\n{observation}"
                        )))
                    yield {"type": "preflight", "tools": missing,
                           "text": "结论涉及买入/加仓或卖出价位，已强制补齐纪律工具数据"}
                    messages.append(HumanMessage(content=(
                        "[系统强制补调] 你刚才的结论涉及买入/加仓建议或具体卖出价位，"
                        "但按硬规则必须先取证。以上是你未调用的工具结果，"
                        "请据此重新输出最终结论（action=final），并说明这些纪律数据如何影响结论；"
                        "若与情绪政策或目标价冲突，必须修改原结论。不要再调用其他工具。"
                    )))
                    continue
                answer = candidate
                break

            if not action:
                no_action += 1
                if no_action >= 2:
                    last_error = "模型既没有调用工具也没有给出答案"
                    last_error_code = "no_action"
                    break
                messages.append(HumanMessage(content=(
                    "你的输出缺少 action 字段。请二选一，只输出一个 JSON 对象：\n"
                    "调工具：{\"thought\": \"...\", \"action\": \"工具名\", \"args\": {}}\n"
                    "给结论：{\"thought\": \"...\", \"action\": \"final\", \"answer\": {\"headline\": \"...\", \"items\": []}}"
                )))
                yield {"type": "retry", "step": step, "reason": "no_action"}
                continue

            # 工具是否属于本场景
            if action not in tool_names:
                messages.append(HumanMessage(content=(
                    f"工具 {action} 不在本次可用清单内。可用工具：" + "、".join(tool_names) +
                    "。请从这些里选，或直接 action=final 回答。"
                )))
                yield {"type": "retry", "step": step, "reason": "unknown_tool", "detail": action}
                continue

            args = payload.get("args")
            if not isinstance(args, dict):
                args = {}

            tool_count += 1
            yield {"type": "tool_start", "step": step, "name": action, "args": args}

            t0 = time.monotonic()
            result = execute_tool(action, args)
            cost_ms = int((time.monotonic() - t0) * 1000)

            observation = str(result.get("text") or "")
            if len(observation) > OBSERVATION_LIMIT:
                observation = observation[:OBSERVATION_LIMIT] + "…（结果过长已截断）"
            observations.append({"name": action, "text": observation})

            yield {
                "type": "tool_end",
                "step": step,
                "name": action,
                "ok": bool(result.get("ok")),
                "ms": cost_ms,
                "source": result.get("source") or "",
                "summary": observation,
                "data": result.get("data") if isinstance(result.get("data"), (dict, list)) else None,
            }

            messages.append(HumanMessage(content=(
                f"[工具 {action} 执行结果]\n{observation}\n\n"
                "请基于这个结果判断：还需要别的数据就继续调用工具；信息够了就输出 "
                "{\"thought\": \"...\", \"action\": \"final\", \"answer\": {...}}。"
            )))

        # ---- 仍未收尾（步数用尽 / 格式反复失败）：强制总结一次 ----
        if answer is None and observations:
            hint = ("已达到本次分析的最大步数。" if last_error is None
                    else f"上一轮输出有问题（{last_error}）。")
            try:
                messages.append(HumanMessage(content=(
                    hint + "请立刻基于已取到的数据输出最终结论，"
                    "只输出 {\"thought\": \"...\", \"action\": \"final\", \"answer\": {\"headline\": \"...\", \"items\": [...]}}，不要再调用任何工具。"
                )))
                raw = _invoke(llm, messages, timeout=60)
                payload = _extract_json(raw)
                if payload and payload.get("answer"):
                    answer = _normalize_answer(payload.get("answer"))
                elif payload and str(payload.get("thought") or "").strip():
                    answer = _normalize_answer(str(payload.get("thought")).strip()[:600])
                elif payload is None and raw.strip():
                    answer = _normalize_answer(raw.strip()[:600])
            except AgentLLMError as exc:
                logger.warning("收尾调用失败 [%s] %s", exc.code, exc.message)
                last_error = exc.message
                last_error_code = exc.code
            except Exception as exc:  # noqa: BLE001
                last_error = f"收尾失败：{exc}"
                last_error_code = "finalize"

        # ---- 模型确实收不了尾：用已取到的数据兜一个确定性结论，别让用户两手空空 ----
        if answer is None and observations:
            logger.warning("agent_fallback_summary tools=%s reason=%s", len(observations), last_error)
            answer = {
                "headline": "本轮没能完成归纳（模型输出异常），先把已经取到的数据原样给你",
                "items": [
                    {"tag": "数据", "title": o["name"], "body": o["text"][:240], "action": ""}
                    for o in observations[:6]
                ],
            }
            last_error_code = None
            last_error = None

        if answer is not None:
            yield {"type": "answer", "answer": answer}
            yield {
                "type": "done",
                "steps": step,
                "tools": tool_count,
                "elapsed": round(time.monotonic() - started, 1),
                "ok": True,
                "model": getattr(model_cfg, "model", ""),
            }
        else:
            yield {"type": "error", "code": last_error_code or "agent_failed",
                   "message": last_error or "未能得出结论"}
            yield {
                "type": "done",
                "steps": step,
                "tools": tool_count,
                "elapsed": round(time.monotonic() - started, 1),
                "ok": False,
            }


agent_runtime = AgentRuntime()


def sse_pack(event: Dict[str, Any]) -> str:
    """把事件包成 SSE 帧。data 里可能含换行，json.dumps 后仍是单行，安全。"""
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
