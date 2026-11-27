/* ============================================================
   股票监控台 · 前端逻辑（对接真实后端 /api）
   设计对齐 stock-monitor-redesign 原型（现代浅色数据看板）
   ============================================================ */
"use strict";

/* 401 统一拦截：会话失效/未登录时跳转登录页（鉴权门禁由后端中间件实现） */
(function () {
  const _orig = window.fetch.bind(window);
  window.fetch = async function (url, opts) {
    const res = await _orig(url, opts);
    if (res.status === 401 && !String(url).includes("/api/auth/")) {
      window.location.href = "/login";
    }
    return res;
  };
})();

const qs = (sel) => document.querySelector(sel);
const qsa = (sel) => Array.from(document.querySelectorAll(sel));

/* ---------------- 工具函数 ---------------- */
function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
// 账户卡片/环境卡片里一直用短名 esc()，但本文件只定义了 escapeHTML——
// 结果是 renderAcctStatus 一进就 ReferenceError，被上层 catch 成"加载设置失败"，
// 账户市值/持仓概述整块空白。这里补个别名，别再各自造轮子。
const esc = escapeHTML;
function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "-";
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : String(value);
}
function formatValue(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : formatNumber(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return `${value.length} 条`;
  if (typeof value === "object") return "对象";
  return String(value);
}
function formatPercent(value) {
  if (value === null || value === undefined || value === "") return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return `${num.toFixed(2)}%`;
}
function formatCurrency(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtAmount(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return "-";
  if (num >= 1e8) return `${(num / 1e8).toFixed(2)} 亿`;
  if (num >= 1e4) return `${(num / 1e4).toFixed(2)} 万`;
  return formatNumber(num, 0);
}
function yuan(value) { const n = Number(value); return Number.isFinite(n) ? "¥" + formatCurrency(n) : "-"; }

function parseMoneyInput(value) {
  const raw = String(value ?? "").trim().replace(",", ".");
  if (!raw) return 0;
  const num = Number(raw);
  if (!Number.isFinite(num) || num < 0) throw new Error("成本价必须是有效的非负数字");
  return Math.round(num * 100) / 100;
}
function parseNonNegativeAmount(value, label) {
  const raw = String(value ?? "").trim().replace(",", ".");
  if (!raw) return 0;
  const num = Number(raw);
  if (!Number.isFinite(num) || num < 0) throw new Error(`${label}必须是有效的非负数字`);
  return Math.round(num * 100) / 100;
}

/* 颜色 / 涨跌工具（A股：涨红跌绿） */
function clsOf(p) { return p > 0 ? "up" : (p < 0 ? "down" : "flat"); }
function signOf(p) { return p > 0 ? "+" : ""; }
function colorOf(p) { return p > 0 ? "#e23d4e" : (p < 0 ? "#16a34a" : "#64748b"); }
function fmt(n, d = 2) { return Number(n).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d }); }

/* 文本映射 */
const stageTextMap = { unknown:"未知", sideways:"震荡", uptrend:"上升趋势", downtrend:"下降趋势", accumulation:"底部吸筹", washout:"洗盘", distribution:"顶部派发" };
const actionTextMap = { watch:"观望", hold:"持有", hold_add:"持有/加仓", hold_watch:"持有/观察", buy:"买入", buy_dip:"低吸", probe_buy:"试探买入", trend_follow:"顺势跟进", accumulate:"逢低布局", reduce:"减仓", stop_loss:"止损", exit:"清仓", focus:"重点关注", data_only:"仅数据" };
const signalTextMap = { ma_break_up:"站上 MA20", ma_break_down:"跌破 MA20", rsi_oversold:"RSI 超卖", rsi_overbought:"RSI 超买", macd_golden_cross:"MACD 金叉", macd_dead_cross:"MACD 死叉" };
// 四池是唯一用户口径；long/mid_long/swing 只是后端卖出参数的内部风格，前端不再暴露。
const POOL_LABELS = { dividend: "股息", catalyst: "催化波段", long_track: "长赛道", value: "低估价值" };
function poolText(v) { return POOL_LABELS[v] || "未分类"; }
function toStageText(v) { return stageTextMap[v] || v || "-"; }
function toActionText(v) { return actionTextMap[v] || v || "-"; }
function toSignalText(signal) {
  if (!signal) return "无";
  if (typeof signal === "string") return signalTextMap[signal] || signal;
  return signalTextMap[signal.name] || signal.name || signal.desc || "未命名信号";
}

function toMonitorReasonText(text) {
  const raw = String(text || "").trim();
  if (!raw) return "-";
  const rules = [
    [/^Insufficient kline data:\s*(\d+)\/(\d+)\.?$/i, "K线数据不足（$1/$2）"],
    [/^Not enough data\.?$/i, "数据不足"],
    [/^Price crossed above MA20\.?$/i, "价格上穿 MA20"],
    [/^Price crossed below MA20\.?$/i, "价格下破 MA20"],
    [/^MACD golden cross\.?$/i, "MACD 金叉"],
    [/^MACD dead cross\.?$/i, "MACD 死叉"],
    [/^No strong signal yet\. Keep monitoring\.?$/i, "暂无强信号，继续观察"],
    [/^No clear market structure detected\.?$/i, "暂无清晰市场结构"],
    [/^Buy-side conditions still support holding\.?$/i, "买方条件仍支持继续持有"],
    [/^Sell signals are present while holding a position\.?$/i, "持仓中出现卖出信号，建议减仓"],
    [/^Confirmed uptrend\. Consider following the trend\.?$/i, "上升趋势确认，可考虑顺势参与"],
    [/^Accumulation pattern with buy confirmation\.?$/i, "底部吸筹形态，并出现买入确认"],
    [/^Pullback remains constructive\. Consider buying the dip\.?$/i, "回调结构仍健康，可考虑低吸"],
    [/^Buy signal detected\. Consider a small starter position\.?$/i, "出现买入信号，可考虑小仓位试探"],
    [/^Long-horizon valuation is in an undervalued zone\. Consider adding on dips\.?$/i, "长线估值处于低估区，建议在回调时逢低加仓"],
    [/^Price and moving averages are aligned in a bullish structure\.?$/i, "价格与均线呈多头排列"],
    [/^Price is below medium and long-term averages\.?$/i, "价格位于中长期均线下方"],
    [/^No stop rule triggered\. Current pnl is ([+-]?\d+(?:\.\d+)?)%\.?$/i, "未触发止盈止损规则，当前盈亏为 $1%"],
    [/^Stop loss triggered by (.+): current pnl is ([+-]?\d+(?:\.\d+)?)%\.?$/i, "$1 触发止损，当前盈亏为 $2%"],
    [/^Take profit triggered by (.+): current pnl is ([+-]?\d+(?:\.\d+)?)%\.?$/i, "$1 触发止盈，当前盈亏为 $2%"],
  ];
  for (const [re, cn] of rules) if (re.test(raw)) return raw.replace(re, cn);
  return raw;
}
function valuationZoneText(v) {
  const zone = String(v?.zone || "").toLowerCase();
  if (zone === "undervalued") return "低估区";
  if (zone === "overvalued") return "高估区";
  if (zone === "fair") return "合理区间";
  return "未知";
}
function valuationReasonText(v) {
  const reason = String(v?.reason || v?.industry_fallback_reason || "");
  const map = {
    relative_to_industry_undervalued: "相对行业低估：PE/PB 较行业更便宜，且盈利质量不弱",
    relative_to_industry_overvalued: "相对行业偏高：估值高于行业或盈利能力偏弱",
    relative_to_industry_fair: "相对行业处于合理区间",
    low_pe_pb_and_healthy_roe: "PE/PB 较低且 ROE 健康",
    valuation_is_high_or_profitability_weak: "估值偏高或盈利能力偏弱",
    valuation_near_fair_range: "估值接近合理区间",
    insufficient_metrics: "估值指标不足",
  };
  return map[reason] || reason || "暂无说明";
}

/* ---------------- 通用组件 ---------------- */
function showToast(title, msg, type = "info") {
  const stack = qs("#toast");
  if (!stack) return;
  const el = document.createElement("div");
  const borderColor = type === "error" ? "up" : "brand-500"; /* 成功/信息用品牌蓝，错误用红(up) */
  el.className = "toast border-l-" + borderColor;
  el.innerHTML = `<div class="toast-title">${escapeHTML(title)}</div>${msg ? `<div class="toast-message">${escapeHTML(msg)}</div>` : ""}`;
  stack.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 300); }, type === "error" ? 5200 : 2600);
}
function setBusy(btn, busy, text = "处理中...") {
  if (!btn) return;
  if (busy) { btn.dataset.t = btn.textContent; btn.textContent = text; btn.disabled = true; }
  else { btn.disabled = false; if (btn.dataset.t) { btn.textContent = btn.dataset.t; delete btn.dataset.t; } }
}
async function runAction(btn, fn, { success, failure = "操作失败", busy = "处理中..." } = {}) {
  setBusy(btn, true, busy);
  try {
    const r = await fn();
    if (r === false) throw new Error(failure);
    if (success) showToast("成功", success, "success");
    return r;
  } catch (err) {
    showToast("失败", err?.message || failure, "error");
    return false;
  } finally { setBusy(btn, false); }
}
async function fetchJSON(url, options = {}) {
  // 统一请求层见 api.js（此处保留函数名，避免改动上百处调用方）
  return window.requestJSON(url, options);
}

/* ---------------- 状态 ---------------- */
let favoritesCache = [];   // /api/stock/favorites?quotes=true
let monitorCache = [];      // /api/stock/monitor
let indicesCache = [];
let totalCapital = 0;
let accountCache = null;  // 账户快照：可用现金 + 真实仓位，用于算总收益率
let tradesCache = [];     // /api/stock/trades 交易流水（对话记账留痕）
let tradesSummary = null; // 已实现盈亏等汇总
let preferSource = "amazingdata";
let autoTimer = null;
const AUTO_REFRESH_KEY = "monitor_auto_refresh"; // 自动刷新开关持久化，跨页面/刷新不丢失
const SIDEBAR_KEY = "monitor_sidebar_collapsed";  // 侧栏收起状态持久化
function markQuoteUpdated() {
  // 行情刷新指示：loadFavorites / loadIndices 成功后调用
  const el = qs("#updatedAt");
  if (el) el.textContent = new Date().toTimeString().slice(0, 8);
}
function startAutoRefresh() {
  if (autoTimer) return;
  autoTimer = setInterval(async () => {
    // 自动轮询不传 refresh：批量行情源（星耀/腾讯）本身就是实时的，
    // 强刷会触发东财全市场快照这类高成本兜底，30 秒一次会把源打挂
    await Promise.allSettled([loadFavorites(false), loadIndices()]);
  }, 30000);
  const t = qs("#autoToggle"); if (t) t.checked = true;
}
function stopAutoRefresh() {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  const t = qs("#autoToggle"); if (t) t.checked = false;
}
let lastPrices = {};        // 用于价格闪动
let alertRulesCache = {};
let currentDrawerSecID = null;
let intradayCache = {};          // secID -> { ts, v: 收盘价序列, f: 时间轴位置 0~1, asOf: 截止时间 }
let sourceHealth = { primary: "amazingdata", total: 0, dist: {} };

/* ---------------- 真实分时（按当日时间轴定位，09:30~15:00 压缩午休） ---------------- */
const INTRADAY_TTL = 60000;    // 分时缓存 1 分钟，与后端 K 线缓存 TTL 对齐；到期重拉，不让走势停在旧的一天
const INTRADAY_POINTS = 60;    // 240 分钟取 60 点 ≈ 每 4 分钟一个点：格子拉宽后仍看得出细节，又不会重
function minuteFrac(dateStr) {
  // '2026-11-03 09:31' → 在全天刻度（09:30~15:00，午休压缩）中的位置 0~1
  const m = String(dateStr || "").match(/(\d{1,2}):(\d{2})/);
  if (!m) return null;
  const mins = (+m[1]) * 60 + (+m[2]);
  if (mins <= 690) return Math.max(0, (mins - 570) / 240);   // 上午 09:30~11:30 → 0~0.5
  return Math.min(1, 0.5 + (mins - 780) / 240);              // 下午 13:00~15:00 → 0.5~1
}
function sparkSVG(arr, color, fracs, asOf, state) {
  // 没有真实分时就明说，绝不放随机假曲线——假曲线长在真实价格旁边比空白更误导
  if (state && state.empty) return `<div class="text-[11px] text-ink-faint w-full text-center">无分时</div>`;
  if (!arr || arr.length < 2) return `<div class="text-[11px] text-ink-faint w-full text-center">分时加载中…</div>`;
  // 宽度交给容器：viewBox 只当坐标系，格子多宽就画多宽。
  // 原来写死 120px，而「分时」列被表格 w-full 撑到三百多像素，
  // 收盘后当天走完的曲线也只占格子三分之一，白白浪费横向空间。
  const W = 240, H = 34, padY = 3;
  const max = Math.max(...arr), min = Math.min(...arr), span = (max - min) || 1;
  // 时间轴位置只在「确实是当日分时」时用；拿回来的是日线（没有 hh:mm）就按序号均分，
  // 否则所有点都会落在 0.5 处缩成一根竖线。
  const timed = Array.isArray(fracs) && fracs.length === arr.length && fracs.every(Number.isFinite);
  const xAt = i => (timed ? Math.max(0, Math.min(1, fracs[i])) : i / (arr.length - 1)) * W;
  const yAt = v => padY + (H - padY * 2) * (1 - (v - min) / span);
  const pts = arr.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
  const openY = yAt(arr[0]).toFixed(1);
  const tip = asOf ? `<title>分时数据截至 ${escapeHTML(asOf)}</title>` : "";
  // preserveAspectRatio=none：横向拉满、纵向不变形；vector-effect 保证线宽不被一起拉粗
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="display:block;width:100%;height:${H}px" role="img" aria-label="当日分时走势">${tip}`
    + `<line x1="0" y1="${openY}" x2="${W}" y2="${openY}" stroke="#e8eef6" stroke-width="1" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>`
    + `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`
    + `</svg>`;
}
function klineSVG(candles) {
  if (!candles || !candles.length) return `<div class="muted">暂无 K 线数据</div>`;
  const W = 760, H = 248, padT = 10, gap = 12;
  const priceH = 168, volTop = padT + priceH + gap, volH = H - volTop - 12;
  const n = candles.length;
  const maVals = candles.flatMap(c => [c.MA5, c.MA10, c.MA20].filter(v => v != null));
  const hi = Math.max(...candles.map(c => c.high), ...(maVals.length ? maVals : [0]));
  const lo = Math.min(...candles.map(c => c.low), ...(maVals.length ? maVals : [0]));
  const span = (hi - lo) || 1;
  const y = v => padT + (hi - v) / span * priceH;
  const cw = W / n, bw = Math.max(2, cw * 0.62);
  const volMax = Math.max(1, ...candles.map(c => c.volume || 0));
  const vy = v => volTop + volH - (v / volMax) * volH;
  let s = "";
  // 网格 + 价格刻度
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = hi - (i / ticks) * span, yy = y(v);
    s += `<line x1="0" y1="${yy.toFixed(1)}" x2="${W}" y2="${yy.toFixed(1)}" stroke="#eef2f7" stroke-width="1"/>`;
    s += `<text x="${W - 4}" y="${(yy - 2).toFixed(1)}" text-anchor="end" font-size="10" fill="#8a99ad">${fmt(v)}</text>`;
  }
  // 成交量柱
  candles.forEach((c, i) => {
    const x = i * cw + cw / 2, yy = vy(c.volume || 0), col = c.close >= c.open ? "#e23d4e" : "#16a34a";
    s += `<rect x="${(x - bw / 2).toFixed(1)}" y="${yy.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, volTop + volH - yy).toFixed(1)}" fill="${col}" opacity="0.32"/>`;
  });
  // 蜡烛
  candles.forEach((c, i) => {
    const x = i * cw + cw / 2, col = c.close >= c.open ? "#e23d4e" : "#16a34a";
    s += `<line x1="${x.toFixed(1)}" y1="${y(c.high).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(c.low).toFixed(1)}" stroke="${col}" stroke-width="1"/>`;
    const yo = y(c.open), yc = y(c.close), top = Math.min(yo, yc), bh = Math.max(1, Math.abs(yc - yo));
    s += `<rect x="${(x - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" fill="${col}"/>`;
  });
  // 均线
  const maLine = (key, color) => {
    const pts = [];
    candles.forEach((c, i) => { if (c[key] != null) pts.push(`${(i * cw + cw / 2).toFixed(1)},${y(c[key]).toFixed(1)}`); });
    return pts.length ? `<polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="1.4"/>` : "";
  };
  s += maLine("MA5", "#f59e0b") + maLine("MA10", "#3b82f6") + maLine("MA20", "#a855f7");
  return `<svg width="100%" viewBox="0 0 ${W} ${H}">${s}</svg>
    <div class="flex flex-wrap gap-3 text-[11px] mt-1">
      <span class="text-up">■ MA5</span><span style="color:#3b82f6">■ MA10</span><span style="color:#a855f7">■ MA20</span>
      <span class="text-ink-faint">■ 成交量（红涨绿跌）</span>
    </div>`;
}
function downsample(arr, n) {
  if (arr.length <= n) return arr;
  const out = [], step = (arr.length - 1) / (n - 1);
  for (let i = 0; i < n; i++) out.push(arr[Math.round(i * step)]);
  return out;
}
async function loadIntraday(code) {
  const hit = intradayCache[code];
  if (hit && Date.now() - hit.ts < INTRADAY_TTL) return;   // 短缓存：1 分钟后重拉，盘中走势才能持续生长
  // 单只最多等 8 秒：ETF 没有分钟线时，后端会顺着源链一路回退（实测能拖到分钟级），
  // 不设上限就会把「整批重绘」一起拖死——别的票都画不出来。
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const r = await fetchJSON(`/api/stock/realtime_minute?code=${encodeURIComponent(code)}&period=1d`, { signal: ctrl.signal });
    const list = Array.isArray(r) ? r : (r.data || []);
    const pts = list
      .map(d => ({ date: String(d.date || ""), close: Number(d.close) }))
      .filter(d => Number.isFinite(d.close) && d.date);
    if (pts.length >= 2) {
      intradayCache[code] = {
        ts: Date.now(),
        v: downsample(pts.map(p => p.close), INTRADAY_POINTS),
        // 保留 null 不填 0.5：sparkSVG 靠「有没有时间戳」区分分时与日线
        f: downsample(pts.map(p => minuteFrac(p.date)), INTRADAY_POINTS),
        asOf: pts[pts.length - 1].date,
      };
    } else {
      // 明确记成空：否则这只票（如 ETF 无分钟线）会永远显示「分时加载中…」，
      // 用户以为一直在转圈，实际是数据源根本没有。
      intradayCache[code] = { ts: Date.now(), v: [], f: [], asOf: "", empty: true };
    }
  } catch (e) {
    // 超时/失败也记一笔，60 秒后重试；否则这一格永远停在「加载中」且没人知道为什么
    if (ctrl.signal.aborted) intradayCache[code] = { ts: Date.now(), v: [], f: [], asOf: "", empty: true };
  } finally { clearTimeout(timer); }
}

/* ---------------- 数据加载 ---------------- */
async function loadIndices() {
  try {
    const res = await fetchJSON("/api/stock/market_indices");
    indicesCache = (res.data || []).map(d => ({ name: d.name, val: d.price, pct: d.change }));
    markQuoteUpdated();
  } catch (e) { indicesCache = []; }
  renderIndices();
}
async function loadFavorites(refresh = false) {
  try {
    const res = await fetchJSON(`/api/stock/favorites?quotes=true&refresh=${refresh ? "true" : "false"}`);
    favoritesCache = res.data || [];
    updateSourceHealth();
    markQuoteUpdated();
    renderIndices();
    renderOverview();
    renderWatchlist();
    renderCalcOptions();
    // 分时走势图是锦上添花：卡片带着现价先出来，缺的分时放后台补，回来再重绘一次。
    // 原来在这里 await，会让首屏等于「最慢那只票的分时耗时」（实测每票 4–7 秒），
    // 用户盯着的是满屏「加载中」。
    // 注意：不仅要补「缓存里没有」的，还要补「已超过 INTRADAY_TTL」的，
    // 否则分时图首屏画完就冻结，60s TTL 那段守卫永远走不到（盘中走势无法持续生长）。
    const stale = favoritesCache.filter(f => f.secID && (!intradayCache[f.secID] || Date.now() - intradayCache[f.secID].ts >= INTRADAY_TTL));
    if (stale.length) {
      // 逐只回来逐只重绘：用 allSettled 等「全部到齐」再画，只要有一只慢（ETF 没分钟线，
      // 后端源链回退能拖到分钟级），整张表就停在「分时加载中…」——实测接口全 200、
      // DOM 里却 0 个 svg。
      // 另外必须同时重绘 renderOverview（总览表）与 renderWatchlist（自选股卡片页），
      // 只调后者的话，总览表在首屏之后永远不会刷新。
      stale.forEach(f => loadIntraday(f.secID).then(() => { renderWatchlist(); renderOverview(); }));
    }
    return true;
  } catch (err) { showToast("失败", "加载自选股失败：" + (err.message || ""), "error"); return false; }
}
async function loadMonitor(refresh = false, notify = "auto") {
  try {
    const res = await fetchJSON(`/api/stock/monitor?refresh=${refresh ? "true" : "false"}&notify_mode=${encodeURIComponent(notify)}`);
    monitorCache = res.data || [];
    renderOverview();
    return true;
  } catch (err) { showToast("失败", "加载监控结果失败：" + (err.message || ""), "error"); return false; }
}
/* ---------------- 交易流水（对话记账的留痕） ---------------- */
async function loadTrades() {
  try {
    const res = await fetchJSON("/api/stock/trades?limit=20");
    tradesCache = res.data || [];
    tradesSummary = res.summary || null;
    renderTrades();
  } catch (err) {
    const tb = qs("#tradeTable");
    if (tb) tb.innerHTML = `<tr><td colspan="8" class="text-center muted py-6">交易流水加载失败：${escapeHTML(err.message || "")}</td></tr>`;
  }
}
function renderTrades() {
  const tb = qs("#tradeTable");
  if (!tb) return;
  if (!tradesCache.length) {
    tb.innerHTML = `<tr><td colspan="8" class="text-center muted py-6">还没有交易记录 · 在「分析顾问」里说一句「今天卖了 XX 500 股，27.35」即可记账</td></tr>`;
    return;
  }
  const label = { buy: "买入", sell: "卖出", adjust: "纠正" };
  const tone = { buy: "text-down", sell: "text-up", adjust: "text-ink-muted" };
  tb.innerHTML = tradesCache.map(t => {
    const side = String(t.side || "");
    const pnl = t.realized_pnl;
    const pnlText = pnl == null ? "—" : (Number(pnl) >= 0 ? "+" : "") + yuan(pnl);
    const priceText = t.price == null ? "—" : yuan(t.price);
    return `<tr>
      <td class="text-ink-faint">${escapeHTML(t.trade_date || (t.created_at || "").slice(0, 10))}</td>
      <td class="${tone[side] || ""} font-medium">${label[side] || escapeHTML(side)}</td>
      <td><div class="font-medium">${escapeHTML(t.name || "")}</div><div class="text-[12px] text-ink-faint">${escapeHTML(t.secID || "")}</div></td>
      <td class="text-right kbox">${fmt(t.shares)}</td>
      <td class="text-right kbox">${priceText}</td>
      <td class="text-right kbox">${t.fee == null ? "—" : yuan(t.fee)}</td>
      <td class="text-right kbox ${pnl == null ? "" : clsOf(pnl)}">${pnlText}</td>
      <td class="text-right kbox">${fmt(t.shares_after)}</td>
    </tr>`;
  }).join("");
}

/* ---------------- 渲染：指数 + 数据源状态（真实健康度） ---------------- */
function updateSourceHealth() {
  // 统计实际拿到现价的来源分布：主源挂了但兜底顶上了，也应该让用户看见真相
  const dist = {};
  let total = 0;
  favoritesCache.forEach(f => {
    const s = f.source || (f.spot && f.spot.source);
    if (s) { total++; dist[s] = (dist[s] || 0) + 1; }
  });
  sourceHealth = { primary: preferSource, total, dist };
}
const SOURCE_LABEL = { amazingdata: "星耀", xueqiu: "雪球", tencent: "腾讯", eastmoney: "东财", akshare: "东财", none: "无数据" };
function sourceStatusText() {
  const sh = sourceHealth || { primary: preferSource, total: 0, dist: {} };
  const primaryLabel = SOURCE_LABEL[sh.primary] || sh.primary;
  if (!sh.total) return { label: primaryLabel, text: "数据加载中…", cls: "text-ink-faint" };
  const dist = sh.dist || {};
  const parts = Object.entries(dist).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${SOURCE_LABEL[k] || k} ${v}`).join(" · ");
  const noneCount = dist.none || 0;
  if (noneCount === sh.total) return { label: "全部失败", text: "行情源全部不可用，现价缺失", cls: "text-up" };
  if ((dist[sh.primary] || 0) === sh.total) return { label: primaryLabel, text: `主源命中 ${sh.total}/${sh.total} · 在线`, cls: "text-down" };
  if (noneCount) return { label: primaryLabel, text: `${parts}（${noneCount} 只无现价）`, cls: "text-up" };
  return { label: primaryLabel, text: `${parts} · 已降级兜底`, cls: "text-amber-600" };
}
function renderSidebarSource() {
  const el = qs("#sideSourceSub");
  if (el) { const st = sourceStatusText(); el.textContent = st.text; }
}
function renderIndices() {
  const st = sourceStatusText();
  const plus = `<div class="bg-white border border-line rounded-2xl shadow-card p-4 min-w-0">
      <div class="text-[12px] text-ink-muted">数据源状态</div>
      <div class="mt-1 flex items-center gap-2"><span class="dot ${preferSource === "amazingdata" ? "bg-down" : "bg-amber-500"}"></span>
      <span class="font-bold text-[15px]">${st.label}</span></div>
      <div class="text-[12px] mt-1 ${st.cls}">${st.text}</div>
    </div>`;
  const cards = indicesCache.map(x => {
    const c = clsOf(x.pct);
    return `<div class="bg-white border border-line rounded-2xl shadow-card p-4 min-w-0">
      <div class="text-[12px] text-ink-muted truncate">${escapeHTML(x.name)}</div>
      <div class="mt-1 font-extrabold text-[20px] kbox">${fmt(x.val)}</div>
      <div class="text-[13px] font-semibold kbox text-${c}">${signOf(x.pct)}${fmt(x.pct)}%</div>
    </div>`;
  }).join("");
  qs("#indexStrip").innerHTML = cards + plus;
  renderSidebarSource();
}

/* ---------------- 渲染：总览 ---------------- */
function pickName(item) { return item.name || item.secShortName || item.secID || "-"; }
function priceCellHTML(code, price, pct) {
  const prev = lastPrices[code];
  let flash = "";
  if (prev !== undefined && price !== prev) flash = price > prev ? "price-flash-up" : "price-flash-down";
  lastPrices[code] = price;
  const c = clsOf(pct);
  return `<div class="text-right font-semibold kbox text-${c} price-cell ${flash}" data-code="${escapeHTML(code)}">${price == null ? "-" : fmt(price)}</div>`;
}
function renderOverview() {
  const tbody = qs("#ovTable");
  if (!favoritesCache.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-center muted py-6">暂无自选股，去「自选股」页添加</td></tr>`;
  } else {
    tbody.innerHTML = favoritesCache.map(item => {
      const price = item.price ?? item.spot?.price;
      const pct = item.pctChg ?? item.spot?.pct_chg;
      const change = item.change ?? item.spot?.change;
      const amount = item.spot?.amount;
      const c = clsOf(pct);
      const d = priceCellHTML(item.secID, price, pct);
      return `<tr class="cursor-pointer hover:bg-brand-50/40" data-code="${escapeHTML(item.secID)}">
        <td><div class="font-semibold">${escapeHTML(pickName(item))}</div><div class="text-[12px] text-ink-faint">${escapeHTML(item.secID)}</div></td>
        <td class="text-right">${d}</td>
        <td class="text-right font-semibold kbox text-${c}">${pct == null ? "-" : signOf(pct) + fmt(pct) + "%"}</td>
        <td class="text-right kbox text-${c}">${change == null ? "-" : signOf(change) + fmt(change)}</td>
        <td class="text-right kbox text-ink-muted">${fmtAmount(amount)}</td>
        <td class="text-center">${(() => { const s = intradayCache[item.secID]; return sparkSVG(s?.v, colorOf(pct), s?.f, s?.asOf, s); })()}</td>
        <td class="text-right"><button class="text-brand-600 text-[13px] font-semibold hover:underline" data-act="detail" data-code="${escapeHTML(item.secID)}">详情</button></td>
      </tr>`;
    }).join("");
  }
  renderAlerts();
  renderPositionSummary();
  renderHoldingsOverview();
}
function buildAlerts() {
  const out = [];
  monitorCache.forEach(item => {
    const action = item.action;
    const pct = item.pctChg ?? item.spot?.pct_chg;
    const name = item.name || item.secShortName || item.secID || "-";
    const trade = action && ["buy", "buy_dip", "probe_buy", "accumulate", "reduce", "stop_loss", "exit", "trend_follow"].includes(action);
    if (trade) {
      out.push({ tag: action === "buy" || action === "buy_dip" || action === "probe_buy" || action === "accumulate" ? "买入" : "预警",
        kind: action === "buy" || action === "buy_dip" || action === "probe_buy" || action === "accumulate" ? "buy" : "sell",
        title: `${name} 触发「${toActionText(action)}」信号`, desc: toMonitorReasonText(item.action_desc || item.stage_reason || "-") });
    } else if (pct != null && Math.abs(pct) >= 3) {
      out.push({ tag: pct > 0 ? "涨幅" : "跌幅", kind: pct > 0 ? "buy" : "sell",
        title: `${name} ${pct > 0 ? "涨幅" : "跌幅"} ${signOf(pct)}${fmt(pct)}%`, desc: "触发涨跌幅提醒阈值" });
    }
  });
  return out.slice(0, 12);
}
function renderAlerts() {
  const alerts = buildAlerts();
  const ul = qs("#alertFeed");
  if (!alerts.length) { ul.innerHTML = `<li class="muted text-center py-4">暂无预警，运行监控后将在此显示</li>`; return; }
  ul.innerHTML = alerts.map(a => {
    const col = a.kind === "buy" ? "text-down" : "text-up";
    return `<li class="flex items-start gap-3 p-2.5 rounded-xl border border-line">
      <span class="tag bg-brand-50 ${col} mt-0.5">${escapeHTML(a.tag)}</span>
      <div><div class="font-medium">${escapeHTML(a.title)}</div><div class="text-[12px] text-ink-faint">${escapeHTML(a.desc)}</div></div>
    </li>`;
  }).join("");
}
function renderPositionSummary() {
  let mv = 0, tc = 0;
  favoritesCache.forEach(s => {
    const shares = Number(s.shares || 0), cost = Number(s.cost || 0), price = Number(s.price || 0);
    if (shares > 0) { tc += shares * cost; mv += shares * price; }
  });
  const pl = mv - tc;
  // 总收益率必须用「账户总资产 − 本金」，不能用「持仓市值 − 本金」：
  // 后者把还没买成股票的现金当成亏损，本金 27 万、买 25.25 万、剩 1.7 万现金时会虚报亏损。
  const cash = accountCache && accountCache.available_cash != null ? Number(accountCache.available_cash) : null;
  const hasCash = cash != null;
  const totalAssets = hasCash ? mv + cash : mv;
  const cap = Number(totalCapital || 0);
  const hasCap = cap > 0;
  const totalPlp = hasCap ? (totalAssets - cap) / cap * 100 : (tc > 0 ? pl / tc * 100 : 0);
  const totalTip = hasCap
    ? (hasCash
        ? `总收益率 =（账户总资产 − 本金）/ 本金；总资产 = 持仓市值 ${yuan(mv)} + 可用现金 ${yuan(cash)}，本金 ${yuan(cap)}`
        : `总收益率 =（持仓市值 − 本金）/ 本金；本金 ${yuan(cap)}。⚠ 未填可用现金，现金部分未计入，亏损可能被高估`)
    : "未设置本金（设置→投入本金），暂按持仓成本口径计算，设置本金后自动切换为含已实现盈亏的总收益率";
  // 已实现盈亏：有交易流水就用流水算（按每笔真实卖价减成本），没有才退化为反推估算
  // 反推式 = 持仓成本 + 可用现金 − 本金，能对上账但会被出入金污染
  const ledgerRealized = accountCache && accountCache.realized_pnl != null ? Number(accountCache.realized_pnl) : null;
  const estimatedRealized = hasCap ? tc + (hasCash ? cash : 0) - cap : null;
  const realized = ledgerRealized != null ? ledgerRealized : estimatedRealized;
  const realizedLabel = ledgerRealized != null ? "已实现盈亏(记账)" : "已实现盈亏(估)";
  const realizedTip = ledgerRealized != null
    ? `由 ${accountCache.trade_count || 0} 笔交易流水逐笔算出：Σ(卖价 − 成本)×股数 − 费用。比反推值可信，不受出入金影响`
    : "已实现盈亏 ≈ 持仓成本 + 可用现金 − 本金；负数主要为历史割肉亏损（估算值，在「分析顾问」里报过成交后会切换为记账值）";
  const items = [
    ["持仓市值", yuan(mv), "", "按最新价 × 持股数累计。自动刷新开启时每 30 秒随行情重算；盘中股价不跳动时数字不变是正常的"],
    ["持仓成本", yuan(tc), ""],
    ["浮动盈亏", (pl >= 0 ? "+" : "") + yuan(pl), clsOf(pl)],
    ["总收益率", signOf(totalPlp) + fmt(totalPlp) + "%", clsOf(totalPlp), totalTip],
    ...(hasCap || ledgerRealized != null ? [[realizedLabel, (realized >= 0 ? "+" : "") + yuan(realized), clsOf(realized), realizedTip]] : []),
    ...(hasCash ? [["真实仓位", (totalAssets > 0 ? (mv / totalAssets * 100).toFixed(1) : "0.0") + "%", "", `仓位 = 持仓市值 ÷ 账户总资产（市值 ${yuan(mv)} + 现金 ${yuan(cash)}）。满仓时钉钉只提醒卖出`]] : []),
    ["自选数量", favoritesCache.length + " 只", ""],
    ["预警条数", buildAlerts().length + " 条", ""],
  ];
  qs("#posSummary").innerHTML = items.map(([k, v, c, tip]) =>
    `<div class="rounded-xl bg-brand-50/60 p-3"${tip ? ` title="${escapeHTML(tip)}"` : ""}><div class="text-[12px] text-ink-muted">${k}</div><div class="mt-1 font-bold kbox ${c ? "text-" + c : ""}">${v}</div></div>`
  ).join("");
}

/* ---------------- 渲染：持仓概述（成本锁定不可调整） ---------------- */
function renderHoldingsOverview() {
  const tbody = qs("#holdingsTable");
  const held = favoritesCache.filter(s => Number(s.shares || 0) > 0);
  if (!held.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-center muted py-6">暂无持仓（请在自选股中填写持股数量与成本）</td></tr>`;
    return;
  }
  tbody.innerHTML = held.map(s => {
    const shares = Number(s.shares || 0);
    const cost = Number(s.cost || 0);
    const price = Number(s.price ?? s.spot?.price ?? 0);
    const mv = shares * price;
    const tc = shares * cost;
    const pl = mv - tc;
    const plp = tc > 0 ? pl / tc * 100 : 0;
    const c = clsOf(pl);
    const costLocked = `<span class="kbox">${yuan(cost)}</span> <span class="text-[11px] text-ink-faint" title="持仓成本已锁定，不可调整">🔒</span>`;
    return `<tr class="hover:bg-brand-50/40 cursor-pointer" data-code="${escapeHTML(s.secID)}">
      <td><div class="font-semibold">${escapeHTML(pickName(s))}</div><div class="text-[12px] text-ink-faint">${escapeHTML(s.secID)}</div></td>
      <td class="text-right kbox">${fmt(shares)}</td>
      <td class="text-right">${costLocked}</td>
      <td class="text-right kbox">${yuan(price)}</td>
      <td class="text-right kbox">${yuan(mv)}</td>
      <td class="text-right kbox text-${c}">${pl >= 0 ? "+" : ""}${yuan(pl)}</td>
      <td class="text-right kbox text-${c}">${signOf(plp)}${fmt(plp)}%</td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("tr[data-code]").forEach(tr => {
    tr.addEventListener("click", () => openDetail(tr.dataset.code));
  });
}

/* ---------------- 渲染：自选股卡片 ---------------- */
let activeGroup = "__all__";   // 自选分组筛选：__all__ = 全部，"" = 未分组

function renderGroupTabs() {
  const box = qs("#wlGroupTabs");
  if (!box) return;
  const groups = [...new Set(favoritesCache.map(x => String(x.group || "").trim()).filter(Boolean))];
  const items = [{ key: "__all__", label: `全部 ${favoritesCache.length}` }]
    .concat(favoritesCache.some(x => !String(x.group || "").trim()) ? [{ key: "", label: "未分组" }] : [])
    .concat(groups.map(g => ({ key: g, label: g })));
  // 当前选中的分组被删空时自动回到"全部"，避免出现空白页
  if (activeGroup !== "__all__" && !items.some(it => it.key === activeGroup)) activeGroup = "__all__";
  box.innerHTML = items.map(it => {
    const on = activeGroup === it.key;
    return `<button data-group="${escapeHTML(it.key)}" class="px-3 py-1.5 rounded-lg border text-[12.5px] font-semibold ${on ? "bg-brand-500 text-white border-brand-500" : "border-line text-ink-muted hover:bg-brand-50"}">${escapeHTML(it.label)}</button>`;
  }).join("");
  const dl = qs("#favGroupOptions");
  if (dl) dl.innerHTML = groups.map(g => `<option value="${escapeHTML(g)}"></option>`).join("");
}

function renderWatchlist() {
  const grid = qs("#wlGrid");
  if (!grid) return;
  renderGroupTabs();
  const list = activeGroup === "__all__" ? favoritesCache : favoritesCache.filter(x => String(x.group || "").trim() === activeGroup);
  if (!list.length) {
    grid.innerHTML = `<div class="muted col-span-full py-6">${favoritesCache.length ? "该分组下暂无自选股。" : "暂无自选股，先在上方添加。"}</div>`;
    return;
  }
  grid.innerHTML = list.map(item => {
    const price = item.price ?? item.spot?.price;
    const pct = item.pctChg ?? item.spot?.pct_chg;
    const c = clsOf(pct);
    const groupTag = String(item.group || "").trim()
      ? `<span class="tag bg-brand-50 text-brand-600 ml-1">${escapeHTML(item.group)}</span>` : "";
    return `<div class="bg-white border border-line rounded-2xl shadow-card p-4 cursor-pointer hover:shadow-md transition" data-code="${escapeHTML(item.secID)}" data-act="detail">
      <div class="flex items-start justify-between">
        <div><div class="font-bold text-[16px]">${escapeHTML(pickName(item))}</div><div class="text-[12px] text-ink-faint">${escapeHTML(item.secID)} · <span class="${item.pool ? "" : "text-amber-600"}">${escapeHTML(poolText(item.pool))}</span>${groupTag}</div></div>
        <button class="text-[12px] text-up hover:underline" data-act="del" data-code="${escapeHTML(item.secID)}">移除</button>
      </div>
      <div class="mt-3 flex items-end gap-3">
        <div class="shrink-0"><div class="text-[24px] font-extrabold kbox price-cell ${lastPrices[item.secID] !== undefined && price !== lastPrices[item.secID] ? (price > lastPrices[item.secID] ? "price-flash-up" : "price-flash-down") : ""}" data-code="${escapeHTML(item.secID)}">${price == null ? "-" : fmt(price)}</div>
          <div class="text-[13px] font-semibold kbox text-${c}">${pct == null ? "-" : signOf(pct) + fmt(pct) + "%"}</div></div>
        <div class="flex-1 min-w-0 pb-1">${(() => { const s = intradayCache[item.secID]; return sparkSVG(s?.v, colorOf(pct), s?.f, s?.asOf, s); })()}</div>
      </div>
    </div>`;
  }).join("");
}

/* ---------------- 渲染：差价计算器 ---------------- */
function renderCalcOptions() {
  const sel = qs("#calcStock");
  if (!sel) return;
  sel.innerHTML = favoritesCache.map(s => `<option value="${escapeHTML(s.secID)}">${escapeHTML(pickName(s))} ${escapeHTML(s.secID)}</option>`).join("");
  if (favoritesCache.length) {
    const s = favoritesCache[0];
    qs("#calcPrice").value = s.price ?? s.spot?.price ?? "";
    qs("#calcShares").value = s.shares ?? "";
    qs("#calcCost").value = s.cost ?? "";
    calc();
  }
}
function calcSpreadBreakEven({ sellPrice, shares, commissionRatePct, stampDutyRatePct }) {
  if (!Number.isFinite(sellPrice) || sellPrice <= 0) return { ok: false, message: "卖出价必须大于 0" };
  if (!Number.isFinite(shares) || shares <= 0) return { ok: false, message: "股数必须大于 0" };
  const commissionRate = commissionRatePct / 100;
  const stampDutyRate = stampDutyRatePct / 100;
  const sellAmount = sellPrice * shares;
  const sellCommission = Math.max(sellAmount * commissionRate, 5);
  const stampDuty = sellAmount * stampDutyRate;
  const netSellCash = sellAmount - sellCommission - stampDuty;
  let breakEvenBuyPrice = netSellCash / shares;
  if (commissionRate > 0) {
    const proportionalCasePrice = netSellCash / (shares * (1 + commissionRate));
    const thresholdPrice = 5 / (shares * commissionRate);
    if (proportionalCasePrice >= thresholdPrice) breakEvenBuyPrice = proportionalCasePrice;
  }
  if (breakEvenBuyPrice <= 0) return { ok: false, message: "计算结果无效" };
  return { ok: true, sellAmount, sellCommission, stampDuty, netSellCash, breakEvenBuyPrice, spreadPerShare: sellPrice - breakEvenBuyPrice };
}
function calc() {
  const code = qs("#calcStock")?.value;
  const s = favoritesCache.find(x => x.secID === code) || favoritesCache[0];
  const price = parseFloat(qs("#calcPrice").value) || (s ? (s.price ?? s.spot?.price) : 0) || 0;
  const shares = parseFloat(qs("#calcShares").value) || (s ? Number(s.shares || 0) : 0) || 0;
  const cost = parseFloat(qs("#calcCost").value) || (s ? Number(s.cost || 0) : 0) || 0;
  const fee = parseFloat(qs("#calcFee").value) || 0.15;
  const r = calcSpreadBreakEven({ sellPrice: price, shares, commissionRatePct: fee, stampDutyRatePct: 0.05 });
  if (!r.ok) {
    qs("#calcOut").innerHTML = `<div class="rounded-xl bg-brand-50/60 p-4 col-span-full text-ink-muted">${escapeHTML(r.message)}</div>`;
    qs("#calcNote").textContent = "请检查输入。";
    return;
  }
  const needUp = (r.breakEvenBuyPrice - price) / price * 100;
  qs("#calcOut").innerHTML = [
    ["保本买回价", yuan(r.breakEvenBuyPrice), "down"],
    ["需回补涨幅", signOf(needUp) + fmt(needUp) + "%", clsOf(needUp)],
    ["本次卖出到手", yuan(r.netSellCash), ""],
  ].map(([k, v, c]) => `<div class="rounded-xl bg-brand-50/60 p-4"><div class="text-[12px] text-ink-muted">${k}</div><div class="mt-1 text-[20px] font-extrabold kbox ${c ? "text-" + c : ""}">${v}</div></div>`).join("");
  qs("#calcNote").innerHTML = `以当前价 <b>${fmt(price)}</b> 卖出 ${shares} 股、综合费率约 ${fee}%（含佣金+印花税），要不亏本买回应回到 <b class="text-down">${fmt(r.breakEvenBuyPrice)}</b> 元。`;
}

/* ---------------- 渲染：详情抽屉 ---------------- */
function renderIndustryVal(iv) {
  if (!iv || !iv.ok) return `<span class="muted">${iv && iv.error ? escapeHTML(iv.error) : "行业估值暂不可用"}</span>`;
  const ind = iv.industry_name || iv.industry_code || "本行业";
  const stats = iv.industry_stats || {};
  const rel = iv.relative || {};
  // 沿用本应用信号配色：绿=正面(便宜) / 红=警示(偏贵) / 中性=合理
  const zoneMap = { cheap: ["便宜", "text-down"], fair: ["合理", "text-ink"], expensive: ["偏贵", "text-up"] };
  const [zoneLabel, zoneCls] = zoneMap[rel.valuation_zone] || ["-", "text-ink"];
  const pct = (v) => v == null ? "-" : (v > 0 ? "+" : "") + Number(v).toFixed(1) + "%";
  const pctl = (v) => v == null ? "-" : (Number(v) * 100).toFixed(0) + "%分位";
  const rows = [];
  if (stats.pe_median != null) rows.push(["行业 PE 中位", Number(stats.pe_median).toFixed(1)]);
  if (stats.pb_median != null) rows.push(["行业 PB 中位", Number(stats.pb_median).toFixed(2)]);
  if (rel.pe_vs_median_pct != null) rows.push(["PE 相对行业", pct(rel.pe_vs_median_pct)]);
  if (rel.pb_vs_median_pct != null) rows.push(["PB 相对行业", pct(rel.pb_vs_median_pct)]);
  if (rel.pe_percentile != null) rows.push(["PE 行业分位", pctl(rel.pe_percentile)]);
  if (rel.pb_percentile != null) rows.push(["PB 行业分位", pctl(rel.pb_percentile)]);
  let html = `<div class="flex items-center justify-between"><span class="text-ink-muted">所属行业　<b class="text-ink">${escapeHTML(ind)}</b></span><span class="tag ${zoneCls} bg-brand-50 font-semibold">${zoneLabel}</span></div>`;
  html += rows.map(([k, v]) => `<div class="flex justify-between mt-1"><span class="text-ink-muted">${k}</span><b class="kbox">${v}</b></div>`).join("");
  const notes = (rel.notes || []).filter(Boolean);
  if (notes.length) html += `<div class="text-[12px] text-ink-faint mt-2">${escapeHTML(notes.join("；"))}</div>`;
  return html;
}

async function openDetail(secID) {
  const fav = favoritesCache.find(x => x.secID === secID);
  if (!fav) return;
  currentDrawerSecID = secID;
  const price = fav.price ?? fav.spot?.price;
  const pct = fav.pctChg ?? fav.spot?.pct_chg;
  const change = fav.change ?? fav.spot?.change;
  const prevClose = fav.spot?.prev_close ?? fav.spot?.pre_close;
  const c = clsOf(pct);
  qs("#dName").textContent = pickName(fav);
  qs("#dCode").textContent = secID;
  const dp = qs("#dPrice"); dp.textContent = price == null ? "--" : fmt(price); dp.className = "text-[34px] font-extrabold kbox " + (c === "up" ? "text-up" : c === "down" ? "text-down" : "text-ink");
  qs("#dPct").textContent = `${pct == null ? "--" : signOf(pct) + fmt(pct) + "%"}${change != null ? "  " + signOf(change) + fmt(change) : ""}`;
  qs("#dPct").className = "text-[15px] font-semibold kbox text-" + c;

  // 持仓表单
  qs("#dShares").value = fav.shares ?? "";
  qs("#dCost").value = fav.cost ?? "";
  qs("#dPool").value = fav.pool || "";

  // K线（真实，含 MA / 成交量）
  qs("#dKline").innerHTML = `<div class="text-[12px] text-ink-muted mb-1">近 40 交易日 K 线（含 MA5/10/20 与成交量）</div><div class="text-ink-muted">加载中...</div>`;
  try {
    const k = await fetchJSON(`/api/stock/kline?code=${encodeURIComponent(secID)}&frequency=d`);
    const kd = (k.data || []).map(d => ({
      open: Number(d.open), high: Number(d.high), low: Number(d.low), close: Number(d.close), volume: Number(d.volume || 0),
      MA5: d.MA5 != null ? Number(d.MA5) : null, MA10: d.MA10 != null ? Number(d.MA10) : null, MA20: d.MA20 != null ? Number(d.MA20) : null,
    }));
    const view = kd.slice(-40);
    qs("#dKline").innerHTML = `<div class="text-[12px] text-ink-muted mb-1">近 40 交易日 K 线（含 MA5/10/20 与成交量）</div>${klineSVG(view)}`;
  } catch (e) { qs("#dKline").innerHTML = `<div class="text-[12px] text-ink-muted mb-1">近 40 交易日 K 线</div><div class="muted">K线加载失败</div>`; }

  // 关键指标
  const spot = fav.spot || {};
  const open = spot.open, high = spot.high, low = spot.low, amount = spot.amount, volume = spot.volume;
  const amp = (high != null && low != null && prevClose) ? (high - low) / prevClose * 100 : null;
  const metrics = [
    ["今开", open != null ? fmt(open) : "-"],
    ["最高", high != null ? fmt(high) : "-"],
    ["最低", low != null ? fmt(low) : "-"],
    ["昨收", prevClose != null ? fmt(prevClose) : "-"],
    ["成交额", fmtAmount(amount)],
    ["振幅", amp != null ? fmt(amp) + "%" : "-"],
  ];
  qs("#dMetrics").innerHTML = metrics.map(([k, v]) => `<div class="rounded-xl bg-brand-50/60 p-2.5"><div class="text-[11px] text-ink-muted">${k}</div><div class="mt-0.5 font-bold kbox">${v}</div></div>`).join("");

  // 策略信号（来自监控缓存）
  const mon = monitorCache.find(x => x.secID === secID);
  if (mon) {
    const sigColor = ["buy","buy_dip","probe_buy","accumulate","trend_follow"].includes(mon.action) ? "text-down" : ["reduce","stop_loss","exit"].includes(mon.action) ? "text-up" : "text-brand-600";
    const signals = (mon.signals || []).map(toSignalText).join("、") || "无触发信号";
    let html = `<div class="flex items-center justify-between rounded-xl border border-line px-3 py-2"><span class="text-ink-muted">综合信号</span><span class="tag bg-brand-50 ${sigColor} font-semibold">${escapeHTML(toActionText(mon.action))}</span></div>`;
    html += `<div class="rounded-xl border border-line px-3 py-2 text-ink-muted">趋势阶段：<b class="text-ink">${escapeHTML(toStageText(mon.market_stage))}</b></div>`;
    html += `<div class="rounded-xl border border-line px-3 py-2 text-ink-muted">估值：<b class="text-ink">${escapeHTML(valuationZoneText(mon.valuation))}</b> · ${escapeHTML(valuationReasonText(mon.valuation))}</div>`;
    html += `<div class="rounded-xl border border-line px-3 py-2 text-ink-muted">信号：<b class="text-ink">${escapeHTML(signals)}</b></div>`;
    html += `<div class="rounded-xl border border-line px-3 py-2 text-ink-muted">说明：<b class="text-ink">${escapeHTML(toMonitorReasonText(mon.action_desc || mon.stage_reason || "-"))}</b></div>`;
    qs("#dSignals").innerHTML = html;

    // 买卖点参考
    const pp = mon.position_plan;
    let buyP = prevClose ?? price, sellP = price;
    if (pp && (pp.suggestion === "buy" || pp.suggestion === "add") && pp.suggest_price) sellP = pp.suggest_price;
    if (pp && (pp.suggestion === "reduce" || pp.suggestion === "exit") && pp.suggest_price) buyP = pp.suggest_price;
    qs("#dTrade").innerHTML = `<div class="flex justify-between"><span>参考买点</span><b class="kbox text-up">${fmt(buyP)}</b></div>
      <div class="flex justify-between mt-1"><span>参考卖点</span><b class="kbox text-down">${fmt(sellP)}</b></div>
      <div class="text-[12px] text-ink-faint mt-2">买卖点为基于成本、信号与仓位计划的示意区间，不构成投资建议。</div>`;
  } else {
    qs("#dSignals").innerHTML = `<div class="rounded-xl border border-line px-3 py-2 text-ink-muted">暂无监控数据，点顶栏「刷新行情」运行监控后显示。</div>`;
     qs("#dTrade").innerHTML = `<div class="text-[12px] text-ink-faint">运行监控后展示买卖点参考。</div>`;
   }

   // 行业估值（相对本行业中枢）：星耀已预热时快速返回，失败不影响其它区块
   qs("#dIndustryVal").innerHTML = "加载中…";
   try {
     const iv = await fetchJSON(`/api/stock/industry_valuation?code=${encodeURIComponent(secID)}`);
     qs("#dIndustryVal").innerHTML = renderIndustryVal(iv);
   } catch (e) {
     qs("#dIndustryVal").innerHTML = `<span class="muted">行业估值加载失败</span>`;
   }

   qs("#drawerMask").classList.remove("hidden");
  qs("#drawer").classList.remove("translate-x-full");
  qs("#drawer").classList.add("animate-slidein");
}
function closeDetail() { qs("#drawer").classList.add("translate-x-full"); qs("#drawerMask").classList.add("hidden"); }

/* ---------------- 渲染：设置 ---------------- */
async function loadSettings() {
  // 账户状态：仓位口径 = 市值 ÷ 总资产。让用户在改设置前先看到现在的真实仓位与弹药
  function renderAcctStatus(a) {
    const el = qs("#acctStatus");
    if (!el) return;
    if (!a || a.available_cash == null) {
      el.innerHTML = `<b class="text-ink">现金未知</b>：填一次「账户可用资金」才能算出真实仓位，<br/>`
        + `否则只能按「本金−持仓成本」估算，卖出过的账户会偏大。`;
      return;
    }
    const num = (v) => Number(v || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
    const pct = (v) => (v == null ? "-" : (Number(v) * 100).toFixed(1) + "%");
    const tone = a.ammo_state === "full" ? "text-up" : (a.ammo_state === "tight" ? "text-ink" : "text-down");
    const src = a.cash_source === "estimated"
      ? `<span class="text-[10.5px] text-up">（估算值，卖出过会偏大）</span>`
      : `<span class="text-[10.5px] text-ink-faint">（你填的真实值）</span>`;
    el.innerHTML = `持仓市值 <b class="text-ink">${num(a.market_value)}</b> 元 · 可用现金 <b class="text-ink">${num(a.available_cash)}</b> 元 ${src}<br/>`
      + `账户总资产 <b class="text-ink">${num(a.total_assets)}</b> 元 · 真实仓位 <b class="${tone}">${pct(a.position_ratio)}</b> · `
      + `弹药状态 <b class="${tone}">${esc(a.ammo_label || "-")}</b>`
      + (a.buy_allowed === false ? ` · <span class="text-up">已停止提醒买入</span>` : "");
  }

  // 市场情绪档位 + 目标仓位 vs 实际仓位
  // 用户理念：情绪低落留现金建仓、情绪疯狂要卖出 —— 所以要让仓位偏离一眼可见
  function renderSentimentPanel(data) {
    const el = qs("#sentimentPanel");
    if (!el) return;
    if (!data || !data.ok || !data.policy || !data.policy.ok) {
      el.innerHTML = `<b class="text-ink">情绪数据不可用</b>：按原有纪律常规管理，`
        + `不因缺数据而放松，也不因缺数据而恐慌。`;
      return;
    }
    const p = data.policy;
    const c = data.cycle || {};
    const pct = (v) => (v == null ? "-" : (Number(v) * 100).toFixed(0) + "%");
    const tMin = pct(p.target_position_min);
    const tMax = pct(p.target_position_max);
    const actual = pct(p.actual_position);
    const phaseTone = {
      collapse: "text-down", ebb: "text-down", freezing: "text-up",
      repair: "text-up", advance: "text-ink", euphoria: "text-up", unknown: "text-ink",
    }[p.phase] || "text-ink";
    const gap = String(p.gap_action || "");
    const gapTone = gap.includes("高于目标上限") ? "text-up"
      : gap.includes("低于目标下限") ? "text-down" : "text-ink";
    const buyLine = p.allow_new_buy
      ? `<span class="text-up">允许开仓</span>（只做低位、有价值的方向）`
      : `<span class="text-up">禁止开仓</span>（此阶段只做减仓/保命/持有）`;

    el.innerHTML = `<div class="flex items-center gap-2 mb-1">`
      + `<b class="${phaseTone}">${esc(p.regime_label || p.phase_label || "-")}</b>`
      + `<span class="text-[10.5px] text-ink-faint">情绪温度 ${pct(p.temperature)}`
      + (c.temperature_percentile != null ? ` · 历史 ${pct(c.temperature_percentile)} 分位` : "")
      + ` · 阶段已持续 ${c.phase_run_days ?? "-"} 日</span></div>`
      + `<div class="text-[11px] text-ink-muted mb-1">目标仓位 <b class="text-ink">${tMin}~${tMax}</b>`
      + ` · 目标现金 <b class="text-ink">${pct(p.target_cash_min)}~${pct(p.target_cash_max)}</b>`
      + ` · 实际仓位 <b class="text-ink">${actual}</b></div>`
      + `<div class="mb-1"><b class="${gapTone}">${esc(gap || "-")}</b></div>`
      + `<div class="text-[11px] text-ink-muted">${esc(p.action_text || "")}</div>`
      + `<div class="text-[11px] text-ink-muted mt-0.5">节奏：${esc(p.pace || "-")} ｜ 开仓：${buyLine}</div>`
      + (c.cycle_text ? `<div class="text-[10.5px] text-ink-faint mt-1">周期位置：${esc(c.cycle_text)}</div>` : "");
  }

  // 情绪历史曲线（迷你 SVG）
  function renderSentimentCurve(data) {
    const box = qs("#sentimentCurve");
    if (!box) return;
    const pts = (data && data.points) || [];
    if (!pts.length) {
      box.innerHTML = `<div class="text-[10.5px] text-ink-faint">情绪历史曲线暂无数据（需每日累积快照）</div>`;
      return;
    }
    const W = 320, H = 76, PAD = 6;
    const vals = pts.map((p) => (typeof p.temperature === "number" ? p.temperature : null));
    const known = vals.filter((v) => v != null);
    if (!known.length) {
      box.innerHTML = `<div class="text-[10.5px] text-ink-faint">情绪历史曲线暂无数据</div>`;
      return;
    }
    const max = Math.max(...known, 0.2), min = Math.min(...known, 0);
    const xOf = (i) => PAD + (i * (W - PAD * 2)) / Math.max(1, pts.length - 1);
    const yOf = (v) => H - PAD - ((v - min) / Math.max(1e-6, max - min)) * (H - PAD * 2);
    // 阶段用颜色区分：亢奋/崩塌高亮，其余淡色
    const phaseColor = {
      euphoria: "#e23d4e", collapse: "#e23d4e", ebb: "#f59e0b",
      advance: "#16a34a", repair: "#0ea5e9", freezing: "#0ea5e9", unknown: "#94a3b8",
    };
    const segs = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const v0 = vals[i], v1 = vals[i + 1];
      if (v0 == null || v1 == null) continue;
      const color = phaseColor[pts[i + 1].phase] || "#94a3b8";
      segs.push(`<line x1="${xOf(i).toFixed(1)}" y1="${yOf(v0).toFixed(1)}" x2="${xOf(i + 1).toFixed(1)}" y2="${yOf(v1).toFixed(1)}" stroke="${color}" stroke-width="2" stroke-linecap="round"/>`);
    }
    const dots = pts.map((p, i) => {
      const v = vals[i];
      if (v == null) return "";
      const color = phaseColor[p.phase] || "#94a3b8";
      const isLast = i === pts.length - 1;
      return `<circle cx="${xOf(i).toFixed(1)}" cy="${yOf(v).toFixed(1)}" r="${isLast ? 3.2 : 1.8}" fill="${color}"/>`;
    }).join("");
    const labels = pts.map((p, i) => {
      if (i !== 0 && i !== pts.length - 1) return "";
      return `<text x="${xOf(i).toFixed(1)}" y="${H - 1}" font-size="8" fill="#94a3b8" text-anchor="${i === 0 ? "start" : "end"}">${esc((p.date || "").slice(5))}</text>`;
    }).join("");
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="w-full" style="height:${H}px" preserveAspectRatio="none">`
      + `<line x1="${PAD}" y1="${yOf(0.5).toFixed(1)}" x2="${W - PAD}" y2="${yOf(0.5).toFixed(1)}" stroke="#e2e8f0" stroke-width="1" stroke-dasharray="3 3"/>`
      + segs.join("") + dots + labels
      + `</svg>`
      + `<div class="flex flex-wrap gap-x-3 gap-y-0.5 text-[9.5px] text-ink-faint mt-0.5">`
      + Object.entries({ euphoria: "亢奋", advance: "主升", repair: "修复", freezing: "冰点", ebb: "退潮", collapse: "崩塌" })
        .map(([k, v]) => `<span><i class="inline-block w-2 h-2 rounded-full mr-0.5" style="background:${phaseColor[k]}"></i>${v}</span>`).join("")
      + `</div>`;
  }

  try {
    const [ms, us, ps, ar, st, acct, sp, sh] = await Promise.allSettled([
      fetchJSON("/api/stock/monitor_settings"),
      fetchJSON("/api/stock/settings"),
      fetchJSON("/api/stock/prefer_source"),
      fetchJSON("/api/stock/alert_rules"),
      fetchJSON("/api/stock/strategy/list"),
      fetchJSON("/api/stock/account"),
      fetchJSON("/api/stock/sentiment_policy"),
      fetchJSON("/api/stock/sentiment_history"),
    ]);
    if (ms.status === "fulfilled") {
      const m = ms.value || {};
      qs("#setMonitor").checked = !!m.enabled;
      qs("#setCapital").value = us.status === "fulfilled" ? (us.value.total_capital || "") : "";
      totalCapital = us.status === "fulfilled" ? Number(us.value.total_capital || 0) : 0;
      if (us.status === "fulfilled") qs("#setAmmo").value = us.value.available_cash ?? "";
      if (us.status === "fulfilled") qs("#setStyle").value = us.value.investment_style || "long";
      qs("#setInterval").value = m.interval_sec ?? 120;
      qs("#setFreq").value = m.monitor_kline_frequency || "d";
      qs("#setDing").checked = !!m.dingtalk_enabled;
      qs("#setDingWebhook").value = m.dingtalk_webhook_url || "";
      qs("#setDingSecret").value = m.dingtalk_secret || "";
      qs("#xqCookie").value = m.xueqiu_cookie || "";
      qs("#xqUA").value = m.xueqiu_user_agent || "";
    }
    if (acct.status === "fulfilled") { accountCache = acct.value || {}; renderAcctStatus(accountCache); }
    if (sp.status === "fulfilled") { renderSentimentPanel(sp.value || {}); }
    if (sh.status === "fulfilled") { renderSentimentCurve(sh.value || {}); }
    // 本金加载晚于行情时，持仓概览的总收益率需要按新口径重算一次
    if (favoritesCache.length) { try { renderPositionSummary(); renderHoldingsOverview(); } catch (e) {} }
    if (ps.status === "fulfilled") { preferSource = ps.value.prefer_source || "xueqiu"; applySourceUI(); }
    if (ar.status === "fulfilled") { alertRulesCache = ar.value || {}; applyAlertRulesUI(); }
    if (st.status === "fulfilled") {
      const sel = qs("#monitorStrategy");
      (st.value.data || []).forEach(s => { const o = document.createElement("option"); o.value = s.name; o.textContent = s.name; sel.appendChild(o); });
      try { const cur = await fetchJSON("/api/stock/monitor_strategy"); sel.value = cur.name || ""; } catch (e) {}
    }
  } catch (e) { console.warn("加载设置失败", e); }
}
function applyAlertRulesUI() {
  const d = alertRulesCache?.dingtalk?.rules || {};
  const up = d.pct_change_up || {}, down = d.pct_change_down || {};
  qs("#alertUpEnabled").checked = !!up.enabled;
  qs("#alertUpThreshold").value = up.threshold ?? 3;
  qs("#alertUpLevels").value = (up.levels || [3,5,8]).join(",");
  qs("#alertUpReset").value = up.reset_below ?? 2;
  qs("#alertDownEnabled").checked = !!down.enabled;
  qs("#alertDownThreshold").value = Math.abs(Number(down.threshold ?? -3) || 3);
  qs("#alertDownLevels").value = (down.levels || [3,5,8]).join(",");
  qs("#alertDownReset").value = down.reset_below ?? 2;
  qs("#alertStrategyEnabled").checked = !!(d.strategy_signal?.enabled ?? true);
}
function applySourceUI() {
  const on = preferSource === "amazingdata";
  qs("#sideSource").textContent = on ? "星耀数智" : "雪球";
  qs("#topSource").textContent = on ? "星耀数智" : "雪球";
  const pill = qs("#srcPill");
  pill.textContent = on ? "星耀数智 · 在线" : "雪球 · 兜底";
  pill.className = "tag " + (on ? "bg-down/10 text-down" : "bg-amber-100 text-amber-600");
  qsa("#srcSeg .seg-btn").forEach(b => b.classList.toggle("active", b.dataset.src === preferSource));
  qs("#xqBox").classList.toggle("hidden", on);
  renderIndices();   // 数据源切换后即时刷新顶部状态卡
}

/* ---------------- 回测 ---------------- */
let btStrategies = [];
let btResult = null;

function btISO(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function initBacktestDefaults() {
  if (qs("#btEnd") && !qs("#btEnd").value) qs("#btEnd").value = btISO(0);
  if (qs("#btStart") && !qs("#btStart").value) qs("#btStart").value = btISO(-365);
  if (qs("#btCapital") && !qs("#btCapital").value && Number(totalCapital) > 0) {
    qs("#btCapital").value = String(Math.round(totalCapital));
  }
}

async function loadBacktestOptions() {
  const selStock = qs("#btStock");
  if (!selStock) return;
  const favs = (favoritesCache || [])
    .map((s) => ({ id: s.secID || "", name: s.name || s.secID || "" }))
    .filter((x) => x.id);
  selStock.innerHTML = favs.length
    ? favs.map((s) => `<option value="${escapeHTML(s.id)}">${escapeHTML(s.name)} · ${escapeHTML(s.id)}</option>`).join("")
    : `<option value="">（暂无自选股，可在下方手动输入代码）</option>`;
  try {
    const r = await fetchJSON("/api/stock/strategy/list");
    btStrategies = (r?.data || []).filter((s) => s && s.entry_expr);
  } catch (_) {
    btStrategies = [];
  }
  const selStrategy = qs("#btStrategy");
  selStrategy.innerHTML = [`<option value="__custom__">自定义表达式</option>`]
    .concat(btStrategies.map((s, i) => `<option value="${i}">${escapeHTML(s.name || `策略 ${i + 1}`)}</option>`))
    .join("");
  if (btStrategies.length) {
    selStrategy.value = "0";
    applyBacktestStrategy();
  }
  initBacktestDefaults();
}

function applyBacktestStrategy() {
  const v = qs("#btStrategy").value;
  if (v === "__custom__") return;
  const s = btStrategies[Number(v)];
  if (!s) return;
  qs("#btEntry").value = s.entry_expr || "";
  qs("#btExit").value = s.exit_expr || "";
}

function btNum(sel, fallback = 0) {
  const n = Number(qs(sel)?.value);
  return Number.isFinite(n) ? n : fallback;
}

async function runBacktest() {
  const manual = qs("#btCode").value.trim();
  const code = manual || qs("#btStock").value;
  if (!code) throw new Error("请先选择自选股或输入股票代码");
  const entry = qs("#btEntry").value.trim();
  const exit = qs("#btExit").value.trim();
  if (!entry) throw new Error("请选择策略或填写入场表达式");
  const payload = {
    code,
    start_date: qs("#btStart").value || undefined,
    end_date: qs("#btEnd").value || undefined,
    capital: btNum("#btCapital", 100000),
    entry_expr: entry,
    exit_expr: exit,
    benchmark: qs("#btBench").value || "hs300",
    fee_rate: btNum("#btFee", 0.075) / 100,
    slippage_pct: btNum("#btSlippage", 0.1) / 100,
    position_pct: Math.min(1, Math.max(0.05, btNum("#btPosition", 100) / 100)),
    strategy_name: qs("#btStrategy").selectedOptions?.[0]?.textContent || "自定义",
  };
  const r = await fetchJSON("/api/stock/backtest", { method: "POST", body: JSON.stringify(payload) });
  btResult = r;
  renderBacktest(r);
}

function btTone(num) {
  if (num === null || num === undefined || !Number.isFinite(Number(num))) return "";
  const n = Number(num);
  return n > 0 ? "text-up" : (n < 0 ? "text-down" : "text-flat");
}

function btCard(label, text, tone = "") {
  return `<div class="rounded-xl bg-brand-50/60 p-3 min-w-0">
    <div class="text-[12px] text-ink-muted">${escapeHTML(label)}</div>
    <div class="mt-1 font-bold kbox ${tone}">${escapeHTML(text)}</div>
  </div>`;
}

function btEquitySvg(points) {
  if (!points || points.length < 2) return `<div class="text-[12px] hint py-10 text-center">数据点不足，无法绘制曲线</div>`;
  const W = 860, H = 280, padT = 16, padB = 26, padX = 10;
  const strat = points.map((p) => Number(p.strategy ?? 1));
  const bench = points.map((p) => Number(p.benchmark ?? 1));
  const all = strat.concat(bench).filter((v) => Number.isFinite(v));
  let hi = Math.max(...all), lo = Math.min(...all);
  if (!(hi > lo)) { hi = lo + 0.01; }
  const x = (i) => padX + (i / (points.length - 1)) * (W - 2 * padX);
  const y = (v) => padT + ((hi - v) / (hi - lo)) * (H - padT - padB);
  const line = (arr) => arr.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = hi - (i / 4) * (hi - lo);
    const yy = y(v).toFixed(1);
    grid += `<line x1="${padX}" y1="${yy}" x2="${W - padX}" y2="${yy}" stroke="#e6edf5" stroke-width="1"/>`;
    grid += `<text x="${W - padX - 2}" y="${Number(yy) - 3}" text-anchor="end" font-size="10" fill="#8a99ad">${(v * 100).toFixed(0)}</text>`;
  }
  const first = escapeHTML(points[0].date || "");
  const last = escapeHTML(points[points.length - 1].date || "");
  return `<svg width="100%" viewBox="0 0 ${W} ${H}" role="img" aria-label="策略净值与基准对比曲线">
    ${grid}
    <polyline points="${line(bench)}" fill="none" stroke="#8a99ad" stroke-width="1.4" stroke-dasharray="4 3"/>
    <polyline points="${line(strat)}" fill="none" stroke="#1677ff" stroke-width="2"/>
    <text x="${padX}" y="${H - 6}" font-size="10" fill="#8a99ad">${first}</text>
    <text x="${W - padX}" y="${H - 6}" font-size="10" fill="#8a99ad" text-anchor="end">${last}</text>
    <text x="${W / 2}" y="${H - 6}" font-size="10" fill="#8a99ad" text-anchor="middle">净值（100 = 初始资金）</text>
  </svg>`;
}

function renderBacktest(r) {
  // 复利曲线/费用口径由 admin.js 的扩展面板渲染（保持主渲染逻辑单一职责）
  if (typeof window.__onBacktestResult === "function") window.__onBacktestResult(r);
  const m = r.metrics || {};
  const b = r.benchmark || {};
  const parts = [
    `<b class="text-ink">${escapeHTML(r.name || r.code)}</b> ${escapeHTML(r.start_date)} ~ ${escapeHTML(r.end_date)}（${r.bars} 个交易日）`,
    `初始 ${yuan(r.initial_capital)} → 期末 <b class="text-ink">${yuan(r.final_capital)}</b>`,
  ];
  if (r.hold_return_pct !== null && r.hold_return_pct !== undefined) {
    parts.push(`同期买入持有 <span class="${btTone(r.hold_return_pct)} kbox">${signOf(r.hold_return_pct)}${fmt(r.hold_return_pct)}%</span>`);
  }
  if (b.available && b.return_pct !== null && b.return_pct !== undefined) {
    parts.push(`${escapeHTML(b.name)} <span class="${btTone(b.return_pct)} kbox">${signOf(b.return_pct)}${fmt(b.return_pct)}%</span>`);
  }
  let summary = parts.join(" ｜ ");
  if (Array.isArray(r.warnings) && r.warnings.length) {
    summary += `<div class="mt-2 text-[11px] text-amber-600">⚠ ${r.warnings.map(escapeHTML).join("；")}</div>`;
  }
  qs("#btSummary").innerHTML = summary;

  const total = m.total_return_pct;
  const annual = m.annual_return_pct;
  const mdd = m.max_drawdown_pct;
  const sharpe = m.sharpe;
  const winRate = m.win_rate_pct;
  qs("#btMetrics").innerHTML = [
    btCard("总收益率", `${signOf(total)}${fmt(total ?? 0)}%`, btTone(total)),
    btCard("年化收益", annual === null || annual === undefined ? "-" : `${signOf(annual)}${fmt(annual)}%`, btTone(annual)),
    btCard("最大回撤", mdd === null || mdd === undefined ? "-" : `-${fmt(Math.abs(mdd))}%`, "text-down"),
    btCard("夏普比率", sharpe === null || sharpe === undefined ? "-" : fmt(sharpe), btTone(sharpe)),
    btCard("胜率", winRate === null || winRate === undefined ? "-" : `${fmt(winRate)}%`, ""),
    btCard("交易次数", `${m.trade_count ?? 0} 笔`, ""),
    btCard("基准收益", b.available && b.return_pct !== null && b.return_pct !== undefined ? `${signOf(b.return_pct)}${fmt(b.return_pct)}%` : "-", btTone(b.available ? b.return_pct : null)),
    btCard("超额收益", r.excess_pct === null || r.excess_pct === undefined ? "-" : `${signOf(r.excess_pct)}${fmt(r.excess_pct)}%`, btTone(r.excess_pct)),
  ].join("");

  qs("#btEquity").innerHTML = btEquitySvg(r.equity);

  const trades = r.trades || [];
  qs("#btTradeCount").textContent = trades.length ? `共 ${trades.length} 条成交记录` : "无成交";
  qs("#btTrades").innerHTML = trades.length
    ? trades.slice(0, 300).map((t) => {
        const isBuy = t.side === "buy";
        const pnl = t.pnl_pct;
        const pnlText = pnl === null || pnl === undefined ? "—" : `${signOf(pnl)}${fmt(pnl)}%`;
        return `<tr>
          <td class="text-right text-ink-faint kbox">${escapeHTML(t.date || "")}</td>
          <td class="text-right"><span class="tag ${isBuy ? "bg-brand-50 text-brand-600" : "bg-up/10 text-up"}">${isBuy ? "买入" : "卖出"}</span></td>
          <td class="text-right kbox">${fmt(t.price, 2)}</td>
          <td class="text-right kbox">${Number(t.shares || 0).toLocaleString("zh-CN")}</td>
          <td class="text-right kbox text-ink-faint">${fmt(t.fee, 2)}</td>
          <td class="text-right kbox ${pnl === null || pnl === undefined ? "" : btTone(pnl)}">${pnlText}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="6" class="text-center muted py-6">区间内未产生交易，可放宽入场条件或拉长区间</td></tr>`;

  qs("#btDiagOut").classList.add("hidden");
  qs("#btDiagOut").innerHTML = "";
}

function diagnoseBacktest() {
  if (!btResult) { showToast("请先运行回测", "没有回测结果可诊断", "error"); return; }
  const m = btResult.metrics || {};
  const b = btResult.benchmark || {};
  const tips = [];
  if (Number(m.max_drawdown_pct) >= 20) {
    tips.push(`最大回撤 ${fmt(m.max_drawdown_pct)}%，回撤偏大。建议收紧止损（如 -8% ~ -10%），或把单笔仓位降到 60% 以下。`);
  }
  if (m.sharpe !== null && m.sharpe !== undefined && Number(m.sharpe) < 1) {
    tips.push(`夏普比率 ${fmt(m.sharpe)}，风险调整后收益不足。建议加入趋势/量能过滤，减少震荡市里的无效交易。`);
  }
  if (btResult.excess_pct !== null && btResult.excess_pct !== undefined && Number(btResult.excess_pct) < 0) {
    tips.push(`相对基准跑输 ${fmt(Math.abs(btResult.excess_pct))}%。建议减少频繁进出，趋势确认后再加仓。`);
  }
  if (m.win_rate_pct !== null && m.win_rate_pct !== undefined && Number(m.win_rate_pct) < 50) {
    tips.push(`胜率 ${fmt(m.win_rate_pct)}%，入场条件偏松。建议叠加确认条件（突破 + 回踩不破、量能同步放大）。`);
  }
  if (btResult.hold_return_pct !== null && btResult.hold_return_pct !== undefined
      && Number(btResult.return_pct) < Number(btResult.hold_return_pct)) {
    tips.push(`策略收益 ${fmt(btResult.return_pct)}% 低于同期买入持有 ${fmt(btResult.hold_return_pct)}%，择时未创造价值；可考虑降低交易频率或改做定投式加仓。`);
  }
  if (Number(m.trade_count) > 0 && Number(m.trade_count) <= 2) {
    tips.push(`仅 ${m.trade_count} 笔完整交易，样本过少，结论不稳健。建议拉长回测区间或放宽入场条件。`);
  }
  if (Number(m.trade_count) > 30) {
    tips.push(`交易 ${m.trade_count} 笔偏频繁，手续费与滑点会持续侵蚀收益，建议提高信号门槛。`);
  }
  if (!tips.length) {
    tips.push("当前指标结构较健康（回撤可控、跑赢基准、胜率过半），可适度提高仓位或延长持有周期。");
  }
  tips.push("以上为规则化诊断（AI 版本后续接入）。调整后请重新回测验证，避免只看单一指标。");
  const box = qs("#btDiagOut");
  box.classList.remove("hidden");
  box.innerHTML = tips.map((t) => `<div class="flex gap-2 items-start rounded-xl border border-line p-3 text-[13px]"><span class="text-brand-600 font-bold">›</span><span class="text-ink-muted">${escapeHTML(t)}</span></div>`).join("");
}

/* ---------------- 事件绑定 ---------------- */
function switchView(v) {
  qsa(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  qsa(".view").forEach(s => s.classList.toggle("active", s.id === "view-" + v));
  const T = {
    overview: ["总览看板", "市场概览 · 自选实时行情 · 监控预警一站查看"],
    watchlist: ["自选股", "管理自选列表，点击卡片查看个股详情"],
    pools: ["四池配置", "股息 / 催化 / 长赛道 / 低估 + 现金，Agent 建议经两道确认才执行"],
    calculator: ["差价计算器", "按持仓计算卖出后保本买回价格"],
    backtest: ["回测", "基于日线验证表达式策略，并对比基准"],
    performance: ["绩效复盘", "净值曲线、回撤、胜率与基准对比，全部由真实流水算出"],
    settings: ["系统设置", "监控、数据源与钉钉提醒"],
    copilot: ["分析顾问", "用自然语言问，Agent 自己调数据、自己算、自己下判断"],
    review: ["信号复核", "在监控信号推送到钉钉之前，先让 Agent 判一次真假"],
    daily: ["盘后复盘", "收盘后自动生成复盘日报，含归因与明日预案"],
    doctor: ["策略诊断", "回测跑完不是终点，Agent 告诉你为什么亏、怎么改"],
    risk: ["持仓风险", "组合视角看集中度、行业暴露与止损距离"],
    orchestration: ["Agent 编排", "数据源 → 工具 → 场景包，Agent 如何挂在平台上"],
  }[v];
  qs("#pageTitle").textContent = T[0]; qs("#pageSub").textContent = T[1];
  if (typeof window.__agentOnView === "function") window.__agentOnView(v);
  // 绩效/运维卡片由 ops.js 独立维护，按需懒加载，避免拖慢首屏
  if (typeof window.__opsOnView === "function") window.__opsOnView(v);
  if (typeof window.__adminOnView === "function") window.__adminOnView(v);
  if (typeof window.__poolsOnView === "function") window.__poolsOnView(v);
  // 小白层：回总览时刷新「一句话结论」，进专业视图时自动展开隐藏的导航
  if (typeof window.__simpleOnView === "function") window.__simpleOnView(v);
}

function bindEvents() {
  // 导航
  qs("#nav").addEventListener("click", e => { const b = e.target.closest(".nav-item"); if (b) { const v = b.dataset.view; switchView(v); if (v === "factors") loadFactors(); } });
  // 自选分组筛选
  qs("#wlGroupTabs")?.addEventListener("click", e => {
    const b = e.target.closest("[data-group]");
    if (!b) return;
    activeGroup = b.dataset.group;
    renderWatchlist();
  });
  // 折叠：收起后只剩图标，所以①箭头要翻向 ②给每项补 title ③记住状态，别每次刷新都弹回来
  const sidebarEl = qs("#sidebar");
  const collapseBtn = qs("#collapseBtn");
  function syncSidebarCollapsed() {
    const collapsed = sidebarEl.classList.contains("collapsed");
    collapseBtn.textContent = collapsed ? "›" : "‹";
    collapseBtn.title = collapsed ? "展开侧栏" : "收起侧栏";
    qsa("#nav .nav-item").forEach(btn => {
      const label = (btn.querySelector("span")?.textContent || "").trim();
      btn.title = collapsed && label ? label : "";
    });
    try { localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0"); } catch (e) {}
  }
  try { if (localStorage.getItem(SIDEBAR_KEY) === "1") sidebarEl.classList.add("collapsed"); } catch (e) {}
  syncSidebarCollapsed();
  collapseBtn.addEventListener("click", () => { sidebarEl.classList.toggle("collapsed"); syncSidebarCollapsed(); });
  // 刷新
  qs("#refreshBtn").addEventListener("click", async () => {
    const btn = qs("#refreshBtn"); setBusy(btn, true, "刷新中…");
    try {
      await Promise.allSettled([loadFavorites(true), loadMonitor(true, "manual")]);
      showToast("已刷新", "行情已更新", "info");
    } finally { setBusy(btn, false); }
  });
  // 自动刷新（每30秒全局刷新自选行情+指数；状态持久化到 localStorage，换页/刷新后自动恢复）
  qs("#autoToggle").addEventListener("change", e => {
    if (e.target.checked) {
      startAutoRefresh();
      localStorage.setItem(AUTO_REFRESH_KEY, "1");
      showToast("自动刷新开启", "每 30 秒刷新行情", "info");
    } else {
      stopAutoRefresh();
      localStorage.removeItem(AUTO_REFRESH_KEY);
      showToast("自动刷新已关闭", "", "info");
    }
  });
  // 帮助
  qs("#helpBtn").addEventListener("click", () => qs("#helpMask").classList.remove("hidden"));
  qs("#helpClose").addEventListener("click", () => qs("#helpMask").classList.add("hidden"));
  qs("#helpMask").addEventListener("click", e => { if (e.target === qs("#helpMask")) qs("#helpMask").classList.add("hidden"); });
  // 抽屉
  qs("#drawerClose").addEventListener("click", closeDetail);
  qs("#drawerMask").addEventListener("click", closeDetail);
  document.addEventListener("keydown", e => { if (e.key === "Escape") { closeDetail(); qs("#helpMask").classList.add("hidden"); } });
  // 总览表 / 自选卡片 点击
  document.addEventListener("click", e => {
    const del = e.target.closest('[data-act="del"]');
    if (del) { const code = del.dataset.code; runAction(del, async () => { await fetchJSON("/api/stock/favorites/remove", { method:"POST", body: JSON.stringify({ secID: code }) }); await loadFavorites(true); await loadMonitor(true); }, { success: "已移除自选股", failure: "移除失败" }); return; }
    const det = e.target.closest('[data-act="detail"]');
    if (det) { openDetail(det.dataset.code); return; }
    const row = e.target.closest("tr[data-code]");
    if (row && !e.target.closest("[data-act]")) { openDetail(row.dataset.code); }
    const card = e.target.closest('[data-code][data-act="detail"]');
    if (card) { /* handled above */ }
  });
  // 加入自选
  qs("#addFav").addEventListener("click", () => runAction(qs("#addFav"), async () => {
    const code = qs("#favCode").value.trim(), name = qs("#favName").value.trim(), pool = qs("#favPool").value;
    const group = (qs("#favGroup")?.value || "").trim();
    if (!code) throw new Error("请先输入股票代码");
    await fetchJSON("/api/stock/favorites/add", { method:"POST", body: JSON.stringify({ secID: code, name: name || null, pool, group }) });
    qs("#favCode").value = ""; qs("#favName").value = "";
    await Promise.allSettled([loadFavorites(true), loadMonitor(true)]);
  }, { success: "已加入自选股", failure: "加入自选失败" }));
  // 计算器
  ["calcStock","calcPrice","calcShares","calcCost","calcFee"].forEach(id => qs("#" + id)?.addEventListener("input", calc));
  qs("#calcStock")?.addEventListener("change", () => { const s = favoritesCache.find(x => x.secID === qs("#calcStock").value); if (s) { qs("#calcPrice").value = s.price ?? s.spot?.price ?? ""; qs("#calcShares").value = s.shares ?? ""; qs("#calcCost").value = s.cost ?? ""; } calc(); });
  // 设置：监控
  qs("#saveMonitorSet").addEventListener("click", () => runAction(qs("#saveMonitorSet"), async () => {
    totalCapital = parseNonNegativeAmount(qs("#setCapital").value, "本金");
    // 可用资金：留空 = 不提交（后端保持原值/退化为估算）；填 0 = 满仓（这是真值，不是"没填"）
    const ammoRaw = qs("#setAmmo").value.trim();
    const settingsBody = { total_capital: totalCapital, investment_style: qs("#setStyle").value || "long" };
    if (ammoRaw !== "") settingsBody.available_cash = parseNonNegativeAmount(ammoRaw, "账户可用资金");
    await fetchJSON("/api/stock/settings", { method:"POST", body: JSON.stringify(settingsBody) });
    await fetchJSON("/api/stock/monitor_settings", { method:"POST", body: JSON.stringify({
      monitor_enabled: qs("#setMonitor").checked,
      monitor_interval_sec: Number(qs("#setInterval").value || 120),
      monitor_kline_frequency: qs("#setFreq").value || "d",
      dingtalk_enabled: qs("#setDing").checked,
      dingtalk_webhook_url: qs("#setDingWebhook").value.trim(),
      dingtalk_secret: qs("#setDingSecret").value.trim(),
      xueqiu_cookie: qs("#xqCookie").value.trim(),
      xueqiu_user_agent: qs("#xqUA").value.trim(),
    }) });
    await loadFavorites(false);
  }, { success: "监控设置已保存", failure: "保存失败" }));
  qs("#saveMonitorStrategy").addEventListener("click", () => runAction(qs("#saveMonitorStrategy"), async () => {
    await fetchJSON("/api/stock/monitor_strategy", { method:"POST", body: JSON.stringify({ name: qs("#monitorStrategy").value || "" }) });
    await loadMonitor(true);
  }, { success: "监控策略已保存", failure: "保存失败" }));
  // 设置：数据源切换
  qs("#srcSeg").addEventListener("click", e => { const b = e.target.closest(".seg-btn"); if (!b) return; preferSource = b.dataset.src; applySourceUI(); });
  qs("#saveSrc").addEventListener("click", () => runAction(qs("#saveSrc"), async () => {
    const r = await fetchJSON("/api/stock/prefer_source", { method:"POST", body: JSON.stringify({ prefer_source: preferSource }) });
    preferSource = r.prefer_source; applySourceUI();
    await loadFavorites(true);
  }, { success: "数据源已切换", failure: "切换失败" }));
  // 设置：钉钉测试
  qs("#testDing").addEventListener("click", () => runAction(qs("#testDing"), async () => {
    const r = await fetchJSON("/api/stock/notify/dingtalk/test", { method:"POST", body: JSON.stringify({}) });
    if (!r?.ok) throw new Error(r?.reason || "未知错误");
  }, { success: "测试提醒已发送", failure: "发送失败", busy: "发送中..." }));
  // 设置：提醒规则
  qs("#saveAlertRules").addEventListener("click", () => runAction(qs("#saveAlertRules"), async () => {
    const pct = v => Math.max(0, Math.round(Number(v || 0) * 10) / 10);
    const levels = v => String(v || "").split(",").map(x => pct(x.trim())).filter(x => x > 0);
    const payload = { dingtalk: { enabled: true, max_items: alertRulesCache?.dingtalk?.max_items || 20, auto_dedupe_seconds: alertRulesCache?.dingtalk?.auto_dedupe_seconds || 1800, rules: {
      pct_change_up: { enabled: qs("#alertUpEnabled").checked, threshold: pct(qs("#alertUpThreshold").value), levels: levels(qs("#alertUpLevels").value), reset_below: pct(qs("#alertUpReset").value || 2), operator: ">=" },
      pct_change_down: { enabled: qs("#alertDownEnabled").checked, threshold: -pct(qs("#alertDownThreshold").value), levels: levels(qs("#alertDownLevels").value), reset_below: pct(qs("#alertDownReset").value || 2), operator: "<=" },
      strategy_signal: { enabled: qs("#alertStrategyEnabled").checked },
    } } };
    const r = await fetchJSON("/api/stock/alert_rules", { method:"POST", body: JSON.stringify(payload) });
    alertRulesCache = r || payload;
  }, { success: "提醒规则已保存", failure: "保存失败" }));
  // 回测：策略下拉 / 表达式输入 / 运行 / 诊断
  qs("#btStrategy").addEventListener("change", applyBacktestStrategy);
  ["#btEntry", "#btExit"].forEach((sel) => qs(sel).addEventListener("input", () => {
    const s = qs("#btStrategy");
    if (s.value !== "__custom__") s.value = "__custom__";
  }));
  qs("#btRun").addEventListener("click", () => runAction(qs("#btRun"), runBacktest, { success: "回测完成", failure: "回测失败", busy: "回测中…" }));
  qs("#btDiag").addEventListener("click", diagnoseBacktest);
  // 策略因子库
  qs("#factorNew").addEventListener("click", () => openFactorModal(null));
  qs("#factorSave").addEventListener("click", () => runAction(qs("#factorSave"), saveFactor, { success: "因子已保存", failure: "保存失败", busy: "保存中…" }));
  qs("#factorCancel").addEventListener("click", closeFactorModal);
  qs("#factorClose").addEventListener("click", closeFactorModal);
  qs("#factorBackdrop").addEventListener("click", closeFactorModal);
  qs("#factorRows").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const name = b.dataset.name;
    if (b.dataset.act === "edit") {
      const f = factorRowsCache.find((x) => x.name === name);
      if (f) openFactorModal(f);
    } else if (b.dataset.act === "del") {
      deleteFactor(name);
    } else if (b.dataset.act === "load") {
      loadFactorIntoBacktest(name);
    }
  });
  qs("#btSaveFactor").addEventListener("click", saveBacktestAsFactor);

  // 抽屉内保存持仓
  qs("#dSavePos").addEventListener("click", () => runAction(qs("#dSavePos"), async () => {
    if (!currentDrawerSecID) return false;
    const cost = parseMoneyInput(qs("#dCost").value);
    await fetchJSON("/api/stock/favorites/update_position", { method:"POST", body: JSON.stringify({ secID: currentDrawerSecID, shares: Number(qs("#dShares").value || 0), cost, pool: qs("#dPool").value || "" }) });
    await Promise.allSettled([loadFavorites(true), loadMonitor(true)]);
    await Promise.allSettled([loadFavorites(true), loadMonitor(true)]);
    closeDetail();
  }, { success: "持仓信息已保存", failure: "保存失败" }));
}

/* ---------------- 策略因子库 ---------------- */
let factorRowsCache = [];
let _editingFactorName = null;

async function loadFactors() {
  try {
    const r = await fetchJSON("/api/stock/strategy/list");
    factorRowsCache = (r?.data || []).filter((s) => s && (s.entry_expr || s.name));
  } catch (e) {
    factorRowsCache = [];
  }
  renderFactorRows();
}

function _fnum(v, d = 2) { const n = Number(v); return Number.isFinite(n) ? n.toFixed(d) : "—"; }
function _fpct(v) { const n = Number(v); return Number.isFinite(n) ? (n > 0 ? "+" : "") + n.toFixed(2) + "%" : "—"; }
function _tone(v) { return v == null ? "" : (v > 0 ? "text-up" : (v < 0 ? "text-down" : "text-flat")); }

function renderFactorRows() {
  const tbody = qs("#factorRows");
  if (!tbody) return;
  if (!factorRowsCache.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center muted py-6">还没有保存的因子。点右上角「+ 新建因子」，或在回测页把验证过的策略点「存为因子」。</td></tr>`;
    return;
  }
  const benchName = { hs300: "沪深300", zz500: "中证500", sh: "上证指数", sz: "深证成指", none: "不对比" };
  tbody.innerHTML = factorRowsCache.map((f) => {
    const m = f.metrics;
    const recent = (m && typeof m === "object")
      ? `收益${_fpct(m.total_return_pct)} 回撤${_fnum(m.max_drawdown_pct)}% 夏普${_fnum(m.sharpe)} 胜率${_fnum(m.win_rate_pct)}% (${m.trade_count || 0}笔)`
      : "—";
    const ret = m ? m.total_return_pct : null;
    return `<tr data-name="${escapeHTML(f.name)}">
      <td class="font-semibold">${escapeHTML(f.name)}</td>
      <td class="text-ink-muted">${escapeHTML(f.description || "—")}</td>
      <td class="kbox text-[12px]">${escapeHTML(f.entry_expr || "—")}</td>
      <td class="kbox text-[12px]">${escapeHTML(f.exit_expr || "—")}</td>
      <td>${benchName[f.benchmark] || f.benchmark || "hs300"}</td>
      <td class="text-right ${_tone(ret)}">${recent}</td>
      <td class="text-ink-faint text-[12px]">${escapeHTML(f.updated_at || f.created_at || "—")}</td>
      <td class="whitespace-nowrap">
        <button class="text-brand-600 text-[13px] font-semibold hover:underline" data-act="load" data-name="${escapeHTML(f.name)}">载入回测</button>
        <button class="text-brand-600 text-[13px] font-semibold hover:underline ml-3" data-act="edit" data-name="${escapeHTML(f.name)}">编辑</button>
        <button class="text-down text-[13px] font-semibold hover:underline ml-3" data-act="del" data-name="${escapeHTML(f.name)}">删除</button>
      </td>
    </tr>`;
  }).join("");
}

function openFactorModal(factor) {
  _editingFactorName = factor ? factor.name : null;
  qs("#factor-modal-title").textContent = factor ? "编辑因子" : "新建因子";
  qs("#fName").value = factor?.name || "";
  qs("#fName").disabled = !!factor;
  qs("#fDesc").value = factor?.description || "";
  qs("#fEntry").value = factor?.entry_expr || "";
  qs("#fExit").value = factor?.exit_expr || "";
  qs("#fBench").value = factor?.benchmark || "hs300";
  qs("#fCapital").value = (factor?.capital != null) ? factor.capital : 100000;
  qs("#fFee").value = (factor?.fee_rate != null) ? (Number(factor.fee_rate) * 100) : 0.075;
  qs("#fSlip").value = (factor?.slippage_pct != null) ? (Number(factor.slippage_pct) * 100) : 0.1;
  qs("#fPos").value = (factor?.position_pct != null) ? Math.round(Number(factor.position_pct) * 100) : 100;
  qs("#factor-modal").classList.remove("hidden");
}

function closeFactorModal() { qs("#factor-modal").classList.add("hidden"); }

async function saveFactor() {
  const name = (_editingFactorName || "").trim() || qs("#fName").value.trim();
  const entry = qs("#fEntry").value.trim();
  if (!name) throw new Error("请填写因子名称");
  if (!entry) throw new Error("请填写入场条件");
  const payload = {
    name,
    description: qs("#fDesc").value.trim(),
    entry_expr: entry,
    exit_expr: qs("#fExit").value.trim(),
    benchmark: qs("#fBench").value,
    capital: Number(qs("#fCapital").value) || 100000,
    fee_rate: (Number(qs("#fFee").value) || 0) / 100,
    slippage_pct: (Number(qs("#fSlip").value) || 0) / 100,
    position_pct: Math.min(1, Math.max(0.05, (Number(qs("#fPos").value) || 100) / 100)),
  };
  await fetchJSON("/api/stock/strategy/save", { method: "POST", body: JSON.stringify(payload) });
  closeFactorModal();
  await loadFactors();
}

async function deleteFactor(name) {
  if (!window.confirm(`确定删除因子「${name}」？此操作不可撤销。`)) return;
  await fetchJSON("/api/stock/strategy/delete", { method: "POST", body: JSON.stringify({ name }) });
  await loadFactors();
}

function loadFactorIntoBacktest(name) {
  const f = factorRowsCache.find((x) => x.name === name);
  if (!f) return;
  switchView("backtest");
  qs("#btStrategy").value = "__custom__";
  qs("#btEntry").value = f.entry_expr || "";
  qs("#btExit").value = f.exit_expr || "";
  qs("#btBench").value = f.benchmark || "hs300";
  qs("#btCapital").value = (f.capital != null) ? f.capital : 100000;
  qs("#btFee").value = (f.fee_rate != null) ? (Number(f.fee_rate) * 100) : 0.075;
  qs("#btSlippage").value = (f.slippage_pct != null) ? (Number(f.slippage_pct) * 100) : 0.1;
  qs("#btPosition").value = (f.position_pct != null) ? Math.round(Number(f.position_pct) * 100) : 100;
  showToast("已载入", `因子「${name}」已填入回测配置，选择标的后点运行回测`, "success");
}

function saveBacktestAsFactor() {
  const entry = qs("#btEntry").value.trim();
  if (!entry) { showToast("提示", "请先填写入场条件或选择已保存策略", "error"); return; }
  const prefill = {
    name: "",
    description: "",
    entry_expr: entry,
    exit_expr: qs("#btExit").value.trim(),
    benchmark: qs("#btBench").value || "hs300",
    capital: Number(qs("#btCapital").value) || 100000,
    fee_rate: (Number(qs("#btFee").value) || 0) / 100,
    slippage_pct: (Number(qs("#btSlippage").value) || 0) / 100,
    position_pct: Math.min(1, Math.max(0.05, (Number(qs("#btPosition").value) || 100) / 100)),
  };
  if (typeof btResult !== "undefined" && btResult && btResult.ok && btResult.metrics) prefill.metrics = btResult.metrics;
  openFactorModal(prefill);
}

/* ---------------- 初始化 ---------------- */
function setInitialLoading() {
  const sk = `<tr><td colspan="7" class="text-center muted py-6">加载中…</td></tr>`;
  qs("#ovTable").innerHTML = sk;
  qs("#holdingsTable").innerHTML = sk;
  qs("#wlGrid").innerHTML = `<div class="muted col-span-full py-6">加载中…</div>`;
  qs("#indexStrip").innerHTML = `<div class="bg-white border border-line rounded-2xl shadow-card p-4 text-ink-faint">加载中…</div>`;
}
async function init() {
  bindEvents();
  // 恢复自动刷新开关（上次开启过则自动继续）
  try { if (localStorage.getItem(AUTO_REFRESH_KEY) === "1") startAutoRefresh(); } catch (e) {}
  setInitialLoading();
  markQuoteUpdated();
  // 这几个请求彼此没有依赖，必须并发：串行 await 会把各自的耗时累加，
  // 首屏时间等于「最慢那个 × 个数」。用 allSettled 是为了单个失败不拖垮整页。
  await Promise.allSettled([
    loadIndices(),
    loadFavorites(false),
    loadTrades(),
    loadMonitor(false, "auto"),
    loadFactors(),
  ]);
  // 下面两个要排在上面的 favorites 之后：
  // - loadSettings 拉 /api/stock/account，它和 favorites 取的是同一份持仓行情。
  //   行情缓存被 favorites 拉热之后再请求，实测 4.78s → 0.07s；并发反而两边都冷启动。
  // - loadBacktestOptions 直接读 favoritesCache。
  await Promise.allSettled([loadSettings(), loadBacktestOptions()]);
  // 暴露给控制台调试
  window.__app = { loadFavorites, loadMonitor, loadIndices, runBacktest, loadBacktestOptions, loadTrades };
}
init();
