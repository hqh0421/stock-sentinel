/* =========================================================================
   Agent 前端逻辑：分析顾问 / 信号复核 / 盘后复盘 / 策略诊断 / 持仓风险 / 编排
   依赖 app.js 的全局工具（escapeHTML、qs、fetchJSON、showToast），在其后加载。
   ========================================================================= */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const esc = (v) => (typeof window.escapeHTML === "function" ? window.escapeHTML(v) : String(v == null ? "" : v));
  const fmt = (v, d = 2) => (Number.isFinite(Number(v)) ? Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d }) : "-");
  const pct = (v, d = 2) => (Number.isFinite(Number(v)) ? (Number(v) > 0 ? "+" : "") + Number(v).toFixed(d) + "%" : "-");
  const cls = (v) => (Number(v) > 0 ? "text-up" : Number(v) < 0 ? "text-down" : "text-ink-muted");
  const mdLite = (v) => esc(v).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br/>");

  function toast(msg, type) {
    if (typeof window.showToast === "function") { window.showToast(type === "error" ? "出错了" : "提示", msg, type || "info"); }
    else { console.log(msg); }
  }

  // 统一请求层见 api.js（401 由 app.js 的 fetch 拦截器统一跳转）
  const api = window.requestJSON;

  let META = null;
  let metaPromise = null;

  function ensureMeta(force) {
    if (META && !force) return Promise.resolve(META);
    if (metaPromise && !force) return metaPromise;
    metaPromise = api("/api/agent/meta").then((d) => { META = d; return d; }).catch((e) => { metaPromise = null; throw e; });
    return metaPromise;
  }

  /* ---------------- SSE 消费 ---------------- */
  async function streamAgent(body, onEvent, signal) {
    const resp = await fetch("/api/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (resp.status === 401) throw new Error("登录已失效，请重新登录");
    if (!resp.ok) {
      const t = await resp.text().catch(() => "");
      throw new Error("HTTP " + resp.status + " " + t.slice(0, 120));
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) { /* 忽略坏帧 */ }
      }
    }
  }

  /* ---------------- 共用渲染 ---------------- */
  const TAG_TONE = [
    { k: ["高优先", "超阈值", "拦截", "必须", "严重", "主因"], c: "bg-up-soft text-up" },
    { k: ["观察", "降级", "需关注", "次因", "注意"], c: "bg-warn-soft text-warn" },
    { k: ["通过", "正常", "持有", "建议", "数据"], c: "bg-down-soft text-down" },
  ];
  function tagCls(tag) {
    const t = String(tag || "");
    for (const g of TAG_TONE) { if (g.k.some((k) => t.includes(k))) return g.c; }
    return "bg-ai-50 text-ai-600";
  }

  function answerCard(ans) {
    const items = (ans && ans.items) || [];
    return `<div class="rounded-2xl border border-ai-100 bg-white p-4">
      <div class="text-[13.5px] font-bold mb-3">${mdLite(ans && ans.headline || "分析完成")}</div>
      <div class="space-y-2.5">
        ${items.map((b) => `
          <div class="rounded-xl border border-line p-3">
            <div class="flex items-center gap-2 mb-1.5 flex-wrap">
              ${b.tag ? `<span class="text-[10px] px-1.5 py-0.5 rounded font-bold ${tagCls(b.tag)}">${esc(b.tag)}</span>` : ""}
              ${b.title ? `<span class="text-[12.5px] font-semibold">${mdLite(b.title)}</span>` : ""}
            </div>
            ${b.body ? `<div class="text-[11.5px] text-ink-muted leading-relaxed">${mdLite(b.body)}</div>` : ""}
            ${b.action ? `<div class="text-[11.5px] text-ink mt-1.5 pl-2.5 border-l-2 border-ai-200 leading-relaxed">${mdLite(b.action)}</div>` : ""}
          </div>`).join("") || `<div class="text-[12px] text-ink-faint">（模型未给出分条结论）</div>`}
      </div>
    </div>`;
  }

  function toolCard(ev) {
    const okDot = ev.ok ? "bg-down" : "bg-up";
    return `<div class="rounded-xl border border-line bg-white p-2.5 animate-fadein">
      <div class="flex items-center gap-2">
        <span class="w-1.5 h-1.5 rounded-full ${okDot} shrink-0"></span>
        <span class="font-mono text-[11.5px] text-ai-600">${esc(ev.name)}</span>
        <span class="ml-auto text-[10px] text-ink-faint shrink-0">${ev.ms}ms</span>
      </div>
      <div class="text-[11.5px] text-ink-muted mt-1.5 leading-relaxed pl-3.5 whitespace-pre-wrap">${esc((ev.summary || "").slice(0, 600))}</div>
      ${ev.source ? `<div class="text-[10px] text-ink-faint mt-1 pl-3.5">来源：${esc(ev.source)}</div>` : ""}
    </div>`;
  }

  function thoughtCard(text, kind) {
    const isRetry = kind === "retry";
    return `<div class="rounded-xl border ${isRetry ? "border-line bg-ink/[0.02]" : "border-[#f3dfae] bg-warn-soft/50"} px-3 py-2 text-[11.5px] ${isRetry ? "text-ink-muted" : "text-[#8a6100]"} leading-relaxed animate-fadein">
      <span class="font-semibold">${isRetry ? "调整" : "推理"} · </span>${mdLite(text)}
    </div>`;
  }

  /* =========================================================================
     1. 分析顾问
     ========================================================================= */
  let chatBusy = false;
  let chatAbort = null;

  /* ---------------- 会话（落库，刷新/换设备都能接着聊） ---------------- */
  const CONV_KEY = "agent_conv_id";
  let conversationId = "";
  try { conversationId = localStorage.getItem(CONV_KEY) || ""; } catch (e) { conversationId = ""; }

  function setConversationId(id) {
    conversationId = id || "";
    try {
      if (conversationId) localStorage.setItem(CONV_KEY, conversationId);
      else localStorage.removeItem(CONV_KEY);
    } catch (e) { /* 隐私模式下 localStorage 不可用，不影响对话 */ }
  }

  function chatClearFlow() {
    const flow = $("#agentChatFlow");
    if (flow) flow.innerHTML = "";
  }

  async function loadConversations(preferId) {
    const sel = $("#agentConvSelect");
    if (!sel) return;
    try {
      const data = await api("/api/agent/history/conversations?limit=30");
      const rows = (data && data.data) || [];
      sel.innerHTML = `<option value="">（新对话）</option>` + rows.map((r) => {
        const title = String(r.title || "未命名会话").replace(/\s+/g, " ").slice(0, 26);
        const when = String(r.updated_at || "").slice(5, 16);
        return `<option value="${esc(r.conversation_id)}">${esc(when)} · ${esc(title)}（${r.message_count || 0}）</option>`;
      }).join("");
      const want = preferId != null ? preferId : conversationId;
      sel.value = want && rows.some((r) => r.conversation_id === want) ? want : "";
    } catch (e) { /* 历史不可用不阻塞对话 */ }
  }

  async function loadConversation(id) {
    setConversationId(id);
    chatClearFlow();
    traceReset();
    if (!id) return;
    try {
      const data = await api(`/api/agent/history?conversation_id=${encodeURIComponent(id)}&limit=80`);
      const rows = (data && data.data) || [];
      rows.forEach((m) => {
        if (m.role === "user") { chatUser(m.content); return; }
        const ans = m.meta && m.meta.answer;
        const body = chatAgentShell();
        const wrap = document.createElement("div");
        if (ans && (ans.headline || (ans.items || []).length)) {
          wrap.innerHTML = answerCard(ans);
        } else {
          wrap.innerHTML = `<div class="rounded-2xl border border-line bg-white p-3 text-[12.5px] text-ink-muted whitespace-pre-wrap">${esc(m.content || "（空）")}</div>`;
        }
        body.appendChild(wrap.firstElementChild);
      });
      const sc = $("#agentChatScroll");
      if (sc) sc.scrollTop = sc.scrollHeight;
    } catch (e) {
      toast("历史加载失败：" + (e.message || e), "error");
    }
  }

  async function deleteConversation() {
    const id = conversationId || ($("#agentConvSelect") || {}).value || "";
    if (!id) { toast("当前没有可删除的历史会话", "error"); return; }
    if (!confirm("删除这个会话的全部问答记录？删除后不可恢复。")) return;
    try {
      await api("/api/agent/history/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: id }),
      });
      if (id === conversationId) { setConversationId(""); chatClearFlow(); }
      await loadConversations("");
      toast("已删除", "该会话记录已清除", "info");
    } catch (e) {
      toast("删除失败：" + (e.message || e), "error");
    }
  }

  function chatSetState(text, tone) {
    const el = $("#agentRunState");
    if (!el) return;
    el.textContent = text;
    el.className = "text-[11px] px-2 py-0.5 rounded-md " + (tone || "bg-ink-faint/10 text-ink-muted");
  }

  function chatAppend(node) {
    const flow = $("#agentChatFlow");
    if (!flow) return;
    flow.appendChild(node);
    const scroller = $("#agentChatScroll");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  function chatUser(text) {
    const d = document.createElement("div");
    d.className = "flex justify-end animate-fadein";
    d.innerHTML = `<div class="max-w-[80%] rounded-2xl rounded-tr-md bg-brand-500 text-white px-4 py-2.5 text-[13px] leading-relaxed whitespace-pre-wrap">${esc(text)}</div>`;
    chatAppend(d);
  }

  function chatAgentShell() {
    const d = document.createElement("div");
    d.className = "flex gap-3 animate-fadein";
    d.innerHTML = `<div class="w-8 h-8 rounded-xl bg-ai-500 text-white grid place-items-center shrink-0">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 3a4 4 0 0 1 4 4v1a4 4 0 0 1 0 8v1a4 4 0 0 1-8 0v-1a4 4 0 0 1 0-8V7a4 4 0 0 1 4-4Z"/></svg>
      </div>
      <div class="flex-1 min-w-0 space-y-2">
        <div class="text-[12.5px] font-bold">分析顾问</div>
        <div class="body space-y-2"></div>
      </div>`;
    chatAppend(d);
    return d.querySelector(".body");
  }

  function traceAdd(n, title, sub, tone) {
    const box = $("#agentTrace");
    if (!box) return;
    if (box.querySelector(".text-ink-faint.text-center")) box.innerHTML = "";
    const col = tone === "plan" ? "bg-brand-50 text-brand-600"
      : tone === "think" ? "bg-warn-soft text-warn"
      : tone === "answer" ? "bg-ai-500 text-white"
      : "bg-ai-50 text-ai-600";
    const el = document.createElement("div");
    el.className = "flex gap-2 items-start animate-fadein";
    el.innerHTML = `<div class="w-6 h-6 rounded-lg ${col} grid place-items-center text-[10px] font-bold shrink-0">${n}</div>
      <div class="flex-1 min-w-0">
        <div class="text-[11.5px] font-medium leading-tight break-words">${esc(title)}</div>
        <div class="text-[10px] text-ink-faint mt-0.5 leading-relaxed break-words">${esc(sub)}</div>
      </div>`;
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }

  function traceReset() {
    const box = $("#agentTrace");
    if (box) box.innerHTML = `<div class="text-[11px] text-ink-faint text-center py-6 leading-relaxed">运行后这里会实时显示<br/>规划 → 调工具 → 推理 → 回答</div>`;
    $("#agentMSteps").textContent = "—";
    $("#agentMTools").textContent = "—";
    $("#agentMTime").textContent = "—";
    $("#agentSources").innerHTML = "";
  }

  async function agentAsk(question) {
    if (chatBusy) return;
    const q = String(question || "").trim();
    if (!q) { toast("请先输入问题", "error"); return; }

    chatBusy = true;
    chatAbort = new AbortController();
    $("#agentSendBtn").disabled = true;
    $("#agentSendBtn").classList.add("opacity-60");
    $("#agentStopBtn").classList.remove("hidden");
    chatSetState("运行中", "bg-ai-50 text-ai-600");
    traceReset();

    chatUser(q);
    const body = chatAgentShell();
    const t0 = Date.now();
    let steps = 0, tools = 0;
    // 记账类写工具执行过 → 对话结束后要刷新页面上的持仓/现金/流水，否则页面还是旧数字
    let wroteAccount = false;
    const sources = new Set();

    const push = (html) => {
      const wrap = document.createElement("div");
      wrap.innerHTML = html;
      body.appendChild(wrap.firstElementChild);
      const sc = $("#agentChatScroll");
      if (sc) sc.scrollTop = sc.scrollHeight;
    };

    try {
      await streamAgent({ question: q, scenario: "copilot", conversation_id: conversationId || undefined }, (ev) => {
        switch (ev.type) {
          case "meta":
            // 新会话时后端会生成 id，这里接住，后续提问就落在同一会话里
            if (ev.conversation_id) setConversationId(ev.conversation_id);
            break;
          case "start":
            push(`<div class="rounded-xl bg-brand-50 border border-brand-100 px-3 py-2 text-[11.5px] text-brand-700 leading-relaxed">已拆解任务，开始逐个取数。通常需要 4-7 次工具调用。</div>`);
            steps += 1;
            traceAdd(steps, "PLAN 规划", "拆解任务与取数顺序", "plan");
            break;
          case "thought":
            steps = Math.max(steps + 1, ev.step || steps);
            push(thoughtCard(ev.text, "thought"));
            traceAdd(ev.step || steps, "REFLECT 推理", ev.text.slice(0, 60), "think");
            break;
          case "retry":
            push(thoughtCard(ev.reason === "json_parse" ? "输出格式不对，正在重新组织" : ("工具 " + (ev.detail || "") + " 不在可用清单，换一个"), "retry"));
            break;
          case "tool_start":
            push(`<div data-pending="1" class="rounded-xl border border-dashed border-ai-200 bg-ai-50/60 px-3 py-2 text-[11.5px] text-ai-700 animate-pulse">
              <span class="font-mono">${esc(ev.name)}</span><span class="text-ink-faint"> 调用中…</span>
            </div>`);
            break;
          case "tool_end":
            body.querySelectorAll("[data-pending]").forEach((el) => el.remove());
            tools += 1;
            if (["record_trade", "sync_position", "update_account_cash"].includes(ev.name)) wroteAccount = true;
            if (ev.source) sources.add(String(ev.source).split(" ")[0]);
            push(toolCard(ev));
            traceAdd(ev.step || steps, ev.name, `${ev.ms}ms · ${ev.source || "-"}`, "tool");
            $("#agentMTools").textContent = tools;
            $("#agentSources").innerHTML = [...sources].map((s) => `<span class="text-[10.5px] px-1.5 py-0.5 rounded bg-brand-50 text-brand-600">${esc(s)}</span>`).join("");
            break;
          case "answer":
            push(answerCard(ev.answer));
            traceAdd(steps + 1, "ANSWER 结论", "输出分级建议", "answer");
            steps += 1;
            break;
          case "error":
            push(`<div class="rounded-xl border border-[#f3dfae] bg-warn-soft px-3 py-2.5 text-[12px] text-[#8a6100] leading-relaxed"><b>没能完成分析</b><br/>${esc(ev.message || "未知错误")}</div>`);
            chatSetState("失败", "bg-up-soft text-up");
            break;
          case "done":
            if (ev.ok) chatSetState("已完成", "bg-down-soft text-down");
            break;
        }
        $("#agentMSteps").textContent = steps;
        $("#agentMTime").textContent = ((Date.now() - t0) / 1000).toFixed(1);
      }, chatAbort.signal);
    } catch (err) {
      if (err.name === "AbortError") {
        push(`<div class="text-[12px] text-ink-faint">已停止。</div>`);
        chatSetState("已停止", "bg-ink-faint/10 text-ink-muted");
      } else {
        push(`<div class="rounded-xl border border-[#f6d6db] bg-up-soft px-3 py-2.5 text-[12px] text-up leading-relaxed">${esc(err.message || err)}</div>`);
        chatSetState("失败", "bg-up-soft text-up");
      }
    } finally {
      chatBusy = false;
      chatAbort = null;
      $("#agentSendBtn").disabled = false;
      $("#agentSendBtn").classList.remove("opacity-60");
      $("#agentStopBtn").classList.add("hidden");
      // 记过账就把首页的持仓、账户、流水一起刷一遍，别让用户看到旧股数
      if (wroteAccount && window.__app) {
        try {
          window.__app.loadFavorites?.(true);
          window.__app.loadTrades?.();
        } catch (e) { /* 刷新失败不影响对话结果 */ }
      }
      // 本轮问答已落库：刷新会话下拉，让「历史」里立刻能看到
      loadConversations(conversationId).catch(() => {});
    }
  }

  function renderSuggest() {
    const box = $("#agentSuggest");
    if (!box) return;
    const items = [
      "今天我这几个票要注意什么？",
      "我的持仓风险大吗？",
      "帮我看看重仓那只票现在什么位置",
      "我的策略适合现在的行情吗？",
    ];
    box.innerHTML = items.map((q) => `<button class="agent-suggest px-2.5 py-1 rounded-lg border border-line bg-white text-[11.5px] text-ink-muted hover:border-ai-200 hover:text-ai-600 hover:bg-ai-50">${esc(q)}</button>`).join("");
    box.querySelectorAll(".agent-suggest").forEach((b) => {
      b.onclick = () => { const inp = $("#agentInput"); if (inp) inp.value = b.textContent; agentAsk(b.textContent); };
    });
  }

  async function initCopilot() {
    renderSuggest();
    // 历史会话：先拉列表，再把上次会话恢复出来（用户关掉页面再回来能接着聊）
    await loadConversations(conversationId);
    if (conversationId) { loadConversation(conversationId).catch(() => {}); }
    try {
      const meta = await ensureMeta();
      const banner = $("#agentBanner");
      if (!banner) return;
      const m = meta.model || {};
      const st = meta.llm_state || {};
      const broken = ["auth", "forbidden", "model_not_found"].indexOf(st.last_error_code) >= 0;
      if (!m.configured) {
        banner.classList.remove("hidden");
        banner.innerHTML = "尚未配置模型 API Key，AI 功能不可用。请到「系统设置 → 模型」填写后重试。";
      } else if (broken) {
        banner.classList.remove("hidden");
        banner.innerHTML = `<b>模型调用失败，AI 功能暂时不可用。</b><br/>${esc(st.last_error || "")}<br/><span class="text-[11.5px] opacity-80">最近失败：${esc(st.last_error_at || "-")}</span>`;
      } else {
        banner.classList.add("hidden");
      }
    } catch (e) { /* meta 拉不到不阻塞界面 */ }
  }

  /* =========================================================================
     2. 信号复核
     ========================================================================= */
  let reviewItems = [];

  async function loadReviewQueue() {
    const btn = $("#reviewRunBtn");
    btn.disabled = true; btn.classList.add("opacity-60");
    $("#reviewList").innerHTML = `<div class="px-4 py-8 text-center text-[12px] text-ink-faint">正在执行监控扫描…</div>`;
    try {
      const rows = await api("/api/stock/monitor");
      const list = Array.isArray(rows) ? rows : (Array.isArray(rows.data) ? rows.data : []);
      reviewItems = list.map((r) => ({
        code: r.secID || r.code || "",
        name: r.name || r.secShortName || "",
        signal: r.reason || r.signal || r.message || "触发监控规则",
        price: r.price,
        pct: r.pctChg != null ? r.pctChg : r.pct_chg,
        verdict: null,
      }));

      if (!reviewItems.length) {
        // 非交易时段无监控信号：给一个手动复核对象，保证功能可用
        let f = null;
        try {
          const favs = await api("/api/stock/favorites?quotes=false");
          f = ((favs && Array.isArray(favs.data)) ? favs.data : (Array.isArray(favs) ? favs : []))[0] || null;
        } catch (e) { /* 忽略 */ }
        reviewItems = [{
          code: f ? f.secID : "",
          name: f ? (f.name || f.secShortName) : "",
          signal: "手动复核：请描述要复核的信号（默认按「放量突破 MA20」处理）",
          price: f ? f.price : null,
          pct: f ? f.pctChg : null,
          verdict: null,
          manual: true,
        }];
        $("#reviewSum").textContent = "当前无监控信号（非交易时段），切换为手动复核";
      } else {
        $("#reviewSum").textContent = `共 ${reviewItems.length} 条待复核`;
      }
      renderReviewList();
      if (reviewItems[0]) selectReview(0);
    } catch (e) {
      $("#reviewList").innerHTML = `<div class="px-4 py-6 text-center text-[12px] text-up">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false; btn.classList.remove("opacity-60");
    }
  }

  const VERDICT_STYLE = {
    pass: ["通过", "bg-down-soft text-down"],
    downgrade: ["降级", "bg-warn-soft text-warn"],
    block: ["拦截", "bg-ai-50 text-ai-600"],
  };

  function renderReviewList() {
    const box = $("#reviewList");
    box.innerHTML = reviewItems.map((it, i) => {
      const v = it.verdict && VERDICT_STYLE[it.verdict];
      return `<button class="w-full text-left px-4 py-3 hover:bg-ai-50/40 transition-colors" data-idx="${i}">
        <div class="flex items-center gap-2">
          <span class="text-[12px] font-semibold">${esc(it.code || "手动复核")}</span>
          <span class="text-[11px] text-ink-faint">${esc(it.name || "")}</span>
          <span class="ml-auto text-[10px] px-1.5 py-0.5 rounded font-semibold ${v ? v[1] : "bg-ink-faint/10 text-ink-muted"}">${v ? v[0] : "待复核"}</span>
        </div>
        <div class="text-[11px] text-ink-muted mt-1 line-clamp-2">${esc(it.signal)}</div>
      </button>`;
    }).join("");
    box.querySelectorAll("button[data-idx]").forEach((b) => {
      b.onclick = () => selectReview(Number(b.dataset.idx));
    });
  }

  async function selectReview(idx) {
    const it = reviewItems[idx];
    if (!it) return;
    const detail = $("#reviewDetail");
    detail.innerHTML = `<div class="text-center py-16 text-[12.5px] text-ink-faint">正在复核 ${esc(it.code)}…<br/><span class="text-[11px]">会依次查分时、资金流、板块、K线</span></div>`;
    try {
      const res = await fetch("/api/agent/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: it.code, name: it.name, signal: it.signal, price: it.price, pct_chg: it.pct }),
      });
      if (res.status === 401) throw new Error("登录已失效");
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "复核失败");

      it.verdict = data.verdict && data.verdict !== "unknown" ? data.verdict : "downgrade";
      renderReviewList();
      const v = VERDICT_STYLE[it.verdict] || VERDICT_STYLE.downgrade;
      detail.innerHTML = `
        <div class="flex items-start gap-3 pb-4 border-b border-line">
          <div class="w-10 h-10 rounded-xl ${v[1]} grid place-items-center shrink-0 text-[13px] font-bold">${v[0]}</div>
          <div class="flex-1">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-[14px] font-bold">${esc(it.code)} ${esc(it.name)}</span>
              <span class="text-[10.5px] px-1.5 py-0.5 rounded font-semibold ${v[1]}">${v[0]}</span>
            </div>
            <div class="text-[12px] text-ink-muted mt-1">原始信号：${esc(it.signal)}</div>
          </div>
        </div>
        <div class="mt-4 rounded-xl bg-ai-50/60 border border-ai-100 p-3.5">
          <div class="text-[12.5px] font-semibold mb-1">Agent 判定</div>
          <div class="text-[12.5px] leading-relaxed">${mdLite(data.headline || "")}</div>
        </div>
        <div class="mt-4">
          <div class="text-[12px] font-semibold mb-2">证据链</div>
          <div class="space-y-2">
            ${(data.evidence || []).map((e, i) => `
              <div class="flex gap-2.5 items-start">
                <div class="w-6 h-6 rounded-lg bg-ai-50 text-ai-600 grid place-items-center text-[10.5px] font-bold shrink-0">${i + 1}</div>
                <div class="flex-1 text-[12px] leading-relaxed">
                  ${e.title ? `<b>${esc(e.title)}</b>　` : ""}<span class="text-ink-muted">${mdLite(e.body || "")}</span>
                </div>
              </div>`).join("") || `<div class="text-[12px] text-ink-faint">无额外证据</div>`}
          </div>
        </div>
        <div class="mt-4 pt-3 border-t border-line text-[10.5px] text-ink-faint">
          调用工具：${(data.tools || []).map((t) => `${esc(t.name)}(${t.ms}ms)`).join(" → ") || "无"}
        </div>`;
    } catch (e) {
      detail.innerHTML = `<div class="rounded-xl border border-[#f3dfae] bg-warn-soft px-3 py-3 text-[12px] text-[#8a6100] leading-relaxed">
        <b>复核未完成</b><br/>${esc(e.message)}</div>`;
    }
  }

  /* =========================================================================
     3. 盘后复盘
     ========================================================================= */
  async function runDaily() {
    const btn = $("#dailyRunBtn");
    btn.disabled = true; btn.classList.add("opacity-60");
    const bodyEl = $("#dailyBody");
    bodyEl.innerHTML = `<div class="bg-white border border-line rounded-2xl shadow-card p-10 text-center text-[12.5px] text-ink-faint">Agent 正在汇总组合与异动…</div>`;

    let answer = null, errMsg = null;
    const trace = [];
    try {
      await streamAgent({ question: "请生成最近一个交易日的盘后复盘。", scenario: "daily" }, (ev) => {
        if (ev.type === "tool_end") trace.push({ name: ev.name, ms: ev.ms, source: ev.source, ok: ev.ok });
        if (ev.type === "answer") answer = ev.answer;
        if (ev.type === "error") errMsg = ev.message;
      });
    } catch (e) { errMsg = e.message; }

    if (!answer) {
      bodyEl.innerHTML = `<div class="bg-white border border-line rounded-2xl shadow-card p-8 text-center">
        <div class="text-[13px] font-semibold mb-2">复盘未能生成</div>
        <div class="text-[12px] text-ink-muted leading-relaxed">${esc(errMsg || "未知错误")}</div>
      </div>`;
      btn.disabled = false; btn.classList.remove("opacity-60");
      return;
    }

    bodyEl.innerHTML = `
      <div class="rounded-2xl bg-gradient-to-br from-ai-500 to-ai-700 text-white p-5">
        <div class="text-[11px] opacity-75 mb-1.5">一句话结论</div>
        <div class="text-[15px] leading-relaxed font-medium">${mdLite(answer.headline || "")}</div>
      </div>
      ${answerCard(answer)}
      <div class="bg-white border border-line rounded-2xl shadow-card p-4">
        <div class="text-[11.5px] font-semibold mb-2">本次调用的数据</div>
        <div class="flex flex-wrap gap-1.5">
          ${trace.map((t) => `<span class="text-[10.5px] px-1.5 py-0.5 rounded ${t.ok ? "bg-brand-50 text-brand-600" : "bg-up-soft text-up"}">${esc(t.name)} ${t.ms}ms</span>`).join("") || `<span class="text-[11px] text-ink-faint">无</span>`}
        </div>
      </div>`;
    btn.disabled = false; btn.classList.remove("opacity-60");
  }

  /* =========================================================================
     4. 策略诊断（回测数据本地算，Agent 只做归因）
     ========================================================================= */
  async function fillDoctorOptions() {
    const codeSel = $("#doctorCode");
    const stratSel = $("#doctorStrategy");
    try {
      const favs = await api("/api/stock/favorites?quotes=false");
      const favList = (favs && Array.isArray(favs.data)) ? favs.data : (Array.isArray(favs) ? favs : []);
      codeSel.innerHTML = favList.map((f) => `<option value="${esc(f.secID)}">${esc(f.secID)} ${esc(f.name || f.secShortName || "")}</option>`).join("") || `<option value="">（无自选股）</option>`;
    } catch (e) { console.warn("doctor favorites load failed:", e); codeSel.innerHTML = `<option value="">加载失败</option>`; }
    try {
      const strats = await api("/api/stock/strategy/list");
      const stratList = (strats && Array.isArray(strats.data)) ? strats.data : (Array.isArray(strats) ? strats : []);
      stratSel.innerHTML = stratList.map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("") || `<option value="">（无策略）</option>`;
    } catch (e) { console.warn("doctor strategies load failed:", e); stratSel.innerHTML = `<option value="">加载失败</option>`; }
  }

  async function runDoctor() {
    const code = $("#doctorCode").value;
    if (!code) { toast("没有可诊断的标的", "error"); return; }
    const btn = $("#doctorRunBtn");
    btn.disabled = true; btn.classList.add("opacity-60");
    const bodyEl = $("#doctorBody");
    bodyEl.innerHTML = `<div class="bg-white border border-line rounded-2xl shadow-card p-8 text-center text-[12.5px] text-ink-faint">正在跑回测…</div>`;

    // 1) 先拿真实回测数据（不依赖模型）
    let bt = null, btErr = null;
    try {
      const strat = $("#doctorStrategy").value;
      const payload = { code, capital: 100000, benchmark: "hs300" };
      if (strat) {
        const strats = await api("/api/stock/strategy/list");
        const stratList = (strats && Array.isArray(strats.data)) ? strats.data : (Array.isArray(strats) ? strats : []);
        const found = stratList.find((s) => s.name === strat);
        if (found) { payload.entry_expr = found.entry_expr; payload.exit_expr = found.exit_expr; }
      }
      const resp = await fetch("/api/stock/backtest", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      bt = await resp.json();
      if (!bt.ok) btErr = bt.message || bt.error || "回测失败";
    } catch (e) { btErr = e.message; }

    let metricHtml = "";
    if (bt && bt.ok) {
      const m = bt.metrics || {};
      const card = (label, val, color) => `<div><div class="text-[10.5px] text-ink-faint">${label}</div><div class="text-[17px] font-bold ${color || ""} leading-tight">${val}</div></div>`;
      metricHtml = `<div class="bg-white border border-line rounded-2xl shadow-card p-4">
        <div class="flex items-center gap-2 mb-3">
          <span class="text-[12.5px] font-bold">真实回测结果</span>
          <span class="text-[10.5px] text-ink-faint">${esc(bt.start_date)} ~ ${esc(bt.end_date)} · ${bt.bars} 根K线</span>
        </div>
        <div class="grid grid-cols-4 gap-3">
          ${card("总收益", pct(bt.return_pct), cls(bt.return_pct))}
          ${card("基准", pct((bt.benchmark || {}).return_pct), cls((bt.benchmark || {}).return_pct))}
          ${card("超额", pct(bt.excess_pct), cls(bt.excess_pct))}
          ${card("最大回撤", (m.max_drawdown_pct != null ? m.max_drawdown_pct.toFixed(2) + "%" : "-"))}
          ${card("夏普", m.sharpe != null ? m.sharpe : "-", cls(m.sharpe))}
          ${card("胜率", m.win_rate_pct != null ? m.win_rate_pct.toFixed(1) + "%" : "-", cls((m.win_rate_pct || 0) - 50))}
          ${card("交易次数", m.trade_count != null ? m.trade_count : "-")}
          ${card("平均持有", m.avg_hold_days != null ? m.avg_hold_days + " 天" : "-")}
        </div>
      </div>`;
    } else {
      metricHtml = `<div class="bg-white border border-line rounded-2xl shadow-card p-5 text-[12px] text-up">回测未成功：${esc(btErr || "未知")}</div>`;
    }
    bodyEl.innerHTML = metricHtml + `<div class="bg-white border border-line rounded-2xl shadow-card p-8 text-center text-[12.5px] text-ink-faint">Agent 正在做归因…</div>`;

    // 2) 再让 Agent 归因
    let answer = null, errMsg = null;
    try {
      let ctx = "";
      if (bt && bt.ok) {
        const m = bt.metrics || {};
        ctx = `回测结果（已跑好，不要重复调用 run_backtest）：\n标的 ${bt.name || code}，区间 ${bt.start_date} ~ ${bt.end_date}，${bt.bars} 根K线\n总收益 ${bt.return_pct}%，基准 ${(bt.benchmark || {}).return_pct}%，超额 ${bt.excess_pct}%\n最大回撤 ${m.max_drawdown_pct}%，夏普 ${m.sharpe}，胜率 ${m.win_rate_pct}%，交易 ${m.trade_count} 笔，平均持有 ${m.avg_hold_days} 天`;
      }
      await streamAgent({
        question: `请诊断 ${code} 这套策略为什么表现不佳，并给出改进建议。`,
        scenario: "doctor",
        context: { prompt: ctx },
      }, (ev) => {
        if (ev.type === "answer") answer = ev.answer;
        if (ev.type === "error") errMsg = ev.message;
      });
    } catch (e) { errMsg = e.message; }

    const diagHtml = answer
      ? answerCard(answer)
      : `<div class="bg-white border border-line rounded-2xl shadow-card p-5 text-[12px] text-ink-muted leading-relaxed">
           <b class="text-ink">归因未生成</b><br/>${esc(errMsg || "未知错误")}
         </div>`;
    bodyEl.innerHTML = metricHtml + diagHtml;
    btn.disabled = false; btn.classList.remove("opacity-60");
  }

  /* =========================================================================
     5. 持仓风险（先纯计算，再让 Agent 解释）
     ========================================================================= */
  async function runRisk() {
    const btn = $("#riskRunBtn");
    btn.disabled = true; btn.classList.add("opacity-60");
    const bodyEl = $("#riskBody");
    bodyEl.innerHTML = `<div class="bg-white border border-line rounded-2xl shadow-card p-8 text-center text-[12.5px] text-ink-faint">
      正在计算组合暴露…<br/><span class="text-[11px]">需要拉取 4 只票的实时行情与所属行业，首次约 20 秒</span>
    </div>`;

    let pf = null, pfErr = null;
    try { pf = await api("/api/agent/portfolio"); if (!pf.ok) pfErr = pf.text || "计算失败"; }
    catch (e) { pfErr = e.message; }

    let calcHtml = "";
    if (pf && pf.ok && pf.data) {
      const d = pf.data;
      const rows = (d.positions || []).slice();
      const maxW = Math.max(...rows.map((r) => r.weight || 0), 1);
      const ind = Object.entries(d.industry_weight || {}).sort((a, b) => b[1] - a[1]);
      calcHtml = `<div class="grid grid-cols-2 gap-4">
        <div class="bg-white border border-line rounded-2xl shadow-card p-4">
          <div class="text-[12px] font-bold mb-3">个股权重</div>
          <div class="space-y-2">
            ${rows.map((r) => `
              <div>
                <div class="flex justify-between text-[11.5px] mb-1">
                  <span>${esc(r.name || r.secID)}</span>
                  <span class="font-medium ${r.weight > 35 ? "text-up" : ""}">${(r.weight || 0).toFixed(1)}%</span>
                </div>
                <div class="h-1.5 rounded-full bg-line overflow-hidden">
                  <div class="h-full ${r.weight > 35 ? "bg-up" : "bg-brand-500"} rounded-full" style="width:${Math.min(100, (r.weight / maxW) * 100)}%"></div>
                </div>
              </div>`).join("")}
          </div>
          <div class="text-[10.5px] text-ink-faint mt-2">红色为超过 35% 集中度阈值</div>
        </div>
        <div class="bg-white border border-line rounded-2xl shadow-card p-4">
          <div class="text-[12px] font-bold mb-3">行业暴露</div>
          <div class="space-y-2">
            ${ind.map(([k, v]) => `
              <div>
                <div class="flex justify-between text-[11.5px] mb-1">
                  <span>${esc(k || "未分类")}</span><span class="font-medium ${v > 50 ? "text-up" : ""}">${v.toFixed(1)}%</span>
                </div>
                <div class="h-1.5 rounded-full bg-line overflow-hidden">
                  <div class="h-full ${v > 50 ? "bg-up" : "bg-brand-500"} rounded-full" style="width:${Math.min(100, v)}%"></div>
                </div>
              </div>`).join("") || `<div class="text-[11px] text-ink-faint">无行业数据</div>`}
          </div>
          <div class="text-[10.5px] text-ink-faint mt-2">红色为超过 50% 行业阈值</div>
        </div>
      </div>
      <div class="bg-white border border-line rounded-2xl shadow-card p-4">
        <div class="text-[12px] font-bold mb-2">明细</div>
        <div class="overflow-x-auto">
          <table class="w-full text-[11.5px]">
            <thead class="text-ink-faint"><tr>
              <th class="text-left py-1.5 font-medium">标的</th><th class="text-right font-medium">持股</th>
              <th class="text-right font-medium">成本</th><th class="text-right font-medium">现价</th>
              <th class="text-right font-medium">浮盈亏</th><th class="text-right font-medium">权重</th>
              <th class="text-right font-medium">距止损线</th>
            </tr></thead>
            <tbody class="divide-y divide-line">
              ${rows.map((r) => `<tr>
                <td class="py-1.5">${esc(r.name || r.secID)}</td>
                <td class="text-right">${r.shares}</td>
                <td class="text-right">${fmt(r.cost)}</td>
                <td class="text-right">${r.price != null ? fmt(r.price) : "-"}</td>
                <td class="text-right ${cls(r.profit_pct)}">${r.profit_pct != null ? pct(r.profit_pct) : "-"}</td>
                <td class="text-right">${(r.weight || 0).toFixed(1)}%</td>
                <td class="text-right ${r.stop_line_distance != null && r.stop_line_distance <= 0 ? "text-up font-semibold" : ""}">${
                  r.stop_line_distance == null ? "-" : (r.stop_line_distance <= 0 ? "已击穿 " + r.stop_line_distance.toFixed(1) : "+" + r.stop_line_distance.toFixed(1) + " 点")
                }</td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>
      </div>`;
      const over = rows.filter((r) => r.weight > 35).length + ind.filter(([, v]) => v > 50).length;
      const badge = $("#riskBadge");
      badge.textContent = over ? `${over} 项超阈值` : "未超阈值";
      badge.className = "text-[11px] px-2 py-0.5 rounded-md " + (over ? "bg-up-soft text-up" : "bg-down-soft text-down");
    } else {
      calcHtml = `<div class="bg-white border border-line rounded-2xl shadow-card p-5 text-[12px] text-up">组合计算失败：${esc(pfErr || "未知")}</div>`;
    }
    bodyEl.innerHTML = calcHtml + `<div class="bg-white border border-line rounded-2xl shadow-card p-8 text-center text-[12.5px] text-ink-faint">Agent 正在解释风险并给优先级…</div>`;

    let answer = null, errMsg = null;
    try {
      await streamAgent({
        question: "请从组合视角给我做一次风险体检，指出最严重的问题并给优先级排序的动作。",
        scenario: "risk",
        context: { prompt: pf && pf.ok ? ("组合计算已给出如下结果（不要重复调用工具算一遍）：\n" + pf.text) : "" },
      }, (ev) => {
        if (ev.type === "answer") answer = ev.answer;
        if (ev.type === "error") errMsg = ev.message;
      });
    } catch (e) { errMsg = e.message; }

    const aiHtml = answer ? answerCard(answer)
      : `<div class="bg-white border border-line rounded-2xl shadow-card p-5 text-[12px] text-ink-muted leading-relaxed"><b class="text-ink">Agent 判断未生成</b><br/>${esc(errMsg || "未知错误")}</div>`;
    bodyEl.innerHTML = calcHtml + aiHtml;
    btn.disabled = false; btn.classList.remove("opacity-60");
  }

  /* =========================================================================
     6. 卖出决策（先按风格算出目标价，再让 Agent 判断可达性与分批）
     ========================================================================= */
  const SELL_STYLE = { long: "长线", mid_long: "中长线", swing: "波段" };
  const SELL_STATUS = {
    below_stop: ["bg-up-soft text-up", "已跌破止损"],
    valuation_blocked: ["bg-up-soft text-up", "估值透支"],
    theme_overheat: ["bg-up-soft text-up", "情绪过热"],
    theme_hot: ["bg-down-soft text-down", "热度偏高"],
    above_t2: ["bg-down-soft text-down", "已达主目标"],
    above_t1: ["bg-down-soft text-down", "已达第一目标"],
    deep_loss: ["bg-up-soft text-up", "深度浮亏"],
    loss: ["bg-ink-faint/10 text-ink-muted", "浮亏持有"],
    profit: ["bg-down-soft text-down", "浮盈持有"],
    no_position: ["bg-ink-faint/10 text-ink-muted", "无持仓成本"],
  };

  const FACTOR_TONE = {
    // 因子分档位：正分偏多（红），负分偏空（绿），A 股习惯
    pos: "bg-down-soft text-down",
    neg: "bg-up-soft text-up",
    flat: "bg-ink-faint/10 text-ink-muted",
  };

  function factorChip(label, verdict, score) {
    const tone = score > 0.12 ? FACTOR_TONE.pos : (score < -0.12 ? FACTOR_TONE.neg : FACTOR_TONE.flat);
    const s = (Number(score) >= 0 ? "+" : "") + Number(score || 0).toFixed(2);
    return `<span class="text-[10px] px-1.5 py-0.5 rounded ${tone}" title="${esc(label)}">
      ${esc(label)} ${esc(verdict || "-")} <b class="font-bold">${s}</b></span>`;
  }

  function sellCard(p) {
    const st = SELL_STATUS[p.status] || ["bg-ink-faint/10 text-ink-muted", p.status_text || p.status || "-"];
    const pnl = p.profit_pct == null ? "无成本" : (p.profit_pct >= 0 ? "+" : "") + Number(p.profit_pct).toFixed(2) + "%";
    const pnlCls = p.profit_pct == null ? "text-ink-muted" : (p.profit_pct >= 0 ? "text-down" : "text-up");
    const rows = (p.targets || []).map((t) => `
      <tr class="border-t border-line/60">
        <td class="py-1.5 text-[11.5px] text-ink-muted whitespace-nowrap">${esc(t.level)} ${esc(t.name || "")}</td>
        <td class="py-1.5 text-right text-[13px] font-bold kbox">${Number(t.price).toFixed(2)}</td>
        <td class="py-1.5 text-right text-[11px] whitespace-nowrap">${t.pct_from_now >= 0 ? "+" : ""}${Number(t.pct_from_now).toFixed(2)}%</td>
        <td class="py-1.5 text-right text-[10.5px] text-ink-muted whitespace-nowrap">${t.trim_pct ? t.trim_pct + "%" : "-"}${t.trim_shares ? `<span class="text-ink-faint">/${t.trim_shares}股</span>` : ""}</td>
        <td class="py-1.5 pl-2 text-[10.5px] text-ink-faint leading-snug">${esc(t.basis || "")}</td>
      </tr>`).join("");
    const sl = p.stop_loss || {};
    const tt = p.t_trade;
    const f = p.factors || {};
    const fv = p.fair_value || {};
    const warns = (p.warnings || []).map((w) => `<div class="text-[10.5px] text-up mt-1">· ${esc(w)}</div>`).join("");
    const chips = [
      factorChip("估值", (f.valuation || {}).verdict, (f.valuation || {}).score),
      factorChip("质量", (f.quality || {}).verdict, (f.quality || {}).score),
      factorChip("成长", (f.growth || {}).verdict, (f.growth || {}).score),
      factorChip("行业", (f.industry_cycle || {}).verdict, (f.industry_cycle || {}).score),
      factorChip("题材", (f.theme || {}).verdict, (f.theme || {}).score),
    ].join("");
    const peBits = [];
    if (f.valuation && f.valuation.pe != null) peBits.push(`PE ${Number(f.valuation.pe).toFixed(1)}`);
    if (f.valuation && f.valuation.pe_pct != null) peBits.push(`分位 ${(Number(f.valuation.pe_pct) * 100).toFixed(0)}%`);
    if (f.valuation && f.valuation.pb != null) peBits.push(`PB ${Number(f.valuation.pb).toFixed(2)}`);
    if (f.growth && f.growth.profit_growth != null) peBits.push(`净利 ${Number(f.growth.profit_growth).toFixed(0)}%`);
    if (f.quality && f.quality.roe != null) peBits.push(`ROE ${Number(f.quality.roe).toFixed(1)}%`);
    const stanceTone = Number(p.composite || 0) >= 0.1 ? "text-down" : (Number(p.composite || 0) <= -0.1 ? "text-up" : "text-ink-muted");
      const ep = p.exit_policy || {};
      const scs = (p.scenarios || []).map((s) => {
        const on = !!s.is_active;
        const prices = (s.targets || []).map((t) => Number(t.price).toFixed(2)).join(" / ");
        const trims = (s.targets || []).map((t) => `${t.trim_pct || 0}%`).join("/");
        return `<div class="rounded-xl border p-2 ${on ? "border-ai-500 bg-ai-50" : "border-line bg-white"}">
          <div class="flex items-center justify-between gap-1">
            <span class="text-[10.5px] font-semibold ${on ? "text-ai-700" : "text-ink-muted"}">${esc(s.label || "")}</span>
            ${on ? '<span class="text-[9px] px-1 py-[1px] rounded bg-ai-600 text-white shrink-0">当前</span>' : ""}
          </div>
          <div class="text-[11.5px] font-bold kbox mt-0.5">${prices || "-"}</div>
          <div class="text-[9.5px] text-ink-faint mt-0.5">减仓 ${trims || "-"}</div>
          <div class="text-[9.5px] text-ink-faint leading-tight mt-1">${esc(s.assumption || "")}</div>
        </div>`;
      }).join("");
      const rs = (ep.rules || []).map((r) => `<div class="text-[10.5px] text-ink-muted leading-snug">· <span class="text-ink">${esc(r.if || "")}</span> → ${esc(r.then || "")}</div>`).join("");
      const hs = ep.hard_stop || {}, ss2 = ep.soft_stop || {}, ts = ep.trailing_stop || {};
      const ms = p.market_sentiment || {}, stk = p.stock_sentiment || {};
      const modeTone = ep.mode === "exit_now" ? "bg-up-soft text-up"
        : ep.mode === "defend" ? "bg-up-soft text-up"
        : ep.mode === "ride" ? "bg-down-soft text-down"
        : ep.mode === "trim_into_strength" ? "bg-warn-soft text-ink"
        : "bg-ink-faint/10 text-ink-muted";
      return `<div class="bg-white border border-line rounded-2xl shadow-card p-4">
      <div class="flex items-start justify-between gap-2">
        <div>
          <div class="text-[13.5px] font-bold">${esc(p.name || p.code || "-")} <span class="text-[11px] text-ink-faint font-normal">${esc(p.code || "")}</span></div>
          <div class="text-[11.5px] text-ink-muted mt-0.5">现价 <b class="text-ink">${Number(p.price).toFixed(2)}</b>
            · 成本 ${p.cost == null ? "-" : Number(p.cost).toFixed(2)} · 浮动 <b class="${pnlCls}">${pnl}</b></div>
        </div>
        <span class="text-[10.5px] px-2 py-0.5 rounded-md shrink-0 ${st[0]}">${esc(st[1])}</span>
      </div>
      ${ms.ok || stk.ok ? `<div class="mt-1.5 flex items-center gap-1.5 flex-wrap text-[10px]">
        ${ms.ok ? `<span class="px-1.5 py-0.5 rounded bg-ink-faint/10 text-ink-muted">市场 ${esc(ms.phase_label || "-")} ${Math.round(Number(ms.temperature || 0) * 100)}%</span>` : ""}
        ${ms.ok && ms.metrics ? `<span class="px-1.5 py-0.5 rounded bg-ink-faint/10 text-ink-faint">涨停 ${ms.metrics.zt_count} / 炸板 ${ms.metrics.zb_count} / 最高 ${ms.metrics.max_board} 板</span>` : ""}
        ${stk.ok ? `<span class="px-1.5 py-0.5 rounded ${stk.relay ? "bg-down-soft text-down" : "bg-ink-faint/10 text-ink-muted"}">个股 ${esc(stk.verdict || "-")}${stk.limit_up_streak ? " " + stk.limit_up_streak + "板" : ""}${stk.zb_today ? " 炸板" : ""}</span>` : ""}
      </div>` : ""}
      <div class="mt-2 flex items-center gap-2 flex-wrap text-[11px]">
        <span class="text-ink-muted">估值中枢 <b class="text-ink kbox">${fv.price == null ? "-" : Number(fv.price).toFixed(2)}</b></span>
        <span class="text-ink-faint">区间 ${fv.low == null ? "-" : Number(fv.low).toFixed(2)}~${fv.high == null ? "-" : Number(fv.high).toFixed(2)}
          · 情绪上限 ${fv.ceiling == null ? "-" : Number(fv.ceiling).toFixed(2)}</span>
        <span class="text-ink-faint">综合 <b class="${stanceTone}">${(Number(p.composite || 0) >= 0 ? "+" : "") + Number(p.composite || 0).toFixed(2)}</b></span>
      </div>
      <div class="mt-1.5 flex items-center gap-1 flex-wrap">${chips}</div>
      <div class="mt-1 text-[10.5px] text-ink-faint leading-snug">${esc(peBits.join(" · "))}</div>
      ${ep.mode ? `<div class="mt-2.5 rounded-xl border border-line p-2.5">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="text-[10.5px] px-1.5 py-0.5 rounded ${modeTone}">${esc(ep.mode_label || ep.mode)}</span>
          <span class="text-[10px] text-ink-faint">硬止损 ${hs.price == null ? "-" : Number(hs.price).toFixed(2)} · 结构止损 ${ss2.price == null ? "-" : Number(ss2.price).toFixed(2)}${ts.applies ? ` · 移动止盈 ${Number(ts.price).toFixed(2)}` : ""}</span>
        </div>
        <div class="text-[11px] text-ink leading-relaxed mt-1">${esc(ep.headline || "")}</div>
        <div class="mt-1 space-y-0.5">${rs}</div>
        ${ep.keep_core_pct ? `<div class="text-[10px] text-ai-700 mt-1">${esc(ep.keep_core_note || "")}</div>` : ""}
      </div>` : ""}
      <div class="mt-2.5">
        <div class="text-[10.5px] text-ink-faint mb-1">三套情景（按触发条件切换，不是三选一）</div>
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-1.5">${scs}</div>
      </div>
      <div class="mt-2 text-[10.5px] text-ink-faint">基线阶梯（当前多因子正常路径）</div>
      <table class="w-full mt-1">
        <thead><tr class="text-[10.5px] text-ink-faint">
          <th class="text-left font-normal pb-1">档位</th><th class="text-right font-normal pb-1">目标价</th>
          <th class="text-right font-normal pb-1">距现价</th><th class="text-right font-normal pb-1">减仓</th>
          <th class="text-left font-normal pb-1 pl-2">依据</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="mt-2 pt-2 border-t border-line flex items-center gap-3 flex-wrap text-[11.5px]">
        <span class="text-ink-muted">止损 <b class="text-up kbox">${sl.price == null ? "-" : Number(sl.price).toFixed(2)}</b>
          <span class="text-ink-faint">(${sl.pct_from_now == null ? "-" : (sl.pct_from_now >= 0 ? "+" : "") + Number(sl.pct_from_now).toFixed(2) + "%"})</span></span>
        <span class="text-ink-muted">盈亏比 <b class="${p.risk_reward != null && p.risk_reward >= 2 ? "text-down" : "text-ink"}">${p.risk_reward == null ? "-" : Number(p.risk_reward).toFixed(2) + ":1"}</b></span>
        ${tt ? `<span class="text-ink-muted">做T 高抛 <b class="kbox">${Number(tt.sell_price).toFixed(2)}</b> / 低吸 <b class="kbox">${Number(tt.buy_price).toFixed(2)}</b></span>` : ""}
        <span class="text-[11px] text-ai-700">${esc(p.style_label || "")}${p.archetype_label ? " · " + esc(p.archetype_label) + "股" : ""}</span>
      </div>
      <div class="mt-1.5 text-[11px] text-ink-muted leading-relaxed">${esc(p.status_text || "")}</div>
      <div class="mt-1 text-[10.5px] text-ink-faint leading-snug">${esc(fv.basis || "")}</div>
      ${warns}
    </div>`;
  }

  async function runSell() {
    const btn = $("#sellRunBtn");
    btn.disabled = true; btn.classList.add("opacity-60");
    const bodyEl = $("#sellBody");
    bodyEl.innerHTML = `<div class="bg-white border border-line rounded-2xl shadow-card p-8 text-center text-[12.5px] text-ink-faint">
      正在做多因子测算：估值分位 · 财报 · 行业周期 · 题材情绪 · 技术位…<br/>
      <span class="text-[11px]">每只票要拉日 K（800 天）、PE/PB 分位、最新财报、申万行业指数；首次约 30~60 秒，之后有缓存会快很多</span>
    </div>`;

    let plan = null, planErr = null;
    try { plan = await api("/api/agent/sell_plan"); if (!plan.ok) planErr = plan.text || "计算失败"; }
    catch (e) { planErr = e.message; }

    let calcHtml = "";
    let baseText = "";
    if (plan && plan.ok && Array.isArray(plan.data) && plan.data.length) {
      const plans = plan.data;
      const styleLabel = SELL_STYLE[plans[0].style] || plans[0].style;
      const badge = $("#sellStyleBadge");
      if (badge) {
        badge.textContent = "投资风格 " + styleLabel;
        badge.className = "text-[11px] px-2 py-0.5 rounded-md bg-ai-50 text-ai-700";
      }
      calcHtml = `<div class="grid grid-cols-1 xl:grid-cols-2 gap-4">` + plans.map(sellCard).join("") + `</div>`;
      baseText = plan.text || "";
    } else {
      calcHtml = `<div class="bg-white border border-line rounded-2xl shadow-card p-5 text-[12px] text-up">
        卖出计划计算失败：${esc(planErr || "没有可用的持仓或 K 线数据")}</div>`;
    }

    bodyEl.innerHTML = calcHtml + `<div class="bg-white border border-line rounded-2xl shadow-card p-8 text-center text-[12.5px] text-ink-faint">Agent 正在判断这些位置能不能到、该在哪一档减多少…</div>`;

    let answer = null, errMsg = null;
    try {
      await streamAgent({
        question: "请基于已给出的卖出计划，判断每只持仓的目标价能不能到、按什么节奏分批卖出，并说明理由。",
        scenario: "sell",
        context: {
          prompt: baseText
            ? ("卖出目标价已由系统按用户投资风格算好（不要重复调用 get_sell_target 再算一遍，可直接引用）：\n" + baseText)
            : "",
        },
      }, (ev) => {
        if (ev.type === "answer") answer = ev.answer;
        if (ev.type === "error") errMsg = ev.message;
      });
    } catch (e) { errMsg = e.message; }

    const aiHtml = answer ? answerCard(answer)
      : `<div class="bg-white border border-line rounded-2xl shadow-card p-5 text-[12px] text-ink-muted leading-relaxed"><b class="text-ink">Agent 判断未生成</b><br/>${esc(errMsg || "未知错误")}</div>`;
    bodyEl.innerHTML = calcHtml + aiHtml;
    btn.disabled = false; btn.classList.remove("opacity-60");
  }

  /* =========================================================================
     7. Agent 编排
     ========================================================================= */
  async function renderOrchestration() {
    const bodyEl = $("#orchBody");
    if (!bodyEl) return;
    let meta;
    try { meta = await ensureMeta(true); }
    catch (e) { bodyEl.innerHTML = `<div class="bg-white border border-line rounded-2xl shadow-card p-6 text-[12px] text-up">加载失败：${esc(e.message)}</div>`; return; }

    const tools = meta.tools || [];
    const groups = meta.groups || [];
    const scenarios = meta.scenarios || [];
    const model = meta.model || {};
    const llmState = meta.llm_state || {};
    const brokenCodes = ["auth", "forbidden", "model_not_found"];
    const keyBroken = model.configured && brokenCodes.indexOf(llmState.last_error_code) >= 0;

    const modelBadge = !model.configured
      ? `<span class="text-[10.5px] px-2 py-0.5 rounded-md bg-up-soft text-up">未配置 API Key</span>`
      : keyBroken
        ? `<span class="text-[10.5px] px-2 py-0.5 rounded-md bg-up-soft text-up">${esc(model.model || "")} · Key 可能失效</span>`
        : `<span class="text-[10.5px] px-2 py-0.5 rounded-md bg-down-soft text-down">${esc(model.model || "已配置")}</span>`;
    const keyWarn = keyBroken
      ? `<div class="mt-3 rounded-xl border border-[#f6d6db] bg-up-soft px-3 py-2 text-[11.5px] text-up leading-relaxed">
           最近一次调用失败（${esc(llmState.last_error_at || "")}）：${esc(llmState.last_error || "")}
         </div>`
      : "";

    bodyEl.innerHTML = `
      <div class="bg-white border border-line rounded-2xl shadow-card p-5">
        <div class="flex items-center gap-3 mb-1 flex-wrap">
          <div class="text-[13px] font-bold">Agent 如何挂在现有平台上</div>
          ${modelBadge}
          <span class="text-[11px] text-ink-faint">${esc(model.provider || "")} · ${esc(model.base_url || "")}</span>
        </div>
        <div class="text-[11.5px] text-ink-faint mb-4">不重做业务逻辑，把已有 API 注册成工具，Agent 站在最上层调度</div>
        ${keyWarn}
        <div class="grid grid-cols-[1fr_auto_1fr_auto_1fr] items-center gap-3">
          <div class="rounded-xl border border-line bg-ink/[0.02] p-3">
            <div class="text-[11px] font-bold text-ink-muted mb-2">数据源</div>
            <div class="space-y-1 text-[11px] text-ink-muted">
              <div>amazingdata（主源）</div>
              <div>xueqiu（备源 · 实际在用）</div>
              <div>eastmoney 资金流</div>
              <div>本地 config 与行业缓存</div>
            </div>
          </div>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#8a99ad" stroke-width="2"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          <div class="rounded-xl border border-brand-100 bg-brand-50/50 p-3">
            <div class="text-[11px] font-bold text-brand-700 mb-2">能力层 · ${tools.length} 个工具</div>
            <div class="space-y-1 text-[11px] text-ink-muted">
              ${groups.map((g) => `<div>${esc(g)} ${tools.filter((t) => t.group === g).length} 个</div>`).join("")}
            </div>
          </div>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#8a99ad" stroke-width="2"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          <div class="rounded-xl border border-ai-200 bg-ai-50 p-3">
            <div class="text-[11px] font-bold text-ai-700 mb-2">Agent 层 · ${scenarios.length} 个场景</div>
            <div class="space-y-1 text-[11px] text-ink-muted">
              ${scenarios.map((s) => `<div>${esc(s.name)}</div>`).join("")}
            </div>
          </div>
        </div>
      </div>

      <div class="bg-white border border-line rounded-2xl shadow-card overflow-hidden">
        <div class="px-5 py-3 border-b border-line flex items-center gap-3">
          <div class="text-[12.5px] font-bold">工具目录</div>
          <span class="text-[10.5px] px-2 py-0.5 rounded-md bg-brand-50 text-brand-600 font-medium">${tools.length} 个</span>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-[11.5px]">
            <thead class="bg-ink/[0.02] text-ink-faint"><tr>
              <th class="text-left font-medium px-5 py-2 w-[90px]">分组</th>
              <th class="text-left font-medium px-3 py-2 w-[190px]">工具名</th>
              <th class="text-left font-medium px-3 py-2">作用</th>
              <th class="text-left font-medium px-3 py-2 w-[150px]">参数</th>
              <th class="text-left font-medium px-3 py-2 w-[70px]">风险</th>
            </tr></thead>
            <tbody class="divide-y divide-line">
              ${tools.map((t) => `<tr class="hover:bg-ai-50/40">
                <td class="px-5 py-2 text-ink-faint">${esc(t.group)}</td>
                <td class="px-3 py-2 font-mono text-[11px] text-ai-600">${esc(t.name)}</td>
                <td class="px-3 py-2 text-ink-muted">${esc(t.desc)}</td>
                <td class="px-3 py-2 text-ink-faint text-[10.5px]">${esc(Object.keys(t.params || {}).join("、") || "无")}</td>
                <td class="px-3 py-2"><span class="text-[10px] px-1.5 py-0.5 rounded font-medium ${t.risk === "read" ? "bg-ink-faint/10 text-ink-faint" : "bg-warn-soft text-warn"}">${t.risk === "read" ? "只读" : t.risk}</span></td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>
        <div class="px-5 py-2.5 bg-ink/[0.02] text-[10.5px] text-ink-faint">
          只读工具 Agent 可自由调用；写操作需你确认；外部通知只能由系统流程触发。
        </div>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div class="bg-white border border-line rounded-2xl shadow-card p-4">
          <div class="text-[12.5px] font-bold mb-3">场景包</div>
          <div class="space-y-2">
            ${scenarios.map((s) => `
              <div class="rounded-xl border border-line p-3">
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="font-mono text-[11.5px] text-ai-600">${esc(s.id)}</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-ink-faint/10 text-ink-muted">${esc(s.trigger)}</span>
                  <span class="ml-auto text-[10px] text-ink-faint">${s.tools.length} 工具 · ${s.max_steps} 步</span>
                </div>
                <div class="text-[11px] text-ink-muted mt-1">${esc(s.desc)}</div>
              </div>`).join("")}
          </div>
        </div>
        <div class="bg-white border border-line rounded-2xl shadow-card p-4">
          <div class="text-[12.5px] font-bold mb-2">运行参数</div>
          <pre class="rounded-xl bg-ink text-[10.5px] leading-relaxed p-3 overflow-x-auto text-[#c9d4e3]"><code>id: signal-reviewer
name: 信号复核
model: ${esc(model.model || "default-llm")}
execution_mode: react
max_steps: 5
timeout: 90s

tools:
${(scenarios.find((s) => s.id === "review") || { tools: [] }).tools.map((t) => "  - " + t).join("\n")}

guard:
  on_timeout: pass     <span class="text-[#6b7d94]"># 超时放行，不阻塞告警</span>
  on_error: pass
  must_decide: true</code></pre>
          <div class="text-[10.5px] text-ink-faint mt-2 leading-relaxed">
            Agent 故障时默认放行告警：宁可多发一条，不能漏发。
          </div>
        </div>
      </div>`;
  }

  /* =========================================================================
     视图切换钩子
     ========================================================================= */
  const loaded = { copilot: false, orchestration: false, doctor: false, risk: false };

  window.__agentOnView = function (v) {
    if (v === "copilot" && !loaded.copilot) { loaded.copilot = true; initCopilot(); }
    if (v === "orchestration") { renderOrchestration(); }
    if (v === "doctor" && !loaded.doctor) { loaded.doctor = true; fillDoctorOptions(); }
    if (v === "risk" && !loaded.risk) { loaded.risk = true; runRisk(); }
    if (v === "daily") {
      const el = $("#dailyDate");
      if (el && !el.textContent) {
        const d = new Date();
        const wd = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
        el.textContent = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${wd} · 非交易时段生成的是最近一个交易日数据`;
      }
    }
  };

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    const send = $("#agentSendBtn");
    if (send) send.onclick = () => { const v = $("#agentInput").value.trim(); agentAsk(v); };
    const stop = $("#agentStopBtn");
    if (stop) stop.onclick = () => { if (chatAbort) chatAbort.abort(); };
    const convSel = $("#agentConvSelect");
    if (convSel) convSel.onchange = () => { loadConversation(convSel.value); };
    const convNew = $("#agentNewConv");
    if (convNew) convNew.onclick = () => {
      setConversationId("");
      chatClearFlow();
      traceReset();
      if (convSel) convSel.value = "";
      toast("已开始新对话", "上一轮记录还在历史下拉里，随时能翻回去", "info");
    };
    const convDel = $("#agentDelConv");
    if (convDel) convDel.onclick = deleteConversation;
    const input = $("#agentInput");
    if (input) {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); agentAsk(input.value.trim()); }
      });
    }
    const rv = $("#reviewRunBtn");
    if (rv) rv.onclick = loadReviewQueue;
    const db = $("#dailyRunBtn");
    if (db) db.onclick = runDaily;
    const dc = $("#doctorRunBtn");
    if (dc) dc.onclick = runDoctor;
    const rb = $("#riskRunBtn");
    if (rb) rb.onclick = runRisk;
    const sb = $("#sellRunBtn");
    if (sb) sb.onclick = runSell;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
