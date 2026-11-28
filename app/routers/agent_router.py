"""Agent API 路由。

端点
----
- ``POST /api/agent/chat``  : SSE 流式对话/分析（核心入口）
- ``POST /api/agent/run``   : 非流式，一次性返回完整结果（供自动化场景调用）
- ``GET  /api/agent/meta``  : 场景清单 + 工具目录 + 模型配置状态
- ``POST /api/agent/review``: 对一条监控信号做复核，返回结构化判定

设计说明：所有场景（顾问/复核/复盘/诊断/风险）共用同一套运行时，
只是 prompt 与工具子集不同。这样新增场景的边际成本接近零。
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from ..agent import SCENARIOS, TOOL_SPECS, agent_runtime, sse_pack
from ..agent.runtime import LAST_LLM_STATE
from ..config_loader import load_config
from ..core.context import resolve_user_id
from ..core.trading_time import trading_status
from ..db import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _answer_to_text(answer: Optional[Dict[str, Any]]) -> str:
    """把结构化 answer 拍平成可读文本，用于历史列表的摘要与降级展示。"""
    if not isinstance(answer, dict):
        return ""
    lines: List[str] = []
    headline = str(answer.get("headline") or "").strip()
    if headline:
        lines.append(headline)
    for item in answer.get("items") or []:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(f"[{tag}] {text}" if tag else text)
    return "\n".join(lines)[:8000]


def _persist_exchange(
    user_id: int,
    conversation_id: str,
    question: str,
    scenario: str,
    answer: Optional[Dict[str, Any]],
    stats: Optional[Dict[str, Any]],
    error: Optional[str],
) -> None:
    """把一问一答落库。失败只记日志，绝不影响已经推给用户的回答。"""
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        store.add_agent_message(
            user_id=int(user_id),
            conversation_id=conversation_id,
            role="user",
            content=question,
            meta={"scenario": scenario},
            created_at=now,
        )
        store.add_agent_message(
            user_id=int(user_id),
            conversation_id=conversation_id,
            role="assistant",
            content=_answer_to_text(answer) or (error or ""),
            meta={
                "scenario": scenario,
                "answer": answer,
                "stats": stats or {},
                "error": error or "",
            },
            created_at=now,
        )
        store.purge_agent_messages(int(user_id), keep=2000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agent 对话落库失败 error=%r", exc)


def _model_status() -> Dict[str, Any]:
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        return {"configured": False, "error": str(exc)}
    key = str(getattr(cfg, "api_key", "") or "").strip()
    return {
        "configured": bool(key),
        "provider": getattr(cfg, "provider", ""),
        "model": getattr(cfg, "model", ""),
        "base_url": getattr(cfg, "base_url", ""),
        "key_preview": (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已配置" if key else ""),
    }


@router.get("/meta")
def agent_meta() -> Dict[str, Any]:
    """前端初始化用：场景、工具目录、模型状态一次拿全。"""
    tools = [
        {
            "name": t.name,
            "group": t.group,
            "desc": t.desc,
            "params": t.params,
            "risk": t.risk,
        }
        for t in TOOL_SPECS
    ]
    scenarios = [
        {
            "id": s["id"],
            "name": s["name"],
            "desc": s["desc"],
            "trigger": s["trigger"],
            "max_steps": s["max_steps"],
            "timeout": s["timeout"],
            "tools": s["tools"],
        }
        for s in SCENARIOS.values()
    ]
    return {
        "ok": True,
        "model": _model_status(),
        "llm_state": dict(LAST_LLM_STATE),
        "trading_status": trading_status(),
        "scenarios": scenarios,
        "tools": tools,
        "groups": sorted({t.group for t in TOOL_SPECS}),
    }


@router.get("/portfolio")
def agent_portfolio() -> Dict[str, Any]:
    """组合风险基础数据（纯计算，不依赖模型）。

    风险页可以先用这份真实数据渲染，再让 Agent 补上解释与建议。
    这样即使模型不可用，用户也能看到集中度、行业暴露、止损距离。
    """
    from ..agent.tools import execute_tool

    result = execute_tool("get_position_summary", {})
    return {
        "ok": bool(result.get("ok")),
        "text": result.get("text", ""),
        "data": result.get("data"),
        "source": result.get("source", ""),
    }


@router.get("/sell_plan")
def agent_sell_plan(code: str = "", style: str = "") -> Dict[str, Any]:
    """卖出目标价基准数据（纯计算，不依赖模型）。

    前端先用这份数据渲染三档目标价与止损位，再让 Agent 解释"能不能到、怎么分批"。
    这样模型不可用时用户也能看到可执行的价位。
    """
    from ..agent.tools import execute_tool

    args: Dict[str, Any] = {}
    if code:
        args["code"] = code
    if style:
        args["style"] = style
    result = execute_tool("get_sell_target", args)
    return {
        "ok": bool(result.get("ok")),
        "text": result.get("text", ""),
        "data": result.get("data"),
        "source": result.get("source", ""),
    }


@router.post("/chat")
def agent_chat(payload: Dict[str, Any] = Body(default_factory=dict)):
    """SSE 流式：前端逐条消费 plan/thought/tool/answer 事件。"""
    question = str(payload.get("question") or "").strip()
    scenario = str(payload.get("scenario") or "copilot").strip()
    context = payload.get("context")
    conversation_id = str(payload.get("conversation_id") or "").strip() or f"c{uuid.uuid4().hex[:12]}"

    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")
    if scenario not in SCENARIOS:
        raise HTTPException(status_code=400, detail=f"未知场景 {scenario}")

    # 在端点里取用户：SSE 的 body 是在 response 返回之后才迭代的，
    # 那时中间件已经 reset 了 contextvar，闭包里必须提前捕获。
    try:
        uid: Optional[int] = resolve_user_id()
    except Exception:  # noqa: BLE001
        uid = None

    def event_stream():
        answer: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        stats: Dict[str, Any] = {}
        # 先告知前端会话 id（新建会话时前端要拿它做后续历史对齐）
        yield sse_pack({"type": "meta", "conversation_id": conversation_id, "scenario": scenario})
        try:
            for event in agent_runtime.run(question, scenario=scenario, context=context):
                etype = event.get("type")
                if etype == "answer":
                    answer = event.get("answer")
                elif etype == "error":
                    error = event.get("message")
                elif etype == "done":
                    stats = {k: event.get(k) for k in ("steps", "tools", "elapsed", "ok")}
                yield sse_pack(event)
        except GeneratorExit:  # 客户端断开
            logger.info("agent SSE 客户端断开")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent SSE 异常")
            error = f"{type(exc).__name__}: {exc}"
            yield sse_pack({"type": "error", "code": "stream", "message": error})
            yield sse_pack({"type": "done", "ok": False, "steps": 0, "tools": 0, "elapsed": 0})
        finally:
            if uid:
                _persist_exchange(uid, conversation_id, question, scenario, answer, stats, error)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/run")
def agent_run(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """非流式版本：把事件收集齐再返回，方便脚本或自动化调用。"""
    question = str(payload.get("question") or "").strip()
    scenario = str(payload.get("scenario") or "copilot").strip()
    context = payload.get("context")
    conversation_id = str(payload.get("conversation_id") or "").strip() or f"c{uuid.uuid4().hex[:12]}"
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")
    if scenario not in SCENARIOS:
        raise HTTPException(status_code=400, detail=f"未知场景 {scenario}")

    events: List[Dict[str, Any]] = []
    answer: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    stats: Dict[str, Any] = {}

    for event in agent_runtime.run(question, scenario=scenario, context=context):
        events.append(event)
        etype = event.get("type")
        if etype == "answer":
            answer = event.get("answer")
        elif etype == "error":
            error = event.get("message")
        elif etype == "done":
            stats = {k: event.get(k) for k in ("steps", "tools", "elapsed", "ok")}

    try:
        _persist_exchange(resolve_user_id(), conversation_id, question, scenario, answer, stats, error)
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": bool(answer) and not error,
        "scenario": scenario,
        "conversation_id": conversation_id,
        "answer": answer,
        "error": error,
        "stats": stats,
        "events": events,
    }


# ------------------------------------------------------------------ 历史

@router.get("/history/conversations")
def agent_conversations(limit: int = 30) -> Dict[str, Any]:
    """会话列表：标题取该会话第一个问题，便于找回「上周问过的那只票」。"""
    uid = resolve_user_id()
    return {"data": store.list_agent_conversations(uid, limit=max(1, min(200, int(limit))))}


@router.get("/history")
def agent_history(conversation_id: str = "", limit: int = 60) -> Dict[str, Any]:
    uid = resolve_user_id()
    return {
        "data": store.list_agent_messages(
            uid, conversation_id=conversation_id, limit=max(1, min(500, int(limit)))
        )
    }


@router.post("/history/delete")
def agent_history_delete(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    uid = resolve_user_id()
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id 不能为空")
    removed = store.delete_agent_conversation(uid, conversation_id)
    return {"ok": True, "removed": removed}


@router.post("/review")
def agent_review(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """复核一条监控信号。

    入参：code / name / signal（信号描述）/ price / pct_chg
    返回：verdict（pass/downgrade/block）+ 理由 + 证据链
    """
    code = str(payload.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code 不能为空")

    name = str(payload.get("name") or "").strip()
    signal_text = str(payload.get("signal") or "").strip() or "触发了技术指标信号"
    price = payload.get("price")
    pct = payload.get("pct_chg")

    ctx_lines = [
        f"待复核信号：{code} {name}",
        f"信号描述：{signal_text}",
    ]
    if price is not None:
        ctx_lines.append(f"触发时价格：{price}")
    if pct is not None:
        ctx_lines.append(f"当日涨跌幅：{pct}%")
    context = {"prompt": "\n".join(ctx_lines)}

    events: List[Dict[str, Any]] = []
    answer: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    for event in agent_runtime.run(
        "请复核这条信号，给出通过/降级/拦截的判定和证据。",
        scenario="review",
        context=context,
    ):
        events.append(event)
        if event.get("type") == "answer":
            answer = event.get("answer")
        elif event.get("type") == "error":
            error = event.get("message")

    # 把 answer 的第一条 tag 映射成机器可读的判定
    verdict = "unknown"
    if answer and answer.get("items"):
        first_tag = str(answer["items"][0].get("tag") or "")
        for key, label in (("pass", "通过"), ("downgrade", "降级"), ("block", "拦截")):
            if label in first_tag:
                verdict = key
                break
    evidence = [it for it in (answer or {}).get("items", [])[1:]]
    tools_used = [
        {"name": e.get("name"), "ms": e.get("ms"), "ok": e.get("ok"), "source": e.get("source")}
        for e in events if e.get("type") == "tool_end"
    ]

    return {
        "ok": bool(answer) and not error,
        "code": code,
        "verdict": verdict,
        "headline": (answer or {}).get("headline", ""),
        "evidence": evidence,
        "tools": tools_used,
        "error": error,
        "raw_events": events if payload.get("debug") else None,
    }
