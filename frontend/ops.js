/* 绩效复盘 + 运维面板（调度 / 告警历史 / 备份 / 审计）
 *
 * 为什么单独一个文件：app.js 已经 1200+ 行，绩效与运维是相对独立的关注点，
 * 拆出来避免相互影响，也便于按视图懒加载（首屏不多发请求）。
 *
 * 依赖 app.js 暴露在全局的 switchView 钩子 window.__opsOnView。
 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  // 统一请求层见 api.js（原实现已删除，避免三份重复的 fetch 封装）
  const api = window.requestJSON;

  function toast(title, msg, type) {
    if (typeof window.showToast === "function") { window.showToast(title, msg, type || "info"); return; }
    console.log("[toast]", title, msg);
  }

  const num = (v, d) => (v == null || Number.isNaN(Number(v)) ? "-" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d }));
  const money = (v) => (v == null ? "-" : "¥" + num(v, 0));
  const pct = (v) => (v == null ? "-" : (v > 0 ? "+" : "") + num(v, 2) + "%");
  const tone = (v) => (v == null ? "text-ink-muted" : v > 0 ? "text-up" : v < 0 ? "text-down" : "text-ink-muted");

  let isAdmin = false;
  let perfLoaded = false;
  let settingsLoaded = false;

  /* ============================================================ 绩效 */

  function perfCard(label, value, sub, toneCls) {
    return `<div class="bg-white border border-line rounded-2xl shadow-card p-4">
      <div class="text-[12px] text-ink-muted">${esc(label)}</div>
      <div class="text-[20px] font-extrabold mt-1 ${toneCls || ""}">${value}</div>
      ${sub ? `<div class="text-[11px] text-ink-faint mt-1">${sub}</div>` : ""}
    </div>`;
  }

  function drawEquity(points, startEquity) {
    const W = 900, H = 300, L = 58, R = 18, T = 16, B = 30;
    const equity = points.map((p) => Number(p.equity) || 0);
    const bench = points.map((p) => (p.benchmark == null ? null : Number(p.benchmark) * startEquity));
    const valid = equity.concat(bench.filter((v) => v != null));
    let min = Math.min.apply(null, valid);
    let max = Math.max.apply(null, valid);
    if (!isFinite(min) || !isFinite(max)) return `<div class="text-ink-faint text-[13px]">数据不足</div>`;
    if (min === max) { min = min * 0.99; max = max * 1.01; }
    const pad = (max - min) * 0.08;
    min -= pad; max += pad;

    const x = (i) => L + (W - L - R) * (points.length <= 1 ? 0 : i / (points.length - 1));
    const y = (v) => T + (H - T - B) * (1 - (v - min) / (max - min));

    const line = (arr) => arr.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(" ");

    const eqPath = line(equity);
    const benchPath = line(bench);
    const areaPath = eqPath ? `${L},${H - B} ${eqPath} ${x(points.length - 1).toFixed(1)},${H - B}` : "";

    // 网格 + Y 轴
    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const gy = T + (H - T - B) * (i / 4);
      const val = max - ((max - min) * i) / 4;
      const label = Math.abs(val) >= 10000 ? (val / 10000).toFixed(1) + "万" : val.toFixed(0);
      grid += `<line x1="${L}" y1="${gy.toFixed(1)}" x2="${W - R}" y2="${gy.toFixed(1)}" stroke="#e5e7eb" stroke-width="1"/>`;
      grid += `<text x="${L - 8}" y="${(gy + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="#94a3b8">${label}</text>`;
    }

    // X 轴日期（最多 5 个）
    const ticks = points.length <= 5 ? points.map((_, i) => i) : [0, Math.floor((points.length - 1) / 4), Math.floor((points.length - 1) / 2), Math.floor(((points.length - 1) * 3) / 4), points.length - 1];
    let axis = "";
    [...new Set(ticks)].forEach((i) => {
      axis += `<text x="${x(i).toFixed(1)}" y="${H - 10}" text-anchor="middle" font-size="10" fill="#94a3b8">${esc(String(points[i].date || "").slice(5))}</text>`;
    });

    const last = points[points.length - 1];
    const lastColor = last.equity >= startEquity ? "#e23d4e" : "#16a34a";

    return `<svg viewBox="0 0 ${W} ${H}" class="w-full" style="height:auto" role="img" aria-label="账户净值曲线">
      <defs><linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#1677ff" stop-opacity="0.18"/><stop offset="100%" stop-color="#1677ff" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}${axis}
      ${areaPath ? `<polygon points="${areaPath}" fill="url(#eqFill)"/>` : ""}
      ${benchPath ? `<polyline points="${benchPath}" fill="none" stroke="#94a3b8" stroke-width="1.4" stroke-dasharray="5 4"/>` : ""}
      <polyline points="${eqPath}" fill="none" stroke="#1677ff" stroke-width="2.2" stroke-linejoin="round"/>
      <circle cx="${x(points.length - 1).toFixed(1)}" cy="${y(last.equity).toFixed(1)}" r="3.5" fill="${lastColor}"/>
    </svg>`;
  }

  async function loadPerformance(force) {
    const days = Number(($("#perfDays") && $("#perfDays").value) || 180);
    const cards = $("#perfCards");
    if (!cards) return;
    if (perfLoaded && !force) return;
    cards.innerHTML = `<div class="col-span-full text-[13px] text-ink-faint">计算中…（首次需要拉取日线，约 5–15 秒）</div>`;
    const chart = $("#perfChart");
    if (chart) chart.innerHTML = `<div class="text-ink-faint text-[13px]">计算中…</div>`;
    try {
      const data = await api(`/api/portfolio/performance?days=${days}`);
      if (!data || data.ok === false) {
        cards.innerHTML = "";
        if (chart) chart.innerHTML = `<div class="text-ink-faint text-[13px]">${esc((data && (data.message || data.reason)) || "暂无数据")}</div>`;
        return;
      }
      perfLoaded = true;
      const s = data.summary || {};
      cards.innerHTML = [
        perfCard("账户权益", money(s.equity_now), `区间起点 ${money(s.equity_start)}`),
        perfCard("区间收益", pct(s.total_return_pct), "现金 + 持仓市值", tone(s.total_return_pct)),
        perfCard("年化收益", pct(s.annualized_pct), "按 252 交易日折算", tone(s.annualized_pct)),
        perfCard("资金加权 XIRR", pct(s.xirr_pct), s.xirr_pct == null ? "需完整记账流水" : "按实际投入资金", tone(s.xirr_pct)),
        perfCard("最大回撤", (s.max_drawdown_pct == null ? "-" : "-" + num(s.max_drawdown_pct, 2) + "%"), "峰值回落", "text-down"),
        perfCard("夏普比率", s.sharpe == null ? "-" : num(s.sharpe, 2), "无风险利率按 0"),
        perfCard("胜率 / 盈亏比", (s.win_rate_pct == null ? "-" : num(s.win_rate_pct, 1) + "%") + (s.profit_factor == null ? "" : " / " + num(s.profit_factor, 2)), `${s.sell_count || 0} 笔卖出`),
        perfCard("沪深300", pct(s.benchmark_return_pct), "同区间基准", tone(s.benchmark_return_pct)),
        perfCard("超额收益", pct(s.excess_pct), "相对基准", tone(s.excess_pct)),
        perfCard("已实现盈亏", money(s.realized_pnl), "卖出兑现", tone(s.realized_pnl)),
        perfCard("累计手续费", money(s.total_fee), "含佣金/印花税/过户费"),
        perfCard("当前回撤", (s.current_drawdown_pct == null ? "-" : "-" + num(s.current_drawdown_pct, 2) + "%"), "距最近高点", "text-ink-muted"),
        perfCard("交易笔数", String(s.trade_count || 0), "含买入与卖出"),
      ].join("");

      if (chart) {
        chart.innerHTML = (data.points && data.points.length > 1)
          ? drawEquity(data.points, Number(s.equity_start) || 1)
          : `<div class="text-ink-faint text-[13px]">区间内交易日不足，画不出曲线</div>`;
      }

      const tbody = $("#perfPositions");
      if (tbody) {
        const rows = data.positions || [];
        tbody.innerHTML = rows.length ? rows.map((p) => `<tr>
          <td class="text-left">${esc(p.name || p.secID)}</td>
          <td class="text-right">${num(p.shares, 0)}</td>
          <td class="text-right">${num(p.cost, 3)}</td>
          <td class="text-right">${num(p.price, 3)}</td>
          <td class="text-right">${money(p.market_value)}</td>
          <td class="text-right ${tone(p.pnl)}">${p.pnl == null ? "-" : (p.pnl > 0 ? "+" : "") + num(p.pnl, 0)}</td>
        </tr>`).join("") : `<tr><td colspan="6" class="text-center muted py-6">当前无持仓</td></tr>`;
      }

      const notes = $("#perfNotes");
      if (notes) {
        const warns = (data.warnings || []).map((w) => `<p class="text-warn">⚠ ${esc(w)}</p>`).join("");
        notes.innerHTML = `
          <p>· 曲线区间：${esc((data.range || {}).start || "")} ~ ${esc((data.range || {}).end || "")}，共 ${num((data.points || []).length, 0)} 个交易日。</p>
          <p>· 曲线末端严格等于「当前可用资金 ${money(s.cash)} + 当前持仓市值」，与账户卡片一致。</p>
          <p>· 最大回撤按权益峰值回落计算；夏普用日收益年化（无风险利率按 0 处理）。</p>
          <p>· 胜率与盈亏比只统计带已实现盈亏的卖出记录。</p>
          ${warns}`;
      }
    } catch (err) {
      cards.innerHTML = `<div class="col-span-full rounded-xl border border-warn/40 bg-warn-soft px-4 py-3 text-[13px] text-[#8a6100]">绩效计算失败：${esc(err.message || err)}</div>`;
      if (chart) chart.innerHTML = `<div class="text-ink-faint text-[13px]">—</div>`;
    }
  }

  /* ============================================================ 调度 */

  function renderScheduler(st) {
    const pill = $("#schedPill");
    const summary = $("#schedSummary");
    const tbody = $("#schedUsers");
    if (!pill || !summary || !tbody) return;
    const running = !!st.running;
    pill.className = "tag " + (running ? "bg-down/10 text-down" : "bg-ink-50 text-ink-muted");
    pill.textContent = running ? "运行中" : "已停止";

    const trading = st.trading_now
      ? `<span class="text-up font-semibold">交易时段</span>`
      : `<span class="text-ink-muted">非交易时段（${esc(st.trading_status || "")}）</span>`;
    summary.innerHTML = `
      调度器 ${running ? "在后台每分钟巡检" : "未运行"}，当前 ${trading}；
      上次巡检 ${esc(st.last_tick_at || "-")}，最近一次备份 ${esc(st.last_backup_date || "未备份")}。
      ${st.last_error ? `<span class="text-warn">最近错误：${esc(st.last_error)}</span>` : ""}
      非交易时段不抓行情（数据不变化，抓了也没意义）。`;

    const users = st.users || [];
    tbody.innerHTML = users.length ? users.map((u) => {
      const okText = u.last_run_ok === true ? `<span class="text-down">成功</span>` : (u.last_run_ok === false ? `<span class="text-warn">失败</span>` : "-");
      return `<tr>
        <td class="text-left">${esc(u.username || u.user_id)}</td>
        <td class="text-center">${u.enabled ? "✅" : "—"}</td>
        <td class="text-right">${num(u.interval_sec, 0)}s</td>
        <td class="text-left">${esc(u.last_run_at || "尚未运行")}</td>
        <td class="text-right">${u.last_elapsed == null ? "-" : num(u.last_elapsed, 1) + "s"}</td>
        <td class="text-left">${okText} <span class="text-ink-faint">${esc(u.last_notify || "")}${u.last_rows == null ? "" : " · " + u.last_rows + " 只"}</span></td>
      </tr>`;
    }).join("") : `<tr><td colspan="6" class="text-center muted py-4">没有启用中的账号</td></tr>`;
  }

  async function loadScheduler() {
    try {
      const st = await api("/api/scheduler/status");
      renderScheduler(st || {});
    } catch (err) {
      const summary = $("#schedSummary");
      if (summary) summary.textContent = "调度状态读取失败：" + (err.message || err);
    }
  }

  /* ============================================================ 告警历史 */

  async function loadAlertLogs() {
    const box = $("#alertLogList");
    if (!box) return;
    const filter = ($("#alertLogFilter") || {}).value || "";
    box.innerHTML = `<div class="text-ink-faint text-[12.5px]">加载中…</div>`;
    try {
      const q = filter === "failed" ? "?limit=80&only_failed=true" : "?limit=80";
      const data = await api("/api/alert_logs" + q);
      const rows = (data && data.data) || [];
      if (!rows.length) { box.innerHTML = `<div class="text-ink-faint text-[12.5px] py-4 text-center">还没有推送记录。触发一次监控或点「发送测试提醒」后会出现在这里。</div>`; return; }
      box.innerHTML = rows.map((r) => {
        const okCls = r.ok ? "text-down" : "text-warn";
        const body = String(r.content || "").replace(/\n/g, " ").slice(0, 140);
        return `<div class="rounded-xl border border-line px-3 py-2">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-ink-faint">${esc(r.created_at)}</span>
            <span class="tag bg-brand-50 text-brand-600">${esc(r.channel || "-")}</span>
            <span class="font-semibold">${esc(r.title || "-")}</span>
            <span class="${okCls} text-[11.5px]">${r.ok ? "✓ 已送达" : "✕ 失败"}</span>
            <span class="text-ink-faint text-[11.5px]">${r.item_count || 0} 条 · ${esc(r.source || "-")}</span>
          </div>
          <div class="text-ink-muted mt-1">${esc(body)}</div>
          ${r.error ? `<div class="text-warn mt-1 text-[11.5px]">${esc(r.error)}</div>` : ""}
        </div>`;
      }).join("");
    } catch (err) {
      box.innerHTML = `<div class="text-warn text-[12.5px]">读取失败：${esc(err.message || err)}</div>`;
    }
  }

  /* ============================================================ 备份 */

  function sizeText(bytes) {
    const n = Number(bytes) || 0;
    if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + " MB";
    if (n > 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  async function loadBackups() {
    if (!isAdmin) return;
    const tbody = $("#backupList");
    const stat = $("#backupStat");
    if (!tbody) return;
    try {
      const data = await api("/api/backups");
      const rows = (data && data.data) || [];
      if (stat) stat.textContent = "最近备份：" + ((data && data.last_backup_date) || "无");
      tbody.innerHTML = rows.length ? rows.map((b) => `<tr>
        <td class="text-left font-mono text-[11.5px]">${esc(b.name)}</td>
        <td class="text-right">${sizeText(b.size)}</td>
        <td class="text-left">${esc(b.created_at)}</td>
        <td class="text-right space-x-2">
          <a class="text-brand-600 hover:underline" href="/api/backups/download/${encodeURIComponent(b.name)}">下载</a>
          <button class="text-warn hover:underline" data-restore="${esc(b.name)}">恢复</button>
        </td>
      </tr>`).join("") : `<tr><td colspan="4" class="text-center muted py-4">还没有备份</td></tr>`;
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="4" class="text-center text-warn py-4">读取失败：${esc(err.message || err)}</td></tr>`;
    }
  }

  /* ============================================================ 审计 */

  function formatAuditDetail(raw) {
    if (!raw) return "";
    let obj;
    try { obj = JSON.parse(raw); } catch (e) { return String(raw).slice(0, 200); }
    const fmt = (v) => (v == null ? "-" : typeof v === "object" ? JSON.stringify(v) : String(v));
    const parts = [];
    Object.entries(obj.changes || {}).forEach(([section, fields]) => {
      Object.entries(fields || {}).forEach(([name, value]) => {
        if (value && typeof value === "object" && "from" in value && "to" in value) {
          parts.push(`${section}.${name}: ${fmt(value.from)} → ${fmt(value.to)}`);
        } else {
          Object.entries(value || {}).forEach(([field, delta]) => {
            parts.push(`${section}.${name}.${field}: ${fmt(delta.from)} → ${fmt(delta.to)}`);
          });
        }
      });
    });
    return parts.join("；") || String(raw).slice(0, 200);
  }

  async function loadAudit() {
    if (!isAdmin) return;
    const box = $("#auditList");
    if (!box) return;
    const kw = (($("#auditPathFilter") || {}).value || "").trim();
    box.innerHTML = `<div class="text-ink-faint">加载中…</div>`;
    try {
      const q = "?limit=120&all_users=true" + (kw ? "&path_like=" + encodeURIComponent(kw) : "");
      const data = await api("/api/audit_logs" + q);
      const rows = (data && data.data) || [];
      box.innerHTML = rows.length ? rows.map((r) => `<div class="flex flex-wrap items-center gap-2 border-b border-line/60 pb-1.5">
        <span class="text-ink-faint">${esc(r.created_at)}</span>
        <span class="tag bg-ink-50 text-ink-muted">${esc(r.method)}</span>
        <span class="font-mono text-[11.5px]">${esc(r.path)}</span>
        <span class="${r.status >= 400 ? "text-warn" : "text-down"}">${r.status}</span>
        <span class="text-ink-faint">uid=${esc(r.user_id == null ? "-" : r.user_id)} · ${esc(r.ip || "-")}</span>
        ${r.detail ? `<div class="w-full pl-2 text-[11.5px] text-ink-muted break-all">变更：${esc(formatAuditDetail(r.detail))}</div>` : ""}
      </div>`).join("") : `<div class="text-ink-faint">暂无记录</div>`;
    } catch (err) {
      box.innerHTML = `<div class="text-warn">读取失败：${esc(err.message || err)}</div>`;
    }
  }

  /* ============================================================ 绑定 */

  function bind() {
    const perfBtn = $("#perfRefresh");
    if (perfBtn) perfBtn.addEventListener("click", () => loadPerformance(true));
    const perfDays = $("#perfDays");
    if (perfDays) perfDays.addEventListener("change", () => loadPerformance(true));

    const schedRun = $("#schedRun");
    if (schedRun) schedRun.addEventListener("click", async () => {
      const old = schedRun.textContent;
      schedRun.disabled = true; schedRun.textContent = "运行中…";
      try {
        const r = await api("/api/scheduler/run", { method: "POST", body: {} });
        const triggered = (r && r.triggered) || [];
        toast(r && r.ok ? "已运行" : "运行完成但有问题",
          (r && r.rows != null ? `扫描 ${r.rows} 只，` : "") + (triggered.length ? `触发 ${triggered.length} 个信号` : "无信号触发") +
          (r && r.notify && r.notify.reason ? `（${r.notify.reason}）` : ""),
          triggered.length ? "warning" : "info");
        await Promise.allSettled([loadScheduler(), loadAlertLogs()]);
      } catch (err) {
        toast("运行失败", err.message || String(err), "error");
      } finally {
        schedRun.disabled = false; schedRun.textContent = old;
      }
    });

    const alertRefresh = $("#alertLogRefresh");
    if (alertRefresh) alertRefresh.addEventListener("click", loadAlertLogs);
    const alertFilter = $("#alertLogFilter");
    if (alertFilter) alertFilter.addEventListener("change", loadAlertLogs);

    const backupRun = $("#backupRun");
    if (backupRun) backupRun.addEventListener("click", async () => {
      backupRun.disabled = true; backupRun.textContent = "备份中…";
      try {
        const r = await api("/api/backups/run", { method: "POST", body: {} });
        toast("备份完成", (r && r.backup && r.backup.name) || "", "info");
        await loadBackups();
      } catch (err) {
        toast("备份失败", err.message || String(err), "error");
      } finally {
        backupRun.disabled = false; backupRun.textContent = "立即备份";
      }
    });

    const backupList = $("#backupList");
    if (backupList) backupList.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-restore]");
      if (!btn) return;
      const name = btn.getAttribute("data-restore");
      if (!confirm(`确定用备份「${name}」覆盖当前数据库吗？\n\n覆盖会丢掉备份之后的所有改动。系统会先自动保存一份当前库作为安全快照。`)) return;
      try {
        const r = await api("/api/backups/restore", { method: "POST", body: { name } });
        toast("已恢复", `已用 ${r.restored} 覆盖当前库；恢复前快照 ${r.safety_backup}`, "warning");
        await Promise.allSettled([loadBackups(), loadScheduler()]);
      } catch (err) {
        toast("恢复失败", err.message || String(err), "error");
      }
    });

    const auditRefresh = $("#auditRefresh");
    if (auditRefresh) auditRefresh.addEventListener("click", loadAudit);
    const auditFilter = $("#auditPathFilter");
    if (auditFilter) auditFilter.addEventListener("keydown", (e) => { if (e.key === "Enter") loadAudit(); });
  }

  async function detectAdmin() {
    try {
      const me = await api("/api/auth/me");
      isAdmin = !!(me && me.user && me.user.is_admin);
    } catch (e) {
      isAdmin = false;
    }
    if (isAdmin) {
      const b = $("#backupCard"); if (b) b.classList.remove("hidden");
      const a = $("#auditCard"); if (a) a.classList.remove("hidden");
    }
  }

  window.__opsOnView = function (view) {
    if (view === "performance") loadPerformance(false);
    if (view === "settings") {
      loadScheduler();
      loadAlertLogs();
      if (isAdmin) { loadBackups(); loadAudit(); }
      if (!settingsLoaded) { settingsLoaded = true; }
    }
  };

  // 设置页里调度状态会随时间变化，进入设置页后每 60 秒静默刷新一次
  setInterval(() => {
    const el = document.getElementById("view-settings");
    if (el && el.classList.contains("active")) loadScheduler();
  }, 60000);

  bind();
  detectAdmin().then(() => {
    // 默认停在总览：不主动拉绩效，等用户点进去再算（首次要拉日线）
    if (document.getElementById("view-settings")?.classList.contains("active")) {
      loadScheduler(); loadAlertLogs();
      if (isAdmin) { loadBackups(); loadAudit(); }
    }
  });
})();
