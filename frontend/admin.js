/* 系统设置补齐 + 运行自检 + 因子/回测扩展面板
 *
 * 背景：后端早已有 /api/models*、/api/auth/change_password、/api/auth/users、
 * /api/backups/export、POST /api/stock/trade 等能力，前端却没有入口；
 * agent.js 的报错甚至让用户去「系统设置 → 模型」这个当时不存在的页面。
 * 本模块把这些入口补齐，并把因子有效性、复利曲线、组合风控、运行自检接进视图。
 *
 * 请求层统一用 api.js 的 window.requestJSON，不另写一份 fetch 封装。
 */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const api = window.requestJSON;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  function toast(title, msg, type) {
    if (typeof window.showToast === "function") window.showToast(title, msg, type || "info");
    else console.log("[toast]", title, msg);
  }

  function setText(sel, text) { const el = $(sel); if (el) el.textContent = text; }

  let bound = false;
  let modelCfg = null;

  // 组合级开关：与后端 /stock/risk_switches 的白名单一一对应，
  // 不在这里另抄一份默认值，状态一律来自 /api/stock/decision_rules。
  const RISK_SWITCHES = [
    ["vol_target_enabled", "波动率目标", "高波动时自动压低仓位上限"],
    ["portfolio_drawdown_guard_enabled", "组合回撤护栏", "权益回撤超阈值时提示降仓"],
    ["pool_drift_alert_enabled", "四池偏离推送", "偏离超带宽时用钉钉提醒（默认关，避免与涨跌提醒重复）"],
  ];

  // ---------------------------------------------------------------- 模型配置
  async function loadModel() {
    try {
      const cfg = await api("/api/models");
      modelCfg = cfg;
      const provider = $("#modelProvider");
      if (provider) provider.value = cfg.provider || "openai";
      const name = $("#modelName");
      if (name) name.value = cfg.model || "";
      const base = $("#modelBase");
      if (base) base.value = cfg.base_url || "";
      setText("#modelState", cfg.api_key_set ? "已配置 Key" : "未配置 Key");
      setText("#modelHint",
        `当前 Key：${cfg.api_key_set ? esc(cfg.api_key || "已隐藏") : "未设置"}；` +
        `视觉模型：${esc(cfg.vision_model || "-")}；自定义模型 ${(cfg.custom_models || []).length} 个。` +
        " 保存时留空 Key 即保留原值；生产可用 MONITOR_STOCK_LLM_API_KEY 环境变量注入，不写入磁盘。");
    } catch (e) {
      setText("#modelState", "读取失败");
      setText("#modelHint", esc(e.message));
    }
  }

  async function saveModel() {
    if (!modelCfg) await loadModel();
    const key = ($("#modelKey")?.value || "").trim();
    const payload = Object.assign({}, modelCfg || {}, {
      provider: $("#modelProvider")?.value || "openai",
      model: ($("#modelName")?.value || "").trim(),
      base_url: ($("#modelBase")?.value || "").trim() || null,
    });
    // 留空时把打码值原样回传，后端识别后保留真实 Key
    payload.api_key = key || modelCfg?.api_key || "";
    try {
      await api("/api/models", { method: "POST", body: payload });
      const k = $("#modelKey"); if (k) k.value = "";
      toast("已保存", "模型配置已更新", "success");
      await loadModel();
    } catch (e) {
      toast("保存失败", e.message, "error");
    }
  }

  // ---------------------------------------------------------------- 账号/数据
  async function loadUsers() {
    const box = $("#adminUserList");
    if (!box) return;
    try {
      const r = await api("/api/auth/users");
      const rows = r?.data || [];
      box.innerHTML = `<div class="font-semibold text-ink-muted mb-1">账号（管理员可见，共 ${rows.length} 个）</div>` +
        rows.map((u) => `<div class="flex justify-between border-t border-line py-1">` +
          `<span>${esc(u.username)}${u.is_admin ? " · 管理员" : ""}</span>` +
          `<span>${u.active === false ? "已停用" : "正常"}</span></div>`).join("");
    } catch (e) {
      box.textContent = "账号列表仅管理员可见";
    }
  }

  async function changePassword() {
    const old_password = ($("#pwdOld")?.value || "");
    const new_password = ($("#pwdNew")?.value || "");
    if (!old_password || !new_password) { toast("提示", "请填写原口令和新口令"); return; }
    try {
      await api("/api/auth/change_password", { method: "POST", body: { old_password, new_password } });
      toast("已修改", "口令已更新，其它设备需重新登录", "success");
      const a = $("#pwdOld"), b = $("#pwdNew"); if (a) a.value = ""; if (b) b.value = "";
    } catch (e) { toast("修改失败", e.message, "error"); }
  }

  async function exportData() {
    try {
      const data = await api("/api/backups/export");
      window.downloadJSON(data, `monitor_stock_export_${new Date().toISOString().slice(0, 10)}.json`);
      toast("已导出", "个人数据快照已下载", "success");
    } catch (e) { toast("导出失败", e.message, "error"); }
  }

  async function logout() {
    try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* 忽略 */ }
    window.location.href = "/login";
  }

  // ---------------------------------------------------------------- 手工记账
  async function submitTrade(forced) {
    const force = forced === true;  // 注意：按钮 click 事件会把 Event 传进来，不能用真假判断
    const payload = {
      code: ($("#trCode")?.value || "").trim(),
      side: $("#trSide")?.value || "buy",
      shares: Number($("#trShares")?.value || 0),
      date: ($("#trDate")?.value || "") || undefined,
    };
    const price = ($("#trPrice")?.value || "").trim();
    const fee = ($("#trFee")?.value || "").trim();
    if (price) payload.price = Number(price);
    if (fee) payload.fee = Number(fee);
    if (force) payload.force = true;
    if (!payload.code || !payload.shares) { setText("#trResult", "请填写代码与股数"); return; }
    try {
      const r = await api("/api/stock/trade", { method: "POST", body: payload });
      const rec = r?.data || {};
      const acct = r?.account || {};
      setText("#trResult",
        `已记账：${esc(rec.side || payload.side)} ${esc(rec.shares || payload.shares)} 股，` +
        `可用现金 ${acct.available_cash ?? "-"}，仓位 ${acct.position_ratio ?? "-"}` +
        (r?.warnings?.length ? `；提示：${esc(r.warnings.join("；"))}` : ""));
      toast("已记账", "持仓与现金已同步", "success");
      if (typeof window.loadTrades === "function") window.loadTrades();
    } catch (e) {
      const msg = String(e.message || "");
      if (!force && msg.indexOf("违反四池规则") >= 0) {
        // 闸门拦下：先讲清违反了哪条，再由用户决定是否明确越过（不静默放行）
        setText("#trResult", `被四池规则拦下：${esc(msg)}`);
        if (window.confirm(`这笔买入违反四池规则：\n\n${msg}\n\n确认要越过规则记账吗？`)) {
          return submitTrade(true);
        }
        return;
      }
      setText("#trResult", `失败：${esc(msg)}`);
      toast("记账失败", msg, "error");
    }
  }

  // ---------------------------------------------------------------- 运行自检
  async function loadHealth() {
    const box = $("#healthBody");
    if (!box) return;
    const safe = async (url) => { try { return await api(url); } catch (e) { return { error: e.message }; } };
    const [health, rules, sched] = await Promise.all([
      safe("/api/stock/source_health"), safe("/api/stock/decision_rules"), safe("/api/scheduler/status"),
    ]);

    const sources = health?.sources || health?.data || null;
    const sourceRows = Array.isArray(sources) ? sources : Object.entries(sources || {}).map(([k, v]) => ({ name: k, ...(v || {}) }));
    const srcHtml = health?.error ? `<div class="hint">${esc(health.error)}</div>` :
      (sourceRows.length ? sourceRows.slice(0, 8).map((s) => {
        const state = s.healthy === false || s.ok === false ? "异常" : (s.cooldown ? "冷却" : "正常");
        const color = state === "异常" ? "text-down" : state === "冷却" ? "text-ink-muted" : "text-up";
        return `<div class="flex justify-between border-b border-line py-1"><span>${esc(s.name || s.source || "-")}</span>` +
          `<span class="${color}">${state}${s.latency_ms ? ` · ${esc(s.latency_ms)}ms` : ""}</span></div>`;
      }).join("") : "<div class='hint'>暂无数据源状态</div>");

    const ruleRows = rules?.error ? `<div class="hint">${esc(rules.error)}</div>` : [
      ["费率口径", rules?.fee_model || "-"],
      ["等效买入费率", rules?.effective_buy_fee_rate ?? "-"],
      ["佣金 / 最低", `${rules?.fees?.commission_rate ?? "-"} / ${rules?.fees?.commission_min ?? "-"}`],
      ["印花税 / 过户费", `${rules?.fees?.stamp_rate ?? "-"} / ${rules?.fees?.transfer_rate ?? "-"}`],
      ["组合回撤上限", `${rules?.risk?.max_portfolio_drawdown_pct ?? "-"}%`],
      ["单票权重上限", `${rules?.risk?.max_single_weight_pct ?? "-"}%`],
      ["规则文件", rules?.path || "-"],
    ].map(([k, v]) => `<div class="flex justify-between gap-2 border-b border-line py-1"><span>${esc(k)}</span><span class="text-right break-all">${esc(v)}</span></div>`).join("");

    const ruleSwitches = RISK_SWITCHES.map(([key, label, hint]) => {
      const on = !!rules?.monitor?.[key];
      return `<div class="flex justify-between gap-2 border-b border-line py-1.5 items-center">
        <span>${esc(label)}<span class="hint block text-[11px]">${esc(hint)}</span></span>
        <button type="button" data-risk-switch="${esc(key)}" data-on="${on ? "1" : "0"}"
          class="shrink-0 px-2.5 py-1 rounded-lg border border-line text-[12px] ${on ? "bg-brand-50 text-brand-600" : ""}">${on ? "已启用" : "关闭"}</button>
      </div>`;
    }).join("");

    const agent = sched?.agent || {};
    const tasks = agent.tasks || {};
    const agentRows = sched?.error ? `<div class="hint">${esc(sched.error)}</div>` : [
      ["调度器", sched?.running ? "运行中" : "未运行"],
      ["当前交易状态", sched?.trading_status || "-"],
      ["Agent 自动任务", agent.enabled ? "已启用" : "已停用"],
      ["播报前信号复核", agent.review_before_notify ? "已启用" : "已停用"],
      ...Object.entries(tasks).map(([k, v]) => [`${k} · ${v.scenario}`, v.at]),
      ["待处理事件", agent.queued_events ?? "-"],
    ].map(([k, v]) => `<div class="flex justify-between border-b border-line py-1"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");

    box.innerHTML = [
      ["行情数据源", srcHtml], ["决策规则（单一事实源）", ruleRows + ruleSwitches], ["Agent 自动调度", agentRows],
    ].map(([title, body]) =>
      `<div class="rounded-xl border border-line p-3"><div class="font-semibold mb-2">${esc(title)}</div>${body}</div>`
    ).join("");
  }

  // 事件委托挂在容器上：loadHealth 会重建 innerHTML，直接绑按钮会丢事件。
  async function onHealthClick(event) {
    const btn = event.target.closest("[data-risk-switch]");
    if (!btn || btn.disabled) return;
    const key = btn.getAttribute("data-risk-switch");
    const label = (RISK_SWITCHES.find(([k]) => k === key) || [key, key])[1];
    const next = btn.getAttribute("data-on") !== "1";
    btn.disabled = true;
    try {
      await api("/api/stock/risk_switches", { method: "PUT", body: { [key]: next } });
      toast("已保存", `${label}已${next ? "启用" : "关闭"}`, "success");
      await loadHealth();
    } catch (err) {
      toast("保存失败", err.message || String(err), "error");
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 因子扩展
  let factorExtrasReady = false;
  function initFactorExtras() {
    if (factorExtrasReady) { refreshFactorSelect(); return; }
    const view = $("#view-factors");
    if (!view) return;
    const box = document.createElement("div");
    box.className = "mt-5 grid lg:grid-cols-2 gap-5";
    box.innerHTML = `
      <div class="bg-white border border-line rounded-2xl shadow-card p-5 space-y-3">
        <div><h3 class="font-bold text-[15px]">载入监控 / 有效性检验</h3>
          <p class="text-[12px] hint mt-1">因子不再只是回测用：可一键设为监控策略，并用 IC / IR / 分层回测 / 样本外验证检验是否真的有效。</p></div>
        <div><label class="text-[12px] text-ink-muted">选择因子</label>
          <select id="fxFactor" class="mt-1 w-full px-3 py-2.5 rounded-lg border border-line bg-white"></select></div>
        <div><label class="text-[12px] text-ink-muted">检验标的</label>
          <input id="fxCode" placeholder="如 600519" class="mt-1 w-full px-3 py-2.5 rounded-lg border border-line" /></div>
        <div class="flex flex-wrap gap-2">
          <button id="fxLoadMonitor" class="px-4 py-2 rounded-lg bg-brand-500 text-white text-[13px] font-semibold hover:bg-brand-600">载入监控</button>
          <button id="fxValidate" class="px-4 py-2 rounded-lg border border-line text-[13px] font-semibold hover:bg-brand-50">有效性检验</button>
        </div>
        <div id="fxMonitorNow" class="text-[12px] hint"></div>
      </div>
      <div class="bg-white border border-line rounded-2xl shadow-card p-5 space-y-3">
        <h3 class="font-bold text-[15px]">检验结果</h3>
        <div id="fxResult" class="text-[12.5px] leading-relaxed">选一个因子并点「有效性检验」。</div>
      </div>`;
    view.appendChild(box);
    factorExtrasReady = true;
    $("#fxLoadMonitor").addEventListener("click", loadIntoMonitor);
    $("#fxValidate").addEventListener("click", validateFactor);
    refreshFactorSelect();
  }

  async function refreshFactorSelect() {
    const sel = $("#fxFactor");
    if (!sel) return;
    try {
      const r = await api("/api/stock/strategy/list");
      const rows = r?.data || [];
      sel.innerHTML = rows.length
        ? rows.map((f) => `<option value="${esc(f.name)}">${esc(f.name)}</option>`).join("")
        : `<option value="">（还没有保存的因子）</option>`;
      const now = await api("/api/stock/monitor_strategy");
      setText("#fxMonitorNow", `当前监控策略：${now?.name || "系统默认监控"}`);
    } catch (e) { setText("#fxMonitorNow", `读取因子失败：${e.message}`); }
  }

  async function loadIntoMonitor() {
    const name = $("#fxFactor")?.value || "";
    if (!name) { toast("提示", "请先选择因子"); return; }
    try {
      await api("/api/stock/monitor_strategy", { method: "POST", body: { name } });
      toast("已载入", `因子「${name}」已成为当前监控策略`, "success");
      setText("#fxMonitorNow", `当前监控策略：${name}`);
    } catch (e) { toast("载入失败", e.message, "error"); }
  }

  async function validateFactor() {
    const name = $("#fxFactor")?.value || "";
    const code = ($("#fxCode")?.value || "").trim();
    if (!name || !code) { setText("#fxResult", "请选择因子并填写检验标的"); return; }
    setText("#fxResult", "检验中（会拉日线并做样本外切分）…");
    try {
      const r = await api("/api/stock/factor_validation", { method: "POST", body: { factor_name: name, code } });
      const m = r?.metrics || r || {};
      const rows = [
        ["样本数", m.samples], ["IC 均值", m.ic_mean], ["ICIR", m.icir],
        ["分层单调性", m.layer_monotonic], ["样本内收益", m.in_sample_return_pct],
        ["样本外收益", r?.walk_forward?.out_sample_return_pct ?? m.out_sample_return_pct],
        ["判定", r?.verdict || m.verdict],
      ];
      $("#fxResult").innerHTML = rows.map(([k, v]) =>
        `<div class="flex justify-between border-b border-line py-1"><span>${esc(k)}</span><span>${esc(v ?? "-")}</span></div>`
      ).join("") + `<div class="mt-2 hint">${esc(r?.note || "样本外收益显著低于样本内即为过拟合，不建议载入监控。")}</div>`;
    } catch (e) { setText("#fxResult", `检验失败：${e.message}`); }
  }

  // ---------------------------------------------------------------- 回测扩展
  window.__onBacktestResult = function (r) {
    const box = $("#backtestExtras");
    if (!box || !r || r.ok === false) return;
    const c = r.compound || {};
    box.innerHTML =
      `<div class="mt-5 rounded-2xl border border-line bg-white p-4 text-[12.5px]">` +
      `<div class="font-semibold mb-2">含再投资复利曲线（口径：${esc((r.fees || {}).fee_model || "ledger_aligned")}）</div>` +
      `<div class="grid md:grid-cols-3 gap-3">` +
      `<div>复利总收益 <b>${esc(c.compound_return_pct ?? r.return_pct)}%</b></div>` +
      `<div>不复投逐笔相加 <b>${esc(c.simple_sum_pct ?? "-")}%</b></div>` +
      `<div>费用口径 <b>与交易台账一致</b></div>` +
      `</div><div class="hint mt-2">曲线字段 equity[].compound；两者差距越大，说明收益再投入的贡献越显著。</div></div>`;
  };

  // ---------------------------------------------------------------- 绑定
  function bind() {
    if (bound) return;
    bound = true;
    $("#modelSave")?.addEventListener("click", saveModel);
    $("#modelRefresh")?.addEventListener("click", loadModel);
    $("#pwdSave")?.addEventListener("click", changePassword);
    $("#exportData")?.addEventListener("click", exportData);
    $("#logoutBtn")?.addEventListener("click", logout);
    $("#trSubmit")?.addEventListener("click", submitTrade);
    $("#healthRefresh")?.addEventListener("click", loadHealth);
    $("#healthBody")?.addEventListener("click", onHealthClick);
    const d = $("#trDate"); if (d && !d.value) d.value = new Date().toISOString().slice(0, 10);
  }

  window.__adminOnView = function (v) {
    if (v === "settings") {
      bind();
      loadModel();
      loadUsers();
      loadHealth();
    } else if (v === "factors") {
      initFactorExtras();
    }
  };
})();
