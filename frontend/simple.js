/* 小白友好层：一句话结论、导航收纳、术语解释
 *
 * 设计原则：只做「翻译」和「收纳」，不新增任何判断口径。
 * - 结论文字全部来自后端 /api/stock/allocation 的 plain 字段，前端不自己判断该提醒什么；
 * - 术语解释只在这一处维护，别处只写 data-term="key"；
 * - 高级工具只是收起来，不删除，用户随时能展开；状态记在 localStorage。
 */
"use strict";

const ADVANCED_VIEWS = [
  "calculator", "backtest", "factors", "review", "daily",
  "doctor", "risk", "sell", "orchestration",
];
const NAV_MORE_KEY = "navMoreOpen";

/* 术语表：写给人话，不是写给同行。新增词条只在这里加，别在模板里写死解释。 */
const GLOSSARY = {
  pool: "把要用的钱分成四个性格不同的池子：吃股息的、跟事件的、赌产业的、捡便宜的。分开管，才不会在错的时候卖。",
  dividend: "靠公司分红赚钱的股票。涨得慢，但每年有现金，适合当底仓。",
  catalyst: "靠某个事件（业绩、订单、政策）短期上涨的股票。来得快，去得也快。",
  long_track: "靠一个长期产业趋势慢慢长大的公司。要给它时间，别用短线的耐心要求它。",
  value: "价格比它本身值钱的程度便宜。赚的是「涨回合理价格」的钱。",
  max_single: "一只股票最多能占你总资金的百分比。超了，就等于把命运押在一家公司身上。",
  cash_floor: "现金的最低比例。留够现金，下跌时才有子弹，不用被迫割肉。",
  drift: "实际占比和目标差了多远。差得越多，越该调整。",
  xirr: "把你什么时候投了多少钱算进去之后的年化收益。比简单收益率更能反映真实水平。",
};

function setupGlossaryStyle() {
  if (qs("#termStyle")) return;
  const style = document.createElement("style");
  style.id = "termStyle";
  style.textContent = "[data-term]{border-bottom:1px dashed currentColor;cursor:help;}";
  document.head.appendChild(style);
}

function setupGlossary() {
  setupGlossaryStyle();
  let bubble = null;

  function close() {
    if (bubble) { bubble.remove(); bubble = null; }
  }

  document.addEventListener("click", function (e) {
    const term = e.target.closest("[data-term]");
    if (!term) { close(); return; }
    const key = term.getAttribute("data-term");
    const text = GLOSSARY[key];
    if (!text) return;
    e.stopPropagation();
    close();
    bubble = document.createElement("div");
    bubble.className = "fixed z-50 max-w-[300px] rounded-xl border border-line bg-white shadow-card p-3 text-[12.5px] text-ink-muted leading-relaxed";
    bubble.textContent = text;
    bubble.dataset.term = key;
    document.body.appendChild(bubble);
    const rect = term.getBoundingClientRect();
    bubble.style.left = Math.min(window.innerWidth - 312, Math.max(12, rect.left)) + "px";
    bubble.style.top = (rect.bottom + 8) + "px";
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") close();
  });
}

function renderPlain(data) {
  const el = qs("#plainSummary");
  if (!el) return;
  const plain = (data && data.plain) || null;
  if (!plain || !plain.headline) { el.classList.add("hidden"); return; }

  const tones = {
    setup: ["border-brand-200 bg-brand-50", "text-brand-700"],
    empty: ["border-brand-200 bg-brand-50", "text-brand-700"],
    unclassified: ["border-amber-200 bg-amber-50", "text-amber-700"],
    risk: ["border-red-200 bg-red-50", "text-red-700"],
    drift: ["border-amber-200 bg-amber-50", "text-amber-700"],
    calm: ["border-emerald-200 bg-emerald-50", "text-emerald-700"],
  };
  const tone = tones[plain.level] || ["border-line bg-white", "text-ink-muted"];

  el.className = "rounded-2xl border " + tone[0] + " p-5";
  el.innerHTML = [
    '<div class="text-[11px] ' + tone[1] + ' font-semibold">现在的情况</div>',
    '<div class="text-[19px] font-bold mt-1 leading-snug">' + escapeHTML(plain.headline) + "</div>",
    plain.detail ? '<div class="text-[13px] text-ink-muted mt-1.5">' + escapeHTML(plain.detail) + "</div>" : "",
    plain.action
      ? '<button type="button" data-goto="' + escapeHTML(plain.action.view) + '" class="mt-3 px-3.5 py-2 rounded-lg bg-brand-500 text-white text-[13px] font-semibold hover:bg-brand-600">' + escapeHTML(plain.action.label) + " →</button>"
      : "",
  ].join("");
  el.classList.remove("hidden");

  const btn = el.querySelector("[data-goto]");
  if (btn) {
    btn.addEventListener("click", function (e) {
      const view = e.currentTarget.getAttribute("data-goto");
      if (typeof window.switchView === "function") window.switchView(view);
      else {
        const navBtn = qs('.nav-item[data-view="' + view + '"]');
        if (navBtn) navBtn.click();
      }
    });
  }
}

async function loadPlain() {
  if (typeof window.requestJSON !== "function") return;
  const data = await window.requestJSON("/api/stock/allocation");
  renderPlain(data || {});
}

function setupNavMore() {
  const nav = qs("#nav");
  if (!nav || qs("#navMore")) return;
  const advanced = ADVANCED_VIEWS
    .map(function (view) { return nav.querySelector('.nav-item[data-view="' + view + '"]'); })
    .filter(Boolean);
  if (!advanced.length) return;

  const box = document.createElement("div");
  box.id = "navMore";
  box.className = "hidden flex-col gap-1";
  advanced.forEach(function (btn) { box.appendChild(btn); });

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.id = "navMoreToggle";
  toggle.className = "flex items-center gap-2 px-3 py-2 rounded-xl text-[12.5px] text-ink-faint hover:bg-brand-50";
  toggle.innerHTML = '<span id="navMoreArrow" class="inline-block transition-transform">▸</span><span>更多工具（专业）</span>';

  nav.appendChild(toggle);
  nav.appendChild(box);

  function openBox(state) {
    box.classList.toggle("hidden", !state);
    box.classList.toggle("flex", state);
    const arrow = qs("#navMoreArrow");
    if (arrow) arrow.style.transform = state ? "rotate(90deg)" : "";
    try { localStorage.setItem(NAV_MORE_KEY, state ? "1" : "0"); } catch (err) {}
  }

  let saved = false;
  try { saved = localStorage.getItem(NAV_MORE_KEY) === "1"; } catch (err) {}
  openBox(saved);
  toggle.addEventListener("click", function () { openBox(box.classList.contains("hidden")); });
}

window.__simpleOnView = function (view) {
  if (view === "overview") loadPlain().catch(function () {});
  if (ADVANCED_VIEWS.indexOf(view) >= 0) {
    const box = qs("#navMore");
    if (box && box.classList.contains("hidden")) {
      box.classList.remove("hidden");
      box.classList.add("flex");
    }
  }
};

document.addEventListener("DOMContentLoaded", function () {
  setupNavMore();
  setupGlossary();
  loadPlain().catch(function () {});
});
