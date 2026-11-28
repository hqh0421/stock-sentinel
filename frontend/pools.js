/* 四池配置 · 前端逻辑
 *
 * 数据全部来自后端：
 *   GET  /api/stock/allocation                  四池实际占比 / 偏离 / 越界 / 再平衡
 *   POST /api/stock/allocation/classify         给持仓打池标签
 *   GET  /api/stock/proposals                   建议单
 *   POST /api/stock/proposals                   生成建议（proposed）
 *   POST /api/stock/proposals/{id}/accept       第一道确认
 *   POST /api/stock/proposals/{id}/execute      第二道确认（confirm=true）
 *   POST /api/stock/proposals/{id}/ignore       忽略
 *   GET  /api/stock/discipline                  纪律执行复盘（纯读聚合）
 *
 * 复用 window.requestJSON（api.js），不重复实现 fetch 封装。
 * 违规标签由后端返回（唯一事实源在 allocation_service.VIOLATION_LABELS），前端不另维护映射。
 */
"use strict";

const POOL_OPTIONS = [
  ["dividend", "股息"],
  ["catalyst", "催化波段成长"],
  ["long_track", "长赛道产业成长"],
  ["value", "低估价值"],
];

const POOL_STATE = { allocation: null, proposals: [], suggestions: null, discipline: null };

const POOL_CONFIDENCE = {
  high: ["置信度高", "bg-emerald-100 text-emerald-700"],
  medium: ["置信度中", "bg-amber-100 text-amber-700"],
  low: ["置信度低", "bg-gray-100 text-gray-600"],
  manual: ["人工指定", "bg-blue-100 text-blue-700"],
};

function poolStatusText(status) {
  return ({ under: "低配", over: "超配", in_band: "区间内" })[status] || status || "-";
}

function poolStatusTone(status) {
  if (status === "over") return "border-red-200 bg-red-50";
  if (status === "under") return "border-amber-200 bg-amber-50";
  return "border-emerald-200 bg-emerald-50";
}

function renderPoolsRisk() {
  const el = qs("#poolsRisk");
  if (!el) return;
  const a = POOL_STATE.allocation || {};
  if (!a.ok) {
    el.innerHTML = `<span class="text-amber-700">${escapeHTML(a.error || "分配不可用")}</span>`;
    return;
  }
  const cash = a.cash || {};
  const issues = a.issues || [];
  const unclassified = a.unclassified || {};
  const parts = [
    `<span class="font-semibold">现金 ${formatNumber(cash.actual_pct)}%</span>`,
    `<span class="text-ink-faint">目标 ${formatNumber(cash.target_pct, 0)}% · 下限 ${formatNumber(cash.min_pct, 0)}%</span>`,
    `<span class="text-ink-faint">总资金 ${formatCurrency(a.total_capital)} 元</span>`,
  ];
  if (unclassified.count) {
    parts.push(`<span class="text-amber-700">未分类 ${unclassified.count} 只 / ${formatNumber(unclassified.pct)}%</span>`);
  }
  parts.push(`<span class="${issues.length ? "text-red-600" : "text-emerald-600"}">${issues.length ? issues.length + " 项越界" : "无越界"}</span>`);
  let html = `<div class="flex flex-wrap items-center gap-x-4 gap-y-1">${parts.join("")}</div>`;
  if (issues.length) {
    html += `<ul class="mt-2 space-y-1 text-[12px] text-red-600">${issues
      .map((item) => `<li>· ${escapeHTML(item.message)}</li>`)
      .join("")}</ul>`;
  }
  el.innerHTML = html;
}

function renderPoolCards() {
  const el = qs("#poolsCards");
  if (!el) return;
  const a = POOL_STATE.allocation || {};
  if (!a.ok) { el.innerHTML = ""; return; }
  const cards = (a.pools || []).map((p) => {
    const sign = p.diff_pp > 0 ? "+" : "";
    const diffTone = p.diff_pp > 0 ? "text-red-600" : p.diff_pp < 0 ? "text-emerald-600" : "text-ink-muted";
    return `<div class="rounded-2xl border ${poolStatusTone(p.status)} p-3">
      <div class="flex items-center justify-between">
        <span class="font-semibold text-[13px]" data-term="${escapeHTML(p.pool)}">${escapeHTML(p.label)}</span>
        <span class="text-[11px] text-ink-faint">${poolStatusText(p.status)}</span>
      </div>
      <div class="text-[22px] font-bold mt-1">${formatNumber(p.actual_pct)}<span class="text-[12px] font-normal text-ink-faint">%</span></div>
      <div class="text-[11px] text-ink-faint">目标 ${formatNumber(p.target_pct, 0)}% · ${formatNumber(p.min_pct, 0)}~${formatNumber(p.max_pct, 0)}%</div>
      <div class="text-[11px] mt-1"><span data-term="drift">偏离</span> <span class="${diffTone}">${sign}${formatNumber(p.diff_pp)}pp</span></div>
      <div class="text-[11px] text-ink-faint"><span data-term="max_single">${p.count}/${p.max_positions} 只 · 单票 ${formatNumber(p.largest_single_pct)}% / 上限 ${formatNumber(p.max_single_pct)}%</span></div>
    </div>`;
  });
  const cash = a.cash || {};
  const cashStatus = cash.status === "below_floor" ? "低于下限" : cash.status === "above_target" ? "高于目标" : "区间内";
  cards.push(`<div class="rounded-2xl border border-line bg-white p-3">
    <div class="flex items-center justify-between">
      <span class="font-semibold text-[13px]">现金</span>
      <span class="text-[11px] text-ink-faint">${cashStatus}</span>
    </div>
    <div class="text-[22px] font-bold mt-1">${formatNumber(cash.actual_pct)}<span class="text-[12px] font-normal text-ink-faint">%</span></div>
    <div class="text-[11px] text-ink-faint">目标 ${formatNumber(cash.target_pct, 0)}% · <span data-term="cash_floor">下限 ${formatNumber(cash.min_pct, 0)}%</span></div>
    <div class="text-[11px] mt-1 text-ink-faint">${formatCurrency(cash.value)} 元</div>
  </div>`);
  el.innerHTML = cards.join("");
}

function renderPoolDiscipline() {
  const el = qs("#poolsDiscipline");
  if (!el) return;
  const d = POOL_STATE.discipline;
  if (!d || !d.ok) {
    el.innerHTML = `<div class="text-ink-faint">纪律执行复盘暂不可用（不影响四池配置）</div>`;
    return;
  }
  const rate = d.compliance_pct;
  const rateText = rate == null ? "暂无买入" : `${formatNumber(rate)}%`;
  const rateTone = rate == null ? "text-ink-faint" : (d.override_count ? "text-amber-600" : "text-emerald-600");
  const byCode = (d.by_code || []).map((c) =>
    `<span class="tag bg-ink-50 text-ink-muted">${escapeHTML(c.label)} ×${c.count}</span>`).join("");
  const byPool = (d.by_pool || []).map((p) =>
    `<span class="text-ink-faint">${escapeHTML(p.pool)} ${p.count} 笔</span>`).join(" · ");
  const recent = (d.recent || []).slice(0, 3).map((r) =>
    `<li>· ${escapeHTML(r.trade_date || r.created_at || "")} ${escapeHTML(r.name || r.secID || "")}：${escapeHTML((r.labels || []).join(" / "))}</li>`).join("");
  let html = `<div class="flex flex-wrap items-center justify-between gap-2">
      <span class="font-semibold text-[14px]">纪律执行 · 近 ${d.days} 天</span>
      <span class="text-[11px] text-ink-faint">${escapeHTML(d.scope || "")}</span>
    </div>
    <div class="flex flex-wrap items-center gap-x-5 gap-y-1 mt-2">
      <span>买入 <b>${d.buy_count}</b> 笔</span>
      <span>越过规则 <b class="${d.override_count ? "text-amber-600" : ""}">${d.override_count}</b> 笔</span>
      <span>遵守率 <b class="${rateTone}">${rateText}</b></span>
    </div>`;
  if (d.override_count) {
    html += `<div class="flex flex-wrap items-center gap-2 mt-2">${byCode}</div>`;
    if (byPool) html += `<div class="text-[11px] mt-1">${byPool}</div>`;
    if (recent) html += `<ul class="mt-2 space-y-1 text-[12px] text-ink-muted">${recent}</ul>`;
  } else {
    html += `<div class="text-[12px] text-emerald-600 mt-2">窗口内没有越过四池规则的记录</div>`;
  }
  html += renderAdherencePnl(d.pnl_by_adherence);
  el.innerHTML = html;
}

function renderAdherencePnl(pnl) {
  // 守规 vs 越权的落袋盈亏对比。口径由后端返回（scope），前端只渲染不重算。
  if (!pnl) return "";
  const line = (label, slot, tone) => {
    if (!slot) return `<div class="text-[12px] text-ink-faint">${label}：暂无已平仓记录</div>`;
    const toneCls = slot.realized_pnl == null ? "text-ink-faint" : (slot.realized_pnl >= 0 ? "text-emerald-600" : "text-red-600");
    const pctText = slot.pnl_pct == null ? "—" : `${slot.pnl_pct >= 0 ? "+" : ""}${formatNumber(slot.pnl_pct)}%`;
    return `<div class="text-[12px] flex flex-wrap items-center gap-x-3">
      <span class="${tone}">${label}</span>
      <span>落袋 <b class="${toneCls}">${slot.realized_pnl >= 0 ? "+" : ""}${formatCurrency(slot.realized_pnl)} 元</b></span>
      <span class="text-ink-faint">${slot.closed_shares} 股 / 收益率 ${pctText} / 胜率 ${formatNumber(slot.win_rate_pct, 0)}%</span>
    </div>`;
  };
  let html = `<div class="mt-3 pt-2 border-t border-line">
    <div class="text-[12px] font-semibold mb-1">落袋盈亏 · 守规 vs 越权</div>`;
  if (pnl.sufficient) {
    html += line("守规买入", pnl.compliant, "text-emerald-700")
      + line("越权买入", pnl.violated, "text-amber-600");
  } else {
    const one = pnl.compliant || pnl.violated;
    if (one) {
      // 单边有数据也照实显示，但明说另一边没有样本、不构成对比
      const label = pnl.compliant ? "守规买入" : "越权买入";
      const tone = pnl.compliant ? "text-emerald-700" : "text-amber-600";
      html += line(label, one, tone)
        + `<div class="text-[11px] text-ink-faint mt-1">另一边还没有已平仓卖出，暂不做对比（避免单边数字被误读）</div>`;
    } else {
      html += `<div class="text-[12px] text-ink-faint">窗口内还没有已平仓的卖出记录</div>`;
    }
  }
  if (pnl.unattributed) {
    html += `<div class="text-[11px] text-ink-faint mt-1">另有 ${pnl.unattributed.closed_shares} 股经持仓纠正、成本无法归因，不计入对比</div>`;
  }
  html += `<div class="text-[11px] text-ink-faint mt-1">${escapeHTML(pnl.scope || "")}</div>`;
  return html + `</div>`;
}

function renderPoolSuggestions() {
  const el = qs("#poolsSuggestions");
  if (!el) return;
  const items = POOL_STATE.suggestions;
  if (!items) { el.innerHTML = ""; return; }
  if (!items.length) {
    el.innerHTML = `<div class="text-[12px] text-emerald-600 border border-emerald-200 bg-emerald-50 rounded-xl px-3 py-2">所有持仓都已归池，无需初判</div>`;
    return;
  }
  el.innerHTML = items.map((s) => {
    const [confLabel, confTone] = POOL_CONFIDENCE[s.confidence] || POOL_CONFIDENCE.low;
    const reasons = (s.reasons || []).slice(0, 3).map((r) => `<li>· ${escapeHTML(r)}</li>`).join("");
    const head = s.suggested_pool
      ? `建议 <b>${escapeHTML(s.suggested_label)}</b>`
      : `<span class="text-amber-700">信号不足，需手动选择</span>`;
    const partial = s.partial
      ? `<span class="text-[11px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200" title="估值/财报未在预算内返回（${escapeHTML((s.missing_data || []).join("、"))}），结论可信度已下调">数据不全</span>`
      : "";
    const actions = s.suggested_pool
      ? `<button class="sug-accept px-2 py-1 rounded-lg bg-brand-500 text-white text-[12px]" data-secid="${escapeHTML(s.code)}" data-pool="${escapeHTML(s.suggested_pool)}">采纳</button>`
      : "";
    return `<div class="border ${s.needs_manual ? "border-amber-300" : "border-line"} rounded-xl px-3 py-2">
      <div class="flex items-center gap-2">
        <span class="text-[13px] font-medium truncate">${escapeHTML(s.name || s.code)}</span>
        <span class="text-[11px] text-ink-faint">${escapeHTML(s.code)}</span>
        <span class="text-[11px] px-1.5 py-0.5 rounded ${confTone}">${confLabel}</span>
        ${partial}
        <span class="ml-auto text-[12px]">${head}</span>
      </div>
      ${reasons ? `<ul class="text-[11px] text-ink-muted mt-1 space-y-0.5">${reasons}</ul>` : ""}
      ${s.note ? `<div class="text-[11px] text-ink-faint mt-1">${escapeHTML(s.note)}</div>` : ""}
      ${actions ? `<div class="mt-2">${actions}</div>` : ""}
    </div>`;
  }).join("");

  qsa(".sug-accept").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const secid = btn.dataset.secid;
      try {
        await window.requestJSON("/api/stock/allocation/classify", {
          method: "POST", body: { secID: secid, pool: btn.dataset.pool },
        });
        // 采纳即落库；同步移除该条建议，避免重复采纳
        POOL_STATE.suggestions = (POOL_STATE.suggestions || []).filter((s) => s.code !== secid);
        await loadPools();
      } catch (err) {
        alert("采纳失败：" + err.message);
      }
    });
  });
}

async function suggestPools() {
  const btn = qs("#poolsSuggest");
  const prev = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "分析中…"; }
  const el = qs("#poolsSuggestions");
  if (el) el.innerHTML = `<div class="text-[12px] text-ink-faint">正在读取估值与财报（外部数据源超时会自动降级，通常 10 秒内出结果）…</div>`;
  try {
    const res = await window.requestJSON("/api/stock/allocation/classify/suggest", {
      method: "POST", body: { only_unclassified: true, fundamentals: true },
    });
    POOL_STATE.suggestions = (res && res.items) || [];
    renderPoolSuggestions();
  } catch (err) {
    POOL_STATE.suggestions = null;
    if (el) el.innerHTML = `<div class="text-[12px] text-red-600">初判失败：${escapeHTML(err.message)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prev || "Agent 初判"; }
  }
}

function renderPoolHoldings() {
  const el = qs("#poolsHoldings");
  if (!el) return;
  const a = POOL_STATE.allocation || {};
  if (!a.ok) { el.innerHTML = `<div class="text-[12px] text-ink-faint">四池还算不出来。先去「系统设置」填总资金，再到「自选股」添加持股数量。</div>`; return; }
  const rows = a.holdings || [];
  if (!rows.length) { el.innerHTML = `<div class="text-[12px] text-ink-faint">还没有持仓。先在「自选股」填持股数量，这里就能把每只票分进四个池子。</div>`; return; }
  el.innerHTML = rows.map((h) => {
    const options = [`<option value="">未分类</option>`]
      .concat(POOL_OPTIONS.map(([key, label]) => `<option value="${key}" ${h.pool === key ? "selected" : ""}>${label}</option>`))
      .join("");
    const tone = h.pool ? "border-line" : "border-amber-300";
    return `<div class="flex items-center gap-2 border ${tone} rounded-xl px-2 py-1.5">
      <div class="flex-1 min-w-0">
        <div class="text-[13px] font-medium truncate">${escapeHTML(h.name || h.secID)}</div>
        <div class="text-[11px] text-ink-faint">${escapeHTML(h.secID)} · ${h.shares} 股 · ${formatCurrency(h.value)} 元${h.valued_by_cost ? " · 按成本估值" : ""}</div>
      </div>
      <select class="pool-pick px-2 py-1.5 rounded-lg border border-line bg-white text-[12px]" data-secid="${escapeHTML(h.secID)}">${options}</select>
      <button class="pool-save px-2 py-1.5 rounded-lg bg-brand-500 text-white text-[12px]" data-secid="${escapeHTML(h.secID)}">保存</button>
    </div>`;
  }).join("");

  qsa(".pool-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const secid = btn.dataset.secid;
      const pick = document.querySelector(`.pool-pick[data-secid="${CSS.escape(secid)}"]`);
      const pool = pick ? pick.value : "";
      if (!pool) { alert("请先选择一个池"); return; }
      try {
        await window.requestJSON("/api/stock/allocation/classify", { method: "POST", body: { secID: secid, pool } });
        await loadPools();
      } catch (err) {
        alert("分类失败：" + err.message);
      }
    });
  });
}

function renderPoolRebalance() {
  const el = qs("#poolsRebalance");
  if (!el) return;
  const a = POOL_STATE.allocation || {};
  const plan = (a.rebalance || []);
  if (!plan.length) {
    el.innerHTML = `<div class="text-[12px] text-ink-faint">四池均在 ±带宽内，无需调整</div>`;
    return;
  }
  el.innerHTML = plan.map((item) => `<div class="border border-line rounded-xl px-3 py-2">
    <div class="text-[13px] font-medium">${escapeHTML(item.label)} · ${item.action === "trim" ? "减" : "加"} ${formatNumber(Math.abs(item.diff_pp))}pp</div>
    <div class="text-[11px] text-ink-faint">约 ${formatCurrency(item.amount)} 元 · ${escapeHTML(item.reason)}</div>
  </div>`).join("");
}

function bindProposalActions() {
  qsa(".prop-accept").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await window.requestJSON(`/api/stock/proposals/${btn.dataset.id}/accept`, { method: "POST", body: {} });
        await loadPools();
      } catch (err) { alert("采纳失败：" + err.message); }
    });
  });
  qsa(".prop-ignore").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!window.confirm("忽略这条建议？不会产生任何交易。")) return;
      try {
        await window.requestJSON(`/api/stock/proposals/${btn.dataset.id}/ignore`, { method: "POST", body: {} });
        await loadPools();
      } catch (err) { alert("忽略失败：" + err.message); }
    });
  });
  qsa(".prop-execute").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!window.confirm("第二道确认：确认执行这条建议？执行后才会写入执行计划/交易台账。")) return;
      try {
        await window.requestJSON(`/api/stock/proposals/${btn.dataset.id}/execute`, {
          method: "POST", body: { confirm: true },
        });
        await loadPools();
      } catch (err) { alert("执行失败：" + err.message); }
    });
  });
}

function renderPoolProposals() {
  const el = qs("#poolsProposals");
  if (!el) return;
  const items = POOL_STATE.proposals || [];
  if (!items.length) { el.innerHTML = `<div class="text-[12px] text-ink-faint">暂无建议单</div>`; return; }
  const badge = { proposed: "待采纳", accepted: "已采纳", executed: "已执行", ignored: "已忽略" };
  const tone = {
    proposed: "bg-amber-100 text-amber-700",
    accepted: "bg-blue-100 text-blue-700",
    executed: "bg-emerald-100 text-emerald-700",
    ignored: "bg-gray-100 text-gray-600",
  };
  el.innerHTML = items.map((p) => {
    let actions = "";
    if (p.status === "proposed") {
      actions = `<button class="prop-accept px-2 py-1 rounded-lg bg-brand-500 text-white text-[12px]" data-id="${p.id}">采纳（第一道）</button>
        <button class="prop-ignore px-2 py-1 rounded-lg border border-line text-[12px]" data-id="${p.id}">忽略</button>`;
    } else if (p.status === "accepted") {
      actions = `<button class="prop-execute px-2 py-1 rounded-lg bg-red-600 text-white text-[12px]" data-id="${p.id}">确认执行（第二道）</button>
        <button class="prop-ignore px-2 py-1 rounded-lg border border-line text-[12px]" data-id="${p.id}">忽略</button>`;
    }
    return `<div class="border border-line rounded-xl px-3 py-2">
      <div class="flex items-center gap-2">
        <span class="text-[11px] px-1.5 py-0.5 rounded ${tone[p.status] || "bg-gray-100"}">${badge[p.status] || p.status}</span>
        <span class="text-[13px] font-medium">#${p.id} ${escapeHTML(p.title || "")}</span>
        <span class="text-[11px] text-ink-faint ml-auto">${escapeHTML(p.created_at || "")}</span>
      </div>
      ${p.analysis ? `<div class="text-[12px] text-ink-muted mt-1">${escapeHTML(p.analysis)}</div>` : ""}
      ${actions ? `<div class="mt-2 flex gap-2">${actions}</div>` : ""}
    </div>`;
  }).join("");
  bindProposalActions();
}

async function loadPools() {
  const [allocation, proposals, discipline] = await Promise.all([
    window.requestJSON("/api/stock/allocation"),
    window.requestJSON("/api/stock/proposals?limit=20"),
    // 复盘是附加信息，取不到也不能拖垮四池主面板
    window.requestJSON("/api/stock/discipline?days=30").catch(() => null),
  ]);
  POOL_STATE.allocation = allocation || {};
  POOL_STATE.proposals = (proposals && proposals.data) || [];
  POOL_STATE.discipline = discipline || null;
  renderPoolsRisk();
  renderPoolCards();
  renderPoolDiscipline();
  renderPoolSuggestions();
  renderPoolHoldings();
  renderPoolRebalance();
  renderPoolProposals();
}

async function makeRebalanceProposal() {
  const a = POOL_STATE.allocation || {};
  if (!a.ok) { alert(a.error || "分配不可用"); return; }
  const plan = a.rebalance || [];
  if (!plan.length) { alert("四池均在带宽内，没有需要调整的项"); return; }
  const analysis = plan
    .map((item) => `${item.label} ${item.action === "trim" ? "减" : "加"} ${formatNumber(Math.abs(item.diff_pp))}pp（约 ${formatCurrency(item.amount)} 元）`)
    .join("；");
  try {
    await window.requestJSON("/api/stock/proposals", {
      method: "POST",
      body: {
        kind: "rebalance",
        title: "四池再平衡建议",
        analysis,
        payload: { type: "rebalance", actions: plan, constraint: "只在本池内调拨，不跨池挪用" },
      },
    });
    await loadPools();
  } catch (err) {
    alert("生成失败：" + err.message);
  }
}

/* ------------------------------------------------------------------ *
 * 调整池比例（PUT /api/stock/pools）
 *
 * 只开放占比相关的 5 个字段；止盈/止损/加仓/时间止损属策略内容，
 * 仍需手工改 config/decision_rules.json，不在界面放开。
 * 前端先做一次合计校验给即时反馈，服务端仍会独立校验（不信任前端）。
 * ------------------------------------------------------------------ */

const POOL_EDITOR_FIELDS = ["target_pct", "min_pct", "max_pct", "max_single_pct", "max_positions"];

function poolEditorInput(key, field, value, isInt) {
  const step = isInt ? "1" : "0.5";
  return `<input type="number" step="${step}" min="0" max="100" inputmode="decimal"
    class="w-16 text-right px-1.5 py-1 rounded-lg border border-line bg-white"
    data-key="${key}" data-field="${field}" value="${formatNumber(value, isInt ? 0 : 2)}" />`;
}

function renderPoolsEditor() {
  const body = qs("#poolsEditorRows");
  if (!body) return;
  const a = POOL_STATE.allocation || {};
  const rows = (a.pools || []).map((p) => ({ key: p.pool, label: p.label, isCash: false, data: p }));
  const c = a.cash || {};
  rows.push({ key: "cash", label: "现金", isCash: true, data: c });

  body.innerHTML = rows.map((r) => `
    <tr class="border-t border-line">
      <td class="py-1.5 pr-2 whitespace-nowrap">${escapeHTML(r.label)}</td>
      <td class="py-1.5 px-1 text-right">${poolEditorInput(r.key, "target_pct", r.data.target_pct)}</td>
      <td class="py-1.5 px-1 text-right">${poolEditorInput(r.key, "min_pct", r.data.min_pct)}</td>
      <td class="py-1.5 px-1 text-right">${poolEditorInput(r.key, "max_pct", r.data.max_pct)}</td>
      <td class="py-1.5 px-1 text-right">${r.isCash ? '<span class="text-ink-faint">—</span>'
        : poolEditorInput(r.key, "max_single_pct", r.data.max_single_pct)}</td>
      <td class="py-1.5 pl-1 text-right">${r.isCash ? '<span class="text-ink-faint">—</span>'
        : poolEditorInput(r.key, "max_positions", r.data.max_positions, true)}</td>
    </tr>`).join("");

  body.querySelectorAll("input").forEach((el) => el.addEventListener("input", updatePoolsEditorTotal));
  const err = qs("#poolsEditorError");
  if (err) err.textContent = "";
  updatePoolsEditorTotal();
}

function readPoolsEditor() {
  const out = { pools: {}, cash: {} };
  document.querySelectorAll("#poolsEditorRows input").forEach((el) => {
    const num = Number(el.value);
    if (!Number.isFinite(num)) return;
    if (el.dataset.key === "cash") out.cash[el.dataset.field] = num;
    else {
      out.pools[el.dataset.key] = out.pools[el.dataset.key] || {};
      out.pools[el.dataset.key][el.dataset.field] = num;
    }
  });
  return out;
}

function updatePoolsEditorTotal() {
  const el = qs("#poolsEditorTotal");
  if (!el) return true;
  const { pools, cash } = readPoolsEditor();
  const total = Object.values(pools).reduce((sum, p) => sum + (Number(p.target_pct) || 0), 0)
    + (Number(cash.target_pct) || 0);
  const ok = Math.abs(total - 100) <= 0.5;
  el.innerHTML = `四池 + 现金合计 <span class="font-semibold ${ok ? "text-emerald-600" : "text-red-600"}">${formatNumber(total)}%</span>`
    + (ok ? "" : " · 必须等于 100%，否则占比/偏离/越界全部失真");
  return ok;
}

function openPoolsEditor() {
  const panel = qs("#poolsEditor");
  if (!panel) return;
  if (!(POOL_STATE.allocation || {}).ok) { alert("分配不可用（多半是总资金未设置），无法编辑比例"); return; }
  renderPoolsEditor();
  panel.classList.remove("hidden");
}

function closePoolsEditor() {
  const panel = qs("#poolsEditor");
  if (panel) panel.classList.add("hidden");
}

async function savePoolsEditor() {
  const btn = qs("#poolsEditorSave");
  const err = qs("#poolsEditorError");
  if (!updatePoolsEditorTotal()) {
    if (err) err.textContent = "合计不等于 100%，已阻止保存";
    return;
  }

  const { pools, cash } = readPoolsEditor();
  const a = POOL_STATE.allocation || {};
  const before = {};
  (a.pools || []).forEach((p) => { before[p.pool] = p; });

  // 只提交改动过的字段，避免无意义重写规则文件
  const poolsPatch = {};
  Object.entries(pools).forEach(([key, vals]) => {
    const prev = before[key] || {};
    const diff = {};
    POOL_EDITOR_FIELDS.forEach((f) => {
      if (Number(vals[f]) !== Number(prev[f])) diff[f] = Number(vals[f]);
    });
    if (Object.keys(diff).length) poolsPatch[key] = diff;
  });
  const cashPrev = a.cash || {};
  const cashPatch = {};
  ["target_pct", "min_pct", "max_pct"].forEach((f) => {
    if (Number(cash[f]) !== Number(cashPrev[f])) cashPatch[f] = Number(cash[f]);
  });

  const payload = {};
  if (Object.keys(poolsPatch).length) payload.pools = poolsPatch;
  if (Object.keys(cashPatch).length) payload.cash = cashPatch;
  if (!payload.pools && !payload.cash) {
    if (err) err.textContent = "没有改动";
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
  if (err) err.textContent = "";
  try {
    const res = await window.requestJSON("/api/stock/pools", { method: "PUT", body: payload });
    if (res && res.allocation) POOL_STATE.allocation = res.allocation;
    await loadPools();
    closePoolsEditor();
  } catch (e) {
    if (err) err.textContent = e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "保存"; }
  }
}

window.__poolsOnView = function (view) {
  if (view !== "pools") return;
  loadPools().catch((err) => {
    const el = qs("#poolsRisk");
    if (el) el.innerHTML = `<span class="text-red-600">加载失败：${escapeHTML(err.message)}</span>`;
  });
};

document.addEventListener("DOMContentLoaded", function () {
  const refresh = qs("#poolsRefresh");
  if (refresh) refresh.addEventListener("click", () => loadPools().catch((err) => alert("刷新失败：" + err.message)));
  const make = qs("#poolsMakeProposal");
  if (make) make.addEventListener("click", makeRebalanceProposal);
  const suggest = qs("#poolsSuggest");
  if (suggest) suggest.addEventListener("click", suggestPools);
  const editToggle = qs("#poolsEditToggle");
  if (editToggle) editToggle.addEventListener("click", () => {
    const panel = qs("#poolsEditor");
    if (panel && !panel.classList.contains("hidden")) closePoolsEditor();
    else openPoolsEditor();
  });
  const saveBtn = qs("#poolsEditorSave");
  if (saveBtn) saveBtn.addEventListener("click", savePoolsEditor);
  const cancelBtn = qs("#poolsEditorCancel");
  if (cancelBtn) cancelBtn.addEventListener("click", closePoolsEditor);
});
