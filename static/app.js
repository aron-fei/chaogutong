/* A股市场监控台 前端逻辑 */
"use strict";

const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[<>&"]/g,
  c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

let curTab = "speed_up";
let lastIdxPrices = {};   // 用于闪烁动画
let firstLoad = true;

/* ---------- 格式化 ---------- */
function pctCls(v) { return v > 0 ? "c-up" : v < 0 ? "c-down" : "c-dim"; }
function fmtPct(v) {
  if (v == null || isNaN(v)) return "-";
  return (v > 0 ? "+" : "") + Number(v).toFixed(2);
}
function fmtNum(v, d = 2) {
  if (v == null || isNaN(v)) return "-";
  return Number(v).toFixed(d);
}
function speedChip(v) {
  if (v == null || isNaN(v)) return '<span class="speed-chip mute">--</span>';
  const cls = v > 0.02 ? "fast-up" : v < -0.02 ? "fast-down" : "mute";
  const sign = v > 0 ? "+" : "";
  return `<span class="speed-chip ${cls}">${sign}${v.toFixed(2)}</span>`;
}

/* ---------- 渲染：顶栏 ---------- */
function renderStatus(d) {
  const st = $("market-status");
  st.textContent = d.status_text;
  st.className = "pill " + d.market_status;
  $("fresh-info").textContent =
    `源更新 指数:${d.last_update.indices || "--"} 板块:${d.last_update.sectors || "--"} ` +
    `池:${d.last_update.pools || "--"} 全A:${d.last_update.full || "--"}`;
  if (d.errors && d.errors.length && firstLoad) toast("部分数据源异常: " + d.errors[d.errors.length - 1]);
}

/* ---------- 渲染：指数卡片 ---------- */
function renderIndices(list) {
  const row = $("index-row");
  if (!list || !list.length) return;
  row.innerHTML = "";
  for (const idx of list) {
    const cls = idx.pct > 0 ? "up" : idx.pct < 0 ? "down" : "";
    const c = idx.pct > 0 ? "c-up" : idx.pct < 0 ? "c-down" : "c-dim";
    const flash = lastIdxPrices[idx.code] != null && lastIdxPrices[idx.code] !== idx.price
      ? (idx.price > lastIdxPrices[idx.code] ? "flash-up" : "flash-down") : "";
    row.innerHTML += `
      <div class="idx-card ${cls} ${flash}">
        <div class="idx-name">${esc(idx.name)}</div>
        <div class="idx-price ${c}">${fmtNum(idx.price)}</div>
        <div class="idx-chg ${c}">${fmtPct(idx.change)} (${fmtPct(idx.pct)}%)</div>
        <div class="idx-sub">额 <b>${fmtNum(idx.amount_yi, 0)}</b>亿 · 高 <b>${fmtNum(idx.high)}</b> 低 <b>${fmtNum(idx.low)}</b></div>
      </div>`;
    lastIdxPrices[idx.code] = idx.price;
  }
}

/* ---------- 渲染：板块榜（涨幅榜，可点击表头排序） ---------- */
let sectorSort = { key: "pct", dir: "desc" };  // 默认按涨跌%降序
let _lastSectors = [];                          // 缓存最近一次板块数据，供点击表头后重排

function _sectorCmp(a, b) {
  // 数字列 null 排到末尾；名称列 null 也排末尾
  const k = sectorSort.key;
  if (k === "name") {
    return sectorSort.dir === "asc"
      ? String(a.name||"").localeCompare(String(b.name||""), "zh-CN")
      : String(b.name||"").localeCompare(String(a.name||""), "zh-CN");
  }
  const av = a[k], bv = b[k];
  const aN = (av == null || isNaN(av)), bN = (bv == null || isNaN(bv));
  if (aN && bN) return 0;
  if (aN) return 1;       // null 永远排末尾（无论升序降序）
  if (bN) return -1;
  return sectorSort.dir === "asc" ? av - bv : bv - av;
}

function _updateSectorHead() {
  document.querySelectorAll("#tbl-sector-pct thead th.sortable").forEach(th => {
    const arrow = th.querySelector(".sort-arrow");
    if (th.dataset.sort === sectorSort.key) {
      th.classList.add("sort-active");
      if (arrow) arrow.textContent = sectorSort.dir === "asc" ? "▲" : "▼";
    } else {
      th.classList.remove("sort-active");
      if (arrow) arrow.textContent = "";
    }
  });
}

function renderSectors(sectors, N) {
  if (!sectors || !sectors.length) return;
  _lastSectors = sectors;
  const sorted = [...sectors].sort(_sectorCmp);

  // 只有默认排序（涨跌%降序）才显示「跌幅居前」分隔，避免排序切换时夹在中间错位
  const isDefault = sectorSort.key === "pct" && sectorSort.dir === "desc";
  const top = sorted.slice(0, N);
  const bottom = isDefault ? sorted.slice(-4).reverse() : [];

  const tb = $("tbl-sector-pct").querySelector("tbody");
  const row = (s) => `
    <tr>
      <td class="name-cell">${esc(s.name)}</td>
      <td class="${pctCls(s.pct)}">${fmtPct(s.pct)}</td>
      <td>${speedChip(s.speed_1m)}</td>
      <td class="${pctCls(s.net_inflow)}">${fmtPct(s.net_inflow)}</td>
      <td class="c-dim">${esc(s.leader)} <span class="${pctCls(s.leader_pct)}">${fmtPct(s.leader_pct)}</span></td>
    </tr>`;
  let html = "";
  top.forEach(s => html += row(s));
  if (bottom.length) {
    html += `<tr><td colspan="5" class="c-dim" style="text-align:center;padding:2px">······ 跌幅居前 ······</td></tr>`;
    bottom.forEach(s => html += row(s));
  }
  tb.innerHTML = html;
  _updateSectorHead();
}

/* ---------- 渲染：涨停/连板 ---------- */
function renderPools(d) {
  $("stat-zt").textContent = d.zt_count;
  $("stat-dt").textContent = d.dt_count;
  $("stat-zb").textContent = d.zb_count;
  const maxLb = d.zt_pool.length ? Math.max(...d.zt_pool.map(s => s.lianban || 1)) : 0;
  $("stat-lb").textContent = maxLb ? maxLb + "板" : "-";

  // 连板天梯
  const ladder = d.ladder || {};
  const keys = Object.keys(ladder).sort((a, b) => b - a);
  let lh = "";
  if (!keys.length) lh = '<div class="c-dim" style="padding:10px">暂无连板股</div>';
  keys.forEach(k => {
    lh += `<div class="ladder-item">
      <span class="lb-badge ${k >= 5 ? "high" : ""}">${k}板</span>
      <span class="lb-names">${ladder[k].map(esc).join(" · ")}</span>
    </div>`;
  });
  $("ladder").innerHTML = lh;

  // 涨停池
  $("zt-hint").textContent = `共${d.zt_count}家 · 按连板排序`;
  const tb = $("tbl-zt").querySelector("tbody");
  let th = "";
  (d.zt_pool || []).slice(0, 30).forEach(s => {
    th += `<tr>
      <td class="name-cell">${esc(s.name)}</td>
      <td><span class="speed-chip ${s.lianban >= 3 ? "fast-up" : "mute"}">${s.lianban}板</span></td>
      <td class="c-dim">${esc(s.industry)}</td>
      <td class="c-dim">${esc(String(s.first_time).slice(0, 5))}</td>
      <td class="${s.break_count > 0 ? "c-warn" : "c-dim"}">${s.break_count || 0}</td>
      <td class="${pctCls(s.amount_yi)}">${fmtNum(s.amount_yi, 1)}</td>
    </tr>`;
  });
  tb.innerHTML = th || '<tr><td colspan="6" class="c-dim" style="text-align:center;padding:10px">数据加载中…</td></tr>';

  // 炸板池
  $("zb-hint").textContent = `共${d.zb_count}家`;
  const tbz = $("tbl-zb").querySelector("tbody");
  let zh = "";
  (d.zb_pool || []).slice(0, 12).forEach(s => {
    zh += `<tr>
      <td class="name-cell">${esc(s.name)}</td>
      <td class="${pctCls(s.pct)}">${fmtPct(s.pct)}</td>
      <td class="c-warn">${s.break_count || 0}</td>
      <td class="c-dim">${esc(s.industry)}</td>
    </tr>`;
  });
  tbz.innerHTML = zh || '<tr><td colspan="4" class="c-dim" style="text-align:center;padding:10px">无炸板</td></tr>';

  // 跌停池
  const tbd = $("tbl-dt").querySelector("tbody");
  let dh = "";
  (d.dt_pool || []).forEach(s => {
    dh += `<tr>
      <td class="name-cell">${esc(s.name)}</td>
      <td class="${pctCls(s.pct)}">${fmtPct(s.pct)}</td>
      <td>${fmtNum(s.amount_yi, 1)}</td>
      <td class="c-dim">${esc(s.industry)}</td>
    </tr>`;
  });
  tbd.innerHTML = dh || '<tr><td colspan="4" class="c-dim" style="text-align:center;padding:10px">无跌停</td></tr>';
}

/* ---------- 渲染：异动筛选 ---------- */
const SCREEN_HEADS = {
  speed_up: `<th>股票</th><th>现价</th><th>涨速</th><th>涨跌%</th><th>额<span class="unit">亿</span></th>`,
  top_gainers: `<th>股票</th><th>现价</th><th>涨跌%</th><th>振幅%</th><th>额<span class="unit">亿</span></th>`,
  top_amount: `<th>股票</th><th>现价</th><th>涨跌%</th><th>额<span class="unit">亿</span></th><th>振幅%</th>`,
};
function renderScreen(screen) {
  if (!screen) return;
  $("screen-head").innerHTML = SCREEN_HEADS[curTab];
  const list = screen[curTab] || [];
  const tb = $("tbl-screen").querySelector("tbody");
  let html = "";
  list.forEach(s => {
    if (curTab === "speed_up") {
      html += `<tr>
        <td class="name-cell">${esc(s.name)}</td>
        <td>${fmtNum(s.price)}</td>
        <td>${speedChip(s.speed_1m)}</td>
        <td class="${pctCls(s.pct)}">${fmtPct(s.pct)}</td>
        <td>${fmtNum(s.amount_yi, 1)}</td>
      </tr>`;
    } else if (curTab === "top_gainers") {
      html += `<tr>
        <td class="name-cell">${esc(s.name)}</td>
        <td>${fmtNum(s.price)}</td>
        <td class="${pctCls(s.pct)}">${fmtPct(s.pct)}</td>
        <td>${fmtNum(s.amplitude)}</td>
        <td>${fmtNum(s.amount_yi, 1)}</td>
      </tr>`;
    } else {
      html += `<tr>
        <td class="name-cell">${esc(s.name)}</td>
        <td>${fmtNum(s.price)}</td>
        <td class="${pctCls(s.pct)}">${fmtPct(s.pct)}</td>
        <td>${fmtNum(s.amount_yi, 1)}</td>
        <td>${fmtNum(s.amplitude)}</td>
      </tr>`;
    }
  });
  tb.innerHTML = html ||
    '<tr><td colspan="5" class="c-dim" style="text-align:center;padding:14px">数据积累中…（全A快照约75s刷新）</td></tr>';
  if (curTab === "speed_up")
    $("screen-hint").textContent = "筛选：成交额≥3亿 · 涨速为相邻两帧涨跌幅差分";
}

/* ---------- 渲染：市场宽度 + 情绪 ---------- */
function renderBreadth(d) {
  const b = d.breadth || {};
  if (!b.total) return;
  $("b-up").textContent = `涨 ${b.up}`;
  $("b-down").textContent = `跌 ${b.down}`;
  $("b-flat").textContent = `平 ${b.flat}`;
  const upRatio = (b.up / Math.max(b.up + b.down, 1)) * 100;
  $("b-fill").style.width = upRatio.toFixed(1) + "%";

  // 简易情绪温度计
  const zt = d.zt_count || 0, dt = d.dt_count || 0, zb = d.zb_count || 0;
  const zbRate = zt + zb > 0 ? zb / (zt + zb) : 0;
  let score = 50;
  score += (upRatio - 50) * 0.6;          // 涨跌比
  score += Math.min(zt - dt, 30) * 0.5;   // 涨停优势
  score -= zbRate * 20;                   // 炸板率惩罚
  score = Math.max(0, Math.min(100, score));
  let mood, color;
  if (score >= 75) { mood = "🔥 过热"; color = "#cf222e"; }
  else if (score >= 60) { mood = "😀 偏暖"; color = "#a40e26"; }
  else if (score >= 40) { mood = "😐 中性"; color = "#bf8700"; }
  else if (score >= 25) { mood = "🥶 偏冷"; color = "#2da44e"; }
  else { mood = "❄️ 冰点"; color = "#1a7f37"; }
  $("b-sentiment").innerHTML =
    `情绪 ${Math.round(score)}分 <span style="color:${color}">●</span> ${mood} · 炸板率 ${(zbRate * 100).toFixed(0)}%`;
}

/* ---------- 龙头候选雷达 ---------- */
async function pollDragon() {
  try {
    const r = await fetch("/api/dragon");
    if (!r.ok) return;
    const d = await r.json();
    const tb = document.querySelector("#tbl-dragon tbody");
    const md = d.mood_detail;
    if (d.mood != null && md) {
      $("dragon-hint").textContent =
        `情绪系数 ${d.mood} · 涨停${md.zt} 炸板率${md.zha_rate}% 高度${md.max_lb}板`;
    }
    if (!d.rows || !d.rows.length) {
      tb.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#8c959f;padding:14px;">暂无候选（涨停池为空）</td></tr>';
      return;
    }
    tb.innerHTML = d.rows.map((s, i) => {
      let scoreColor = "#8c959f";
      if (s.score >= 78) scoreColor = "#cf222e";
      else if (s.score >= 72) scoreColor = "#d1746f";
      const tipCls = s.tip.startsWith("🔥") || s.tip.startsWith("⚡") || s.tip.startsWith("👑") ? "up" : "flat";
      const tipColor = tipCls === "up" ? "#a40e26" : "#656d76";
      return `<tr>
        <td style="color:#8c959f">${i + 1}</td>
        <td><span class="name-cell">${s.name}</span><span style="color:#8c959f;font-size:10px;margin-left:4px;">${s.code}</span></td>
        <td style="font-weight:600">${s.lianban > 1 ? s.lianban + "板" : "首板"}</td>
        <td style="font-weight:700;color:${scoreColor}">${s.score}</td>
        <td style="text-align:left;font-size:11px;">${(s.tags || []).slice(0, 3).map(t =>
          `<span style="display:inline-block;background:#eaeef2;border-radius:3px;padding:1px 5px;margin:1px;color:#656d76;">${t}</span>`).join("")}</td>
        <td style="text-align:left;font-size:11px;color:${tipColor}">${s.tip}</td>
      </tr>`;
    }).join("");
  } catch (e) { /* 静默，下轮重试 */ }
}

/* ---------- 轮询 ---------- */
async function poll() {
  try {
    const r = await fetch("/api/dashboard");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    renderStatus(d);
    renderIndices(d.indices);
    renderSectors(d.sectors, 12);
    renderPools(d);
    renderScreen(d.screen);
    renderBreadth(d);
    firstLoad = false;
  } catch (e) {
    $("market-status").textContent = "连接失败，重试中…";
    $("market-status").className = "pill closed";
  }
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3000);
}

/* ---------- 时钟 ---------- */
function tickClock() {
  const n = new Date();
  $("clock").textContent =
    `${n.getHours().toString().padStart(2, "0")}:${n.getMinutes().toString().padStart(2, "0")}:${n.getSeconds().toString().padStart(2, "0")}`;
}

/* ---------- 初始化 ---------- */
document.querySelectorAll("#screen-tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#screen-tabs button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    curTab = btn.dataset.tab;
    poll();
  });
});

// 板块涨幅榜表头点击排序
document.querySelectorAll("#tbl-sector-pct thead th.sortable").forEach(th => {
  th.style.cursor = "pointer";             // 即便 CSS 没生效也保证视觉可点
  th.title = "点击按此列排序";
  th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (sectorSort.key === k) {
      // 同列：切换升降序
      sectorSort.dir = sectorSort.dir === "asc" ? "desc" : "asc";
    } else {
      // 换列：名称列默认升序，数值列默认降序（大的先看）
      sectorSort.key = k;
      sectorSort.dir = (k === "name") ? "asc" : "desc";
    }
    if (_lastSectors.length) renderSectors(_lastSectors, 12);
  });
});
tickClock();
setInterval(tickClock, 1000);
poll();
setInterval(poll, POLL_MS);
pollDragon();
setInterval(pollDragon, 20000);
