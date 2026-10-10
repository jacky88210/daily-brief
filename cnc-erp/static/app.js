"use strict";

// ---------------------------------------------------------------- 共用工具

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const app = $("#app");

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n = (v) => Number(v || 0).toLocaleString("zh-TW", { maximumFractionDigits: 3 });
const money = (v) => "$" + Math.round(Number(v || 0)).toLocaleString("zh-TW");
const today = () => new Date().toLocaleDateString("sv-SE");

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "發生錯誤 (" + res.status + ")");
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2200);
}

// 彈出視窗：onSubmit 丟出錯誤時顯示錯誤並保持開啟
function modal({ title, body, ok = "儲存", onOpen, onSubmit, hideOk = false }) {
  const dlg = $("#modal");
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = body;
  $("#modal-error").textContent = "";
  $("#modal-ok").textContent = ok;
  $("#modal-ok").hidden = hideOk;
  $("#modal-cancel").textContent = hideOk ? "關閉" : "取消";
  const form = $("#modal-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (!onSubmit) return dlg.close();
    $("#modal-ok").disabled = true;
    try {
      await onSubmit(form);
      dlg.close();
    } catch (err) {
      $("#modal-error").textContent = err.message;
    } finally {
      $("#modal-ok").disabled = false;
    }
  };
  $("#modal-cancel").onclick = () => dlg.close();
  if (!dlg.open) dlg.showModal();
  if (onOpen) onOpen(form);
}

const formData = (form) => Object.fromEntries(new FormData(form).entries());

function field(label, name, value = "", attrs = "", wide = false) {
  return `<label class="${wide ? "wide" : ""}">${label}<input name="${name}" value="${esc(value)}" ${attrs}></label>`;
}

function selectField(label, name, options, value = "", wide = false) {
  const opts = options.map(([v, t]) =>
    `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(t)}</option>`).join("");
  return `<label class="${wide ? "wide" : ""}">${label}<select name="${name}">${opts}</select></label>`;
}

function table(headers, rowsHtml, emptyText = "沒有資料") {
  if (!rowsHtml.length) return `<p class="empty">${emptyText}</p>`;
  const th = headers.map((h) => {
    const [label, cls] = Array.isArray(h) ? h : [h, ""];
    return `<th class="${cls}">${label}</th>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${rowsHtml.join("")}</tbody></table></div>`;
}

const STATUS = {
  open: ["未交貨", "warn"], partial: ["部分交貨", "warn"], done: ["已結案", "ok"], cancelled: ["已作廢", ""],
  pending: ["待生產", "warn"], in_progress: ["生產中", ""],
};
function statusTag(s, extra = "") {
  const [t, c] = STATUS[s] || [s, ""];
  return `<span class="tag ${c}">${t}${extra}</span>`;
}
function dueTag(o) {
  if (!o.due_date || ["done", "cancelled"].includes(o.status)) return esc(o.due_date || "");
  if (o.due_date < today()) return `<span class="tag danger">${esc(o.due_date)} 逾期</span>`;
  return esc(o.due_date);
}

const CATEGORIES = ["原料", "成品", "半成品", "刀具", "耗材", "其他"];

// ---------------------------------------------------------------- 路由

const views = {};
function go(view) {
  location.hash = view;
}
async function render() {
  const [view, arg] = decodeURIComponent(location.hash.slice(1) || "dashboard").split(":");
  const navView = view === "partner" ? "partners" : view;
  if (view === "partners" && arg) partnersState().type = arg;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === navView));
  app.innerHTML = '<p class="muted">載入中…</p>';
  try {
    await (views[view] || views.dashboard)(arg);
  } catch (err) {
    app.innerHTML = `<div class="card error">${esc(err.message)}</div>`;
  }
}
$$("#nav a").forEach((a) => (a.onclick = () => go(a.dataset.view)));
window.addEventListener("hashchange", render);
const refresh = () => render();

// ---------------------------------------------------------------- 總覽

function pct(a, b) {
  if (!b) return "";
  const p = ((a - b) / b) * 100;
  return `<span class="${p >= 0 ? "up" : "down"}">${p >= 0 ? "▲" : "▼"} ${Math.abs(p).toFixed(0)}%</span>`;
}

// 近 12 個月銷貨長條圖：今年用長條，去年同月用細橫線，滑鼠移上去看數字
function monthChart(months) {
  const max = Math.max(1, ...months.map((m) => Math.max(m.amount, m.last_year)));
  const cols = months.map((m, i) => {
    const h = (m.amount / max) * 100, ly = (m.last_year / max) * 100;
    const last = i === months.length - 1;
    return `<div class="mc-col" data-tip="${esc(m.month)}　今年 ${money(m.amount)}　去年同月 ${money(m.last_year)}">
      <div class="mc-plot">
        ${last && m.amount ? `<div class="mc-val" style="bottom:${h}%">${money(m.amount)}</div>` : ""}
        <div class="mc-bar" style="height:${h}%"></div>
        ${m.last_year ? `<div class="mc-ly" style="bottom:${ly}%"></div>` : ""}
      </div>
      <div class="mc-x">${Number(m.month.slice(5))}月</div></div>`;
  }).join("");
  return `<div class="mc-legend"><span><i class="sw-bar"></i>今年</span><span><i class="sw-ly"></i>去年同月</span>
      <span class="muted">最高 ${money(max)}</span></div>
    <div class="mc">${cols}</div>`;
}

function wireTips(root) {
  let tip = $("#tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "tip";
    document.body.appendChild(tip);
  }
  $$("[data-tip]", root).forEach((el) => {
    el.onmouseenter = () => { tip.textContent = el.dataset.tip; tip.style.opacity = 1; };
    el.onmousemove = (e) => {
      tip.style.left = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8) + "px";
      tip.style.top = e.clientY - 36 + "px";
    };
    el.onmouseleave = () => (tip.style.opacity = 0);
  });
}

views.dashboard = async () => {
  const d = await api("GET", "/api/dashboard");
  const orderRow = (o) => `<tr class="clickable" data-order="${o.id}">
      <td>${esc(o.no)}</td><td>${esc(o.partner_name)}</td><td>${dueTag(o)}</td><td>${statusTag(o.status)}</td></tr>`;
  const thisMonth = d.months[d.months.length - 1];
  const topMax = Math.max(1, ...d.top_customers.map((c) => c.amount));
  const daysAgo = (iso) => Math.round((new Date(d.today) - new Date(iso)) / 86400000);
  app.innerHTML = `
    <h1>今日總覽 <span class="muted" style="font-size:16px;font-weight:400">${d.today}</span></h1>
    <div class="kpis">
      <div class="kpi"><div class="label">本月銷貨</div><div class="value">${money(thisMonth.amount)}</div>
        <div class="sub">去年同月 ${money(thisMonth.last_year)} ${pct(thisMonth.amount, thisMonth.last_year)}</div></div>
      <div class="kpi"><div class="label">今年累計銷貨</div><div class="value">${money(d.ytd)}</div>
        <div class="sub">去年同期 ${money(d.last_ytd)} ${pct(d.ytd, d.last_ytd)}</div></div>
      <div class="kpi"><div class="label">應收帳款</div><div class="value">${money(d.receivable)}</div>
        <div class="sub">已出貨未收款</div></div>
      <div class="kpi"><div class="label">應付帳款</div><div class="value">${money(d.payable)}</div>
        <div class="sub">已進貨未付款</div></div>
      <div class="kpi ${d.overdue_sales.length ? "alert" : ""}"><div class="label">逾期未交訂單</div><div class="value">${d.overdue_sales.length}</div>
        <div class="sub">7 天內要交 ${d.due_soon_sales.length} 張</div></div>
      <div class="kpi ${d.low_stock.length ? "alert" : ""}"><div class="label">低於安全庫存</div><div class="value">${d.low_stock.length}</div>
        <div class="sub"><a class="plink" id="see-low">看清單</a></div></div>
    </div>

    <h2 class="section">今天要處理</h2>
    <div class="grid">
      <div class="card"><h3>⚠️ 逾期未交貨</h3>
        ${table(["單號", "客戶", "交期", "狀態"], d.overdue_sales.map(orderRow), "沒有逾期訂單 👍")}</div>
      <div class="card"><h3>📅 7 天內要交貨</h3>
        ${table(["單號", "客戶", "交期", "狀態"], d.due_soon_sales.map(orderRow), "近期沒有要交的訂單")}</div>
      <div class="card"><h3>🚚 等待供應商交貨（材料 / 外包）</h3>
        ${table(["單號", "供應商", "預計到貨", "狀態"], d.open_purchase.map(orderRow), "沒有未到貨的採購單")}</div>
      <div class="card"><h3>📦 需要補貨</h3>
        ${table(["料號", "品名", ["庫存", "num"], ["安全量", "num"]], d.low_stock.slice(0, 8).map((i) => `
          <tr class="clickable" data-item="${i.id}"><td>${esc(i.code)}</td><td>${esc(i.name)}</td>
          <td class="num">${n(i.stock)} ${esc(i.unit)}</td><td class="num">${n(i.safety_stock)}</td></tr>`), "庫存都充足")}
        ${d.low_stock.length > 8 ? `<p class="muted">還有 ${d.low_stock.length - 8} 項…</p>` : ""}</div>
    </div>

    <h2 class="section">生產現場</h2>
    <div class="grid">
      <div class="card"><h3>⚙️ 機台負荷（未完工工單）</h3>
        ${table(["機台", ["工單", "num"], ["數量", "num"], ["預估工時", "num"], "最近交期"], d.machines.map((m) => `<tr>
          <td>${esc(m.machine)}</td><td class="num">${n(m.orders)}</td><td class="num">${n(m.qty)}</td>
          <td class="num">${m.hours ? n(Math.round(m.hours * 10) / 10) + " 小時" : "—"}${m.no_cycle ? `<span class="muted">（${m.no_cycle} 張未設工時）</span>` : ""}</td>
          <td>${esc(m.next_due || "")}</td></tr>`), "目前沒有未完工的工單")}
        <p class="muted" style="margin-bottom:0">在品項裡填「單件工時」就能算出每台機器還要做多久。</p></div>
      <div class="card"><h3>🏭 進行中工單</h3>
        ${table(["工單", "品名", ["數量", "num"], "機台", "交期", "狀態"], d.work_orders.map((w) => `
          <tr class="clickable" data-wo="${w.id}"><td>${esc(w.no)}</td><td>${esc(w.name)}</td>
          <td class="num">${n(w.qty)}</td><td>${esc(w.machine)}</td><td>${dueTag(w)}</td><td>${statusTag(w.status)}</td></tr>`),
          "目前沒有工單")}</div>
    </div>

    <h2 class="section">經營狀況 <span class="muted" style="font-size:14px;font-weight:400">（含凌越轉入的歷史紀錄）</span></h2>
    <div class="card"><h3>📈 近 12 個月銷貨金額</h3>${monthChart(d.months)}</div>
    <div class="grid">
      <div class="card"><h3>🏆 近一年前十大客戶</h3>
        ${table(["客戶", ["金額", "num"], "占比"], d.top_customers.map((c) => `
          <tr class="${c.partner_id ? "clickable" : ""}" ${c.partner_id ? `data-pid="${c.partner_id}"` : ""}>
          <td>${esc(c.name)}</td><td class="num">${money(c.amount)}</td>
          <td style="min-width:120px"><div class="share"><div style="width:${(c.amount / topMax) * 100}%"></div></div>
            <span class="muted">${d.total_12m ? Math.round((c.amount / d.total_12m) * 100) : 0}%</span></td></tr>`),
          "近一年沒有銷貨紀錄")}
        ${d.top_customers[0] && d.total_12m && d.top_customers[0].amount / d.total_12m > 0.4
          ? '<p class="warn-text">⚠️ 最大客戶占超過四成，訂單過度集中，要留意風險。</p>' : ""}</div>
      <div class="card"><h3>📞 久未下單的老客戶</h3>
        <p class="muted" style="margin-top:0">以前常下單（3 張以上），但超過 4 個月沒有訂單，可以打電話問候。</p>
        ${table(["客戶", "電話", "最後下單", ["近三年金額", "num"]], d.dormant.map((c) => `
          <tr class="${c.partner_id ? "clickable" : ""}" ${c.partner_id ? `data-pid="${c.partner_id}"` : ""}>
          <td>${esc(c.name)}</td><td>${esc(c.phone || "")}</td>
          <td>${esc(c.last_date)} <span class="muted">（${daysAgo(c.last_date)} 天）</span></td>
          <td class="num">${money(c.amount_3y)}</td></tr>`), "老客戶都有持續下單 👍")}</div>
      <div class="card"><h3>🔁 常回單的零件（近兩年）</h3>
        <p class="muted" style="margin-top:0">重複下單次數多的零件，可以考慮先備料或預做。</p>
        ${table(["料號", "品名", ["次數", "num"], ["總數量", "num"], "最近"], d.repeat_parts.map((r) => `
          <tr class="clickable" data-code="${esc(r.item_code)}"><td>${esc(r.item_code)}</td><td>${esc(r.item_name)}</td>
          <td class="num">${n(r.times)}</td><td class="num">${n(r.qty)}</td><td>${esc(r.last_date)}</td></tr>`), "還沒有足夠的紀錄")}</div>
    </div>`;
  $$("[data-order]").forEach((tr) => (tr.onclick = () => showOrder(tr.dataset.order)));
  $$("[data-wo]").forEach((tr) => (tr.onclick = () => showWorkOrder(tr.dataset.wo)));
  $$("[data-item]").forEach((tr) => (tr.onclick = () => showItem(tr.dataset.item)));
  $$("[data-pid]").forEach((tr) => (tr.onclick = () => go("partner:" + tr.dataset.pid)));
  $$("[data-code]").forEach((tr) => (tr.onclick = () => {
    views.history.state = { search: tr.dataset.code, kind: "sales", date_from: "", date_to: "" };
    go("history");
  }));
  $("#see-low").onclick = () => { itemsState().low = true; itemsState().page = 1; go("items"); };
  wireTips(app);
};

// ---------------------------------------------------------------- 品項

const MATERIALS = ["AL6061", "AL7075", "AL5052", "SUS304", "SUS316", "SUS303", "SUS420", "S45C", "SCM440",
  "SKD11", "SKD61", "SS400", "黃銅 C3604", "紅銅", "鈦合金", "POM", "PEEK", "MC 尼龍", "壓克力"];
const FINISHES = ["無", "陽極處理", "硬陽極", "黑色陽極", "鍍鎳", "化學鍍鎳", "鍍鉻", "鍍鋅", "黑染", "噴砂",
  "電解研磨", "淬火", "高週波", "滲碳", "真空熱處理", "調質", "氮化"];

function datalist(id, values) {
  return `<datalist id="${id}">${values.map((v) => `<option value="${esc(v)}">`).join("")}</datalist>`;
}

const itemsState = () => views.items.state || (views.items.state = {
  search: "", category: "", customer_id: "", low: false, sort: "code", dir: "asc", page: 1, per: 50 });

let customerCache = null;
async function customers(force) {
  if (!customerCache || force) customerCache = await api("GET", "/api/partners?type=customer");
  return customerCache;
}
const pname = (p) => p.short_name || p.name;

views.items = async () => {
  const st = itemsState();
  const q = { ...st, low: st.low ? "1" : "" };
  const [res, cus] = await Promise.all([api("GET", "/api/items/page?" + new URLSearchParams(q)), customers()]);
  const pages = Math.max(1, Math.ceil(res.total / res.per));
  const sortHead = (key, label, cls = "") => {
    const on = st.sort === key;
    return [`<a class="sort ${on ? "on" : ""}" data-sort="${key}">${label}${on ? (st.dir === "asc" ? " ▲" : " ▼") : ""}</a>`, cls];
  };
  const pager = `<div class="pager">
      <button type="button" class="small ghost" data-page="${st.page - 1}" ${st.page <= 1 ? "disabled" : ""}>‹ 上一頁</button>
      <span>第 ${st.page} / ${pages} 頁</span>
      <button type="button" class="small ghost" data-page="${st.page + 1}" ${st.page >= pages ? "disabled" : ""}>下一頁 ›</button>
      <select class="per">${[50, 100, 200].map((x) => `<option value="${x}" ${x === st.per ? "selected" : ""}>每頁 ${x} 筆</option>`).join("")}</select>
    </div>`;
  app.innerHTML = `
    <h1>庫存品項 <span class="spacer"></span><button id="add">＋ 新增品項</button></h1>
    <div class="toolbar">
      <input id="search" placeholder="搜尋料號、品名、規格、圖號、材質" value="${esc(st.search)}" style="flex:1;min-width:220px">
      <select id="cat"><option value="">全部分類</option>${CATEGORIES.map((c) =>
        `<option ${c === st.category ? "selected" : ""}>${c}</option>`).join("")}</select>
      <select id="cus"><option value="">全部客戶</option>${cus.map((c) =>
        `<option value="${c.id}" ${String(c.id) === String(st.customer_id) ? "selected" : ""}>${esc(pname(c))}</option>`).join("")}</select>
      <label><input type="checkbox" id="low" ${st.low ? "checked" : ""}> 只看低於安全庫存</label>
    </div>
    <p class="muted">共 ${n(res.total)} 項，庫存金額約 ${money(res.value)}。點一下任一列可看詳細資料、盤點、修改。</p>
    <div class="card">
      ${pager}
      ${table([sortHead("code", "料號"), sortHead("name", "品名"), sortHead("drawing", "圖號 / 版次"), "材質", "規格", "客戶",
        sortHead("stock", "庫存", "num"), ["安全量", "num"], sortHead("last", "最近交易")],
        res.rows.map((i) => `<tr class="clickable ${i.safety_stock && i.stock < i.safety_stock ? "low" : ""}" data-item="${i.id}">
          <td>${esc(i.code)}</td><td class="ellipsis">${esc(i.name)}</td>
          <td>${esc(i.drawing_no)}${i.revision ? ` <span class="muted">${esc(i.revision)}</span>` : ""}</td>
          <td>${esc(i.material)}</td><td class="ellipsis">${esc(i.spec)}</td>
          <td class="ellipsis">${esc(i.customer_short || i.customer_name || "")}</td>
          <td class="num">${n(i.stock)} ${esc(i.unit)}</td><td class="num">${i.safety_stock ? n(i.safety_stock) : ""}</td>
          <td>${esc(i.last_date || "")}</td></tr>`),
        st.search || st.category || st.customer_id || st.low ? "沒有符合條件的品項" : "還沒有品項，請按「新增品項」")}
      ${res.total > res.per ? pager : ""}
    </div>`;
  let timer;
  $("#search").oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { st.search = e.target.value; st.page = 1; refresh(); }, 350);
  };
  if (st.search) { $("#search").focus(); $("#search").setSelectionRange(st.search.length, st.search.length); }
  $("#cat").onchange = (e) => { st.category = e.target.value; st.page = 1; refresh(); };
  $("#cus").onchange = (e) => { st.customer_id = e.target.value; st.page = 1; refresh(); };
  $("#low").onchange = (e) => { st.low = e.target.checked; st.page = 1; refresh(); };
  $$("[data-sort]").forEach((a) => (a.onclick = () => {
    st.dir = st.sort === a.dataset.sort && st.dir === "asc" ? "desc" : (a.dataset.sort === "last" || a.dataset.sort === "stock") && st.sort !== a.dataset.sort ? "desc" : "asc";
    st.sort = a.dataset.sort;
    st.page = 1;
    refresh();
  }));
  $$("[data-page]").forEach((b) => (b.onclick = () => { st.page = Number(b.dataset.page); refresh(); window.scrollTo(0, 0); }));
  $$(".per").forEach((s) => (s.onchange = () => { st.per = Number(s.value); st.page = 1; refresh(); }));
  $("#add").onclick = () => itemForm();
  $$("[data-item]").forEach((tr) => (tr.onclick = () => showItem(tr.dataset.item)));
};

async function showItem(id) {
  const d = await api("GET", "/api/items/" + id);
  const i = d.item;
  const low = i.safety_stock && i.stock < i.safety_stock;
  modal({
    title: `${i.code}　${i.name}`,
    hideOk: true,
    body: `
      <div class="kpis" style="margin-bottom:12px">
        <div class="kpi ${low ? "alert" : ""}"><div class="label">目前庫存</div><div class="value">${n(i.stock)} <small>${esc(i.unit)}</small></div>
          <div class="sub">${i.safety_stock ? `安全量 ${n(i.safety_stock)}` : "未設安全量"}</div></div>
        <div class="kpi"><div class="label">售價 / 成本</div><div class="value" style="font-size:20px">${n(i.price)} / ${n(i.cost)}</div>
          <div class="sub">${i.price && i.cost ? `毛利約 ${Math.round(((i.price - i.cost) / i.price) * 100)}%` : ""}</div></div>
        <div class="kpi"><div class="label">交易紀錄</div><div class="value">${n(d.history.count)}</div><div class="sub">筆</div></div>
      </div>
      <div class="detail-head">
        <div><span>分類</span>${esc(i.category)}</div>
        <div><span>客戶</span>${esc(i.customer_name || "—")}</div>
        <div><span>客戶圖號</span>${esc(i.drawing_no || "—")} ${esc(i.revision || "")}</div>
        <div><span>材質</span>${esc(i.material || "—")}</div>
        <div><span>表面 / 熱處理</span>${esc(i.finish || "—")}</div>
        <div><span>單件工時</span>${i.cycle_min ? n(i.cycle_min) + " 分鐘" : "—"}</div>
        <div><span>規格</span>${esc(i.spec || "—")}</div>
        <div><span>儲位</span>${esc(i.location || "—")}</div>
        ${i.note ? `<div style="grid-column:1/-1"><span>備註</span>${esc(i.note)}</div>` : ""}
      </div>
      <div class="actions" style="justify-content:flex-start">
        <button type="button" id="i-edit">修改資料</button>
        <button type="button" class="ghost" id="i-count">盤點</button>
        <button type="button" class="ghost" id="i-moves">庫存異動</button>
        ${["成品", "半成品"].includes(i.category) ? '<button type="button" class="ghost" id="i-wo">開工單</button>' : ""}
      </div>
      ${d.open_lines.length ? `<h3>未結訂單</h3>${table(["單號", "對象", ["數量", "num"], ["已交", "num"], "交期"],
        d.open_lines.map((l) => `<tr class="clickable" data-order="${l.order_id}"><td>${esc(l.no)}</td><td>${esc(l.partner_name)}</td>
          <td class="num">${n(l.qty)}</td><td class="num">${n(l.delivered_qty)}</td><td>${esc(l.due_date)}</td></tr>`))}` : ""}
      ${d.work_orders.length ? `<h3>生產中工單</h3>${table(["工單", ["數量", "num"], "機台", "交期", "狀態"],
        d.work_orders.map((w) => `<tr><td>${esc(w.no)}</td><td class="num">${n(w.qty)}</td><td>${esc(w.machine)}</td>
          <td>${esc(w.due_date)}</td><td>${statusTag(w.status)}</td></tr>`))}` : ""}
      <h3 style="margin-top:16px">跟誰往來、最近價格</h3>
      ${table(["對象", "", ["次數", "num"], ["數量", "num"], ["最近單價", "num"], "最近"], d.partners.map((p) => `<tr>
        <td>${p.partner_id ? `<a class="plink" data-pid="${p.partner_id}">${esc(p.partner_name)}</a>` : esc(p.partner_name)}</td>
        <td><span class="tag">${p.kind === "sales" ? "銷貨" : "進貨"}</span></td><td class="num">${n(p.times)}</td>
        <td class="num">${n(p.qty)}</td><td class="num">${p.last_price == null ? "" : n(p.last_price)}</td>
        <td>${esc(p.last_date)}</td></tr>`), "還沒有交易紀錄")}
      <h3 style="margin-top:16px">最近交易</h3>
      ${historyTable(d.history.rows)}`,
    onOpen: () => {
      const dlg = $("#modal");
      $("#i-edit").onclick = () => itemForm(i);
      $("#i-count").onclick = () => stockCount(i);
      $("#i-moves").onclick = () => { dlg.close(); views.moves.state = { item_id: i.id }; go("moves"); };
      if ($("#i-wo")) $("#i-wo").onclick = () => workOrderForm({ item_id: i.id });
      $$("#modal [data-order]").forEach((tr) => (tr.onclick = () => showOrder(tr.dataset.order)));
      $$("#modal .plink").forEach((a) => (a.onclick = () => { dlg.close(); go("partner:" + a.dataset.pid); }));
    },
  });
}

async function itemForm(item) {
  const i = item || { category: "成品", unit: "個" };
  const cus = await customers();
  modal({
    title: item ? `修改 ${item.code}` : "新增品項",
    body: `<div class="fields">
      ${field("料號 *", "code", i.code, "required")}
      ${field("品名 *", "name", i.name, "required")}
      ${selectField("分類", "category", CATEGORIES.map((c) => [c, c]), i.category)}
      ${selectField("所屬客戶", "customer_id", [["", "（無 / 通用）"], ...cus.map((c) => [c.id, pname(c)])], i.customer_id ?? "")}
      ${field("客戶圖號", "drawing_no", i.drawing_no)}
      ${field("版次", "revision", i.revision, 'placeholder="例：Rev.C"')}
      ${field("材質", "material", i.material, 'list="dl-material"')}
      ${field("表面 / 熱處理", "finish", i.finish, 'list="dl-finish"')}
      ${field("規格 / 尺寸", "spec", i.spec)}
      ${field("單位", "unit", i.unit)}
      ${field("單件工時（分鐘）", "cycle_min", i.cycle_min || "", 'type="number" step="any" min="0" placeholder="用來估機台負荷"')}
      ${field("安全庫存（低於會提醒）", "safety_stock", i.safety_stock ?? 0, 'type="number" step="any" min="0"')}
      ${field("成本單價", "cost", i.cost ?? 0, 'type="number" step="any" min="0"')}
      ${field("售價", "price", i.price ?? 0, 'type="number" step="any" min="0"')}
      ${field("儲位", "location", i.location)}
      ${item ? "" : field("期初庫存", "opening_stock", 0, 'type="number" step="any" min="0"')}
      ${field("備註", "note", i.note, "", true)}
    </div>${datalist("dl-material", MATERIALS)}${datalist("dl-finish", FINISHES)}
    ${item ? '<div class="actions" style="justify-content:flex-start"><button type="button" class="small danger" id="del">停用此品項</button></div>' : ""}`,
    onOpen: () => {
      const del = $("#del");
      if (del) del.onclick = async () => {
        if (!confirm(`確定停用「${i.name}」？歷史單據會保留。`)) return;
        await api("DELETE", "/api/items/" + i.id);
        $("#modal").close();
        toast("已停用");
        refresh();
      };
    },
    onSubmit: async (form) => {
      const data = formData(form);
      if (item) await api("PUT", "/api/items/" + item.id, data);
      else await api("POST", "/api/items", data);
      toast("已儲存");
      refresh();
    },
  });
}

function stockCount(item) {
  modal({
    title: `盤點：${item.code} ${item.name}`,
    body: `<p>系統庫存：<b>${n(item.stock)} ${esc(item.unit)}</b>。請輸入實際點到的數量，系統會自動記錄差異。</p>
      <div class="fields">
        ${field("實盤數量 *", "actual", item.stock, 'type="number" step="any" required')}
        ${field("原因 / 備註", "note", "")}
      </div>`,
    ok: "確認盤點",
    onSubmit: async (form) => {
      const r = await api("POST", "/api/stock/adjust", { item_id: item.id, ...formData(form) });
      toast(r.diff ? `已調整 ${r.diff > 0 ? "+" : ""}${n(r.diff)}` : "數量相符，無需調整");
      refresh();
    },
  });
}

// ---------------------------------------------------------------- 客戶 / 廠商

const PTYPE = { customer: "客戶", supplier: "供應商" };
const partnersState = () => views.partners.state || (views.partners.state = {
  type: "customer", search: "", grp: "", sort: "last_date", dir: "desc" });

views.partners = async (typeArg) => {
  const st = partnersState();
  if (typeArg && PTYPE[typeArg]) st.type = typeArg;
  const all = await api("GET", "/api/partners?stats=1");
  const counts = { customer: 0, supplier: 0 };
  all.forEach((p) => counts[p.type]++);
  const mine = all.filter((p) => p.type === st.type);
  const groups = [...new Set(mine.map((p) => p.grp).filter(Boolean))].sort();
  const words = st.search.toLowerCase().split(/\s+/).filter(Boolean);
  let list = mine.filter((p) => (!st.grp || (st.grp === "-" ? !p.grp : p.grp === st.grp))
    && words.every((w) => [p.name, p.short_name, p.contact, p.phone, p.tax_id, p.address, p.grp, p.note]
      .some((v) => String(v || "").toLowerCase().includes(w))));
  const key = st.sort;
  list.sort((a, b) => {
    let x = a[key] ?? "", y = b[key] ?? "";
    if (key === "name") { x = pname(a); y = pname(b); return st.dir === "asc" ? x.localeCompare(y, "zh-TW") : y.localeCompare(x, "zh-TW"); }
    return (x < y ? -1 : x > y ? 1 : 0) * (st.dir === "asc" ? 1 : -1);
  });
  const year = today().slice(0, 4);
  const sortHead = (k, label, cls = "") => {
    const on = st.sort === k;
    return [`<a class="sort ${on ? "on" : ""}" data-sort="${k}">${label}${on ? (st.dir === "asc" ? " ▲" : " ▼") : ""}</a>`, cls];
  };
  const old = (d) => d && d < new Date(Date.now() - 365 * 86400000).toLocaleDateString("sv-SE");
  app.innerHTML = `
    <h1>客戶 / 廠商 <span class="spacer"></span><button id="add">＋ 新增${PTYPE[st.type]}</button></h1>
    <div class="tabs">
      ${Object.entries(PTYPE).map(([t, label]) => `<a class="tab ${t === st.type ? "on" : ""}" data-tab="${t}">${label} <span class="count">${counts[t]}</span></a>`).join("")}
    </div>
    <div class="toolbar">
      <input id="p-search" placeholder="搜尋名稱、簡稱、電話、統編、聯絡人、地址" value="${esc(st.search)}" style="flex:1;min-width:220px">
    </div>
    <div class="chips">
      <a class="chip ${!st.grp ? "on" : ""}" data-grp="">全部 ${mine.length}</a>
      ${groups.map((g) => `<a class="chip ${st.grp === g ? "on" : ""}" data-grp="${esc(g)}">${esc(g)} ${mine.filter((p) => p.grp === g).length}</a>`).join("")}
      <a class="chip ${st.grp === "-" ? "on" : ""}" data-grp="-">未分類 ${mine.filter((p) => !p.grp).length}</a>
    </div>
    <div class="batch" id="batch" hidden>
      <b id="b-count"></b>
      <input id="b-grp" list="dl-grp" placeholder="分類名稱，例：A級客戶、汽車零件、外包熱處理" style="width:260px;min-width:0">
      <button type="button" class="small" id="b-set">設定分類</button>
      <button type="button" class="small ghost" id="b-move">改為${st.type === "customer" ? "供應商" : "客戶"}</button>
      <button type="button" class="small ghost" id="b-lookup">🔍 補齊公司登記資料</button>
      ${datalist("dl-grp", groups)}
    </div>
    <div class="card">${table([
      `<input type="checkbox" id="check-all" title="全選">`, sortHead("name", "名稱"), "分類", "電話", "聯絡人",
      sortHead("last_date", "最近往來"), sortHead("amount_year", year + " 年", "num"),
      sortHead("amount_last_year", (year - 1) + " 年", "num"), sortHead("amount_total", "累計", "num")],
      list.map((p) => `<tr class="clickable" data-partner="${p.id}">
        <td><input type="checkbox" class="pick" value="${p.id}"></td>
        <td><b>${esc(pname(p))}</b>${p.short_name && p.short_name !== p.name ? `<div class="muted small">${esc(p.name)}</div>` : ""}
          ${p.reg_status && !/核准設立|核准登記/.test(p.reg_status) ? `<span class="tag danger">${esc(p.reg_status)}</span>` : ""}</td>
        <td>${p.grp ? `<span class="tag">${esc(p.grp)}</span>` : ""}</td>
        <td>${esc(p.phone)}</td><td>${esc(p.contact)}</td>
        <td>${esc(p.last_date || "")}${old(p.last_date) ? ' <span class="tag warn">久未往來</span>' : ""}</td>
        <td class="num">${p.amount_year ? money(p.amount_year) : ""}</td>
        <td class="num">${p.amount_last_year ? money(p.amount_last_year) : ""}</td>
        <td class="num">${p.amount_total ? money(p.amount_total) : ""}</td></tr>`),
      st.search || st.grp ? "沒有符合條件的資料" : `還沒有${PTYPE[st.type]}`)}</div>`;

  $$("[data-tab]").forEach((a) => (a.onclick = () => { st.type = a.dataset.tab; st.grp = ""; go("partners:" + st.type); }));
  $$("[data-grp]").forEach((a) => (a.onclick = () => { st.grp = a.dataset.grp; refresh(); }));
  let timer;
  $("#p-search").oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { st.search = e.target.value; refresh(); }, 300);
  };
  if (st.search) { $("#p-search").focus(); $("#p-search").setSelectionRange(st.search.length, st.search.length); }
  $$("[data-sort]").forEach((a) => (a.onclick = () => {
    const k = a.dataset.sort;
    st.dir = st.sort === k ? (st.dir === "asc" ? "desc" : "asc") : (k === "name" ? "asc" : "desc");
    st.sort = k;
    refresh();
  }));
  $("#add").onclick = () => partnerForm({ type: st.type });
  $$("[data-partner]").forEach((tr) => (tr.onclick = (e) => {
    if (e.target.classList.contains("pick")) return;
    go("partner:" + tr.dataset.partner);
  }));

  const picked = () => $$(".pick:checked").map((c) => Number(c.value));
  const syncBatch = () => {
    const k = picked().length;
    $("#batch").hidden = !k;
    $("#b-count").textContent = `已選 ${k} 家：`;
  };
  $$(".pick").forEach((c) => (c.onchange = syncBatch));
  $("#check-all").onclick = (e) => { e.stopPropagation(); $$(".pick").forEach((c) => (c.checked = e.target.checked)); syncBatch(); };
  $("#b-set").onclick = async () => {
    await api("POST", "/api/partners/batch", { ids: picked(), set: { grp: $("#b-grp").value } });
    toast($("#b-grp").value ? `已設定分類「${$("#b-grp").value}」` : "已清除分類");
    refresh();
  };
  $("#b-move").onclick = async () => {
    const to = st.type === "customer" ? "supplier" : "customer";
    if (!confirm(`把勾選的 ${picked().length} 家改為${PTYPE[to]}？`)) return;
    await api("POST", "/api/partners/batch", { ids: picked(), set: { type: to } });
    customerCache = null;
    toast("已移動");
    refresh();
  };
  $("#b-lookup").onclick = () => batchLookup(mine.filter((p) => picked().includes(p.id)));
};

// 單一客戶 / 廠商：基本資料 + 所有往來紀錄（舊系統 + 本系統）
views.partner = async (id) => {
  const { partner: p, stats, history } = await api("GET", `/api/partners/${id}/summary`);
  const isCus = p.type === "customer";
  const info = (label, v) => `<div><span>${label}</span>${esc(v || "—")}</div>`;
  const regBad = p.reg_status && !/核准設立|核准登記/.test(p.reg_status);
  app.innerHTML = `
    <h1><a class="muted" style="cursor:pointer" id="back">${PTYPE[p.type]}</a> › ${esc(p.name)}
      ${p.grp ? `<span class="tag">${esc(p.grp)}</span>` : ""}
      <span class="spacer"></span><button class="ghost" id="lookup">🔍 查公司登記</button>
      <button class="ghost" id="edit">編輯資料</button>
      <button id="new-order">＋ ${isCus ? "開新訂單" : "開採購單"}</button></h1>
    ${regBad ? `<div class="card" style="background:var(--danger-bg)">⚠️ 經濟部登記狀態：<b>${esc(p.reg_status)}</b>，交易前請確認。</div>` : ""}
    <div class="card"><div class="detail-head">
      ${info("簡稱", p.short_name)}${info("聯絡人", p.contact)}${info("電話", p.phone)}${info("傳真", p.fax)}
      ${info("Email", p.email)}${info("統編", p.tax_id)}${info("負責人", p.owner)}${info("地址", p.address)}
      ${p.reg_checked ? `<div style="grid-column:1/-1"><span>登記資料</span>${esc(p.reg_status)}　資本額 ${esc(p.reg_capital || "—")}　設立 ${esc(p.reg_date || "—")}
        <span class="muted">（${esc(p.reg_checked)} 查詢）</span></div>` : ""}
      ${p.note ? `<div style="grid-column:1/-1"><span>備註</span>${esc(p.note)}</div>` : ""}</div></div>
    <div class="kpis">
      <div class="kpi"><div class="label">往來期間</div><div class="value" style="font-size:18px">
        ${stats.first_date ? `${esc(stats.first_date)}<br>～ ${esc(stats.last_date)}` : "尚無紀錄"}</div></div>
      <div class="kpi"><div class="label">累計${isCus ? "銷貨" : "進貨"}金額</div><div class="value">${money(stats.amount)}</div></div>
      <div class="kpi"><div class="label">單據數</div><div class="value">${n(stats.docs)}</div></div>
    </div>
    <div class="grid">
      <div class="card"><h3>${isCus ? "常訂" : "常買"}品項</h3>${table(
        ["料號", "品名", ["次數", "num"], ["累計數量", "num"], ["最近單價", "num"], "最近日期"],
        stats.top_items.map((t) => `<tr class="clickable" data-search="${esc(t.item_code || t.item_name)}">
          <td>${esc(t.item_code)}</td><td>${esc(t.item_name)}</td><td class="num">${n(t.times)}</td>
          <td class="num">${n(t.qty)}</td><td class="num">${t.last_price == null ? "" : n(t.last_price)}</td>
          <td>${esc(t.last_date)}</td></tr>`), "還沒有交易紀錄")}</div>
      <div class="card"><h3>每年金額</h3>${table(["年度", ["單據數", "num"], ["金額", "num"]],
        stats.by_year.map((y) => `<tr><td>${esc(y.year)}</td><td class="num">${n(y.docs)}</td>
          <td class="num">${money(y.amount)}</td></tr>`), "還沒有交易紀錄")}</div>
    </div>
    <div class="card"><h3>全部往來明細 <span class="muted" style="font-weight:400">（最近 ${history.rows.length} 筆，共 ${n(history.count)} 筆）</span></h3>
      ${historyTable(history.rows, false)}</div>`;
  $("#back").onclick = () => go("partners:" + p.type);
  $("#edit").onclick = () => partnerForm(p);
  $("#lookup").onclick = () => lookupPartner(p);
  $("#new-order").onclick = () => orderForm(isCus ? "sales" : "purchase", null, p.id);
  $$("[data-search]").forEach((tr) => (tr.onclick = () => {
    views.history.state = { search: tr.dataset.search, kind: "", date_from: "", date_to: "" };
    go("history");
  }));
  wireHistoryLinks();
};

// ---- 經濟部商工登記查詢

const REG_FIELDS = [["name", "name", "公司名稱"], ["owner", "owner", "負責人"], ["address", "address", "地址"]];

async function lookupPartner(p) {
  const taxId = p.tax_id || prompt(`「${p.name}」還沒有統一編號，請輸入 8 碼統編：`) || "";
  if (!taxId) return;
  let r;
  try {
    r = await api("GET", "/api/company-lookup?tax_id=" + encodeURIComponent(taxId));
  } catch (e) {
    return alert(e.message);
  }
  if (!r.found) {
    return modal({
      title: "查不到登記資料", hideOk: true,
      body: `<p>統編 ${esc(r.tax_id)} 沒有查到資料，或目前連不上經濟部的系統。</p>
        ${r.errors && r.errors.length ? `<p class="muted">${r.errors.map(esc).join("<br>")}</p>` : ""}
        <p>可以到經濟部「商工登記公示資料查詢」網站手動查詢：<br>
          <a href="${esc(r.manual_url)}" target="_blank" rel="noopener">${esc(r.manual_url)}</a></p>`,
    });
  }
  const bad = r.status && !/核准設立|核准登記/.test(r.status);
  modal({
    title: `經濟部登記資料（${esc(r.kind)}）`,
    ok: "套用勾選的資料",
    body: `${bad ? `<p class="error" style="font-size:16px">⚠️ 登記狀態：${esc(r.status)}</p>` : ""}
      <p class="muted">勾選要更新的欄位。登記狀態、資本額、設立日期會一起記錄下來。</p>
      ${table(["", "欄位", "系統裡現在", "經濟部登記"], REG_FIELDS.map(([k, rk, label]) => {
        const cur = p[k] || "", val = r[rk] || "";
        const same = cur.replace(/\s/g, "") === val.replace(/\s/g, "");
        return `<tr><td><input type="checkbox" name="use_${k}" ${!cur && val ? "checked" : ""} ${!val || same ? "disabled" : ""}></td>
          <td>${label}</td><td>${esc(cur || "—")}</td><td>${esc(val || "—")}${same ? ' <span class="muted">（相同）</span>' : ""}</td></tr>`;
      }))}
      <div class="detail-head" style="margin-top:12px">
        <div><span>統編</span>${esc(r.tax_id)}</div><div><span>狀態</span>${esc(r.status || "—")}</div>
        <div><span>資本額</span>${esc(r.capital || "—")}</div><div><span>設立日期</span>${esc(r.setup_date || "—")}</div>
      </div>`,
    onSubmit: async (form) => {
      const body = { tax_id: r.tax_id, reg_status: r.status, reg_capital: r.capital, reg_date: r.setup_date, reg_checked: today() };
      REG_FIELDS.forEach(([k, rk]) => { if (form.elements["use_" + k]?.checked) body[k] = r[rk]; });
      await api("PUT", "/api/partners/" + p.id, body);
      toast("已更新公司資料");
      refresh();
    },
  });
}

async function batchLookup(list) {
  const todo = list.filter((p) => /^\d{8}$/.test((p.tax_id || "").replace(/\D/g, "")));
  if (!todo.length) return alert("勾選的對象都沒有 8 碼統一編號，無法查詢。");
  if (!confirm(`查詢 ${todo.length} 家的經濟部登記資料？\n（只會補上空白的負責人、地址，並記錄登記狀態；${list.length - todo.length} 家沒有統編會略過）`)) return;
  let ok = 0, miss = 0, bad = [];
  for (const [k, p] of todo.entries()) {
    toast(`查詢中 ${k + 1} / ${todo.length}：${pname(p)}`);
    try {
      const r = await api("GET", "/api/company-lookup?tax_id=" + p.tax_id);
      if (!r.found) { miss++; continue; }
      const body = { reg_status: r.status, reg_capital: r.capital, reg_date: r.setup_date, reg_checked: today() };
      if (!p.owner && r.owner) body.owner = r.owner;
      if (!p.address && r.address) body.address = r.address;
      await api("PUT", "/api/partners/" + p.id, body);
      ok++;
      if (r.status && !/核准設立|核准登記/.test(r.status)) bad.push(`${pname(p)}：${r.status}`);
    } catch (e) {
      miss++;
    }
  }
  alert(`完成：更新 ${ok} 家，查不到 ${miss} 家。` + (bad.length ? `\n\n⚠️ 登記狀態異常：\n${bad.join("\n")}` : ""));
  refresh();
}

function historyTable(list, showPartner = true) {
  return table(
    ["日期", "單號", ...(showPartner ? ["客戶 / 廠商"] : []), "料號", "品名", "規格", ["數量", "num"], ["單價", "num"], ["金額", "num"], "來源"],
    list.map((h) => `<tr class="${h.order_id ? "clickable" : ""}" ${h.order_id ? `data-order="${h.order_id}"` : ""}>
      <td>${esc(h.doc_date)}</td><td>${esc(h.doc_no)}</td>
      ${showPartner ? `<td>${h.partner_id ? `<a class="plink" data-pid="${h.partner_id}">${esc(h.partner_name)}</a>` : esc(h.partner_name)}</td>` : ""}
      <td>${esc(h.item_code)}</td><td>${esc(h.item_name)}</td><td>${esc(h.spec)}</td>
      <td class="num">${h.qty == null ? "" : n(h.qty)}</td><td class="num">${h.unit_price == null ? "" : n(h.unit_price)}</td>
      <td class="num">${h.amount == null ? "" : money(h.amount)}</td>
      <td><span class="tag ${h.source === "本系統" ? "ok" : ""}">${esc(h.source)}${h.kind === "purchase" ? "・進貨" : ""}</span></td></tr>`),
    "沒有符合的紀錄");
}

function wireHistoryLinks() {
  $$("[data-order]").forEach((tr) => (tr.onclick = () => showOrder(tr.dataset.order)));
  $$(".plink").forEach((a) => (a.onclick = (e) => { e.stopPropagation(); go("partner:" + a.dataset.pid); }));
}

// 歷史查詢：某個零件以前賣給誰、多少錢；某個客戶以前買過什麼
views.history = async () => {
  const state = views.history.state || (views.history.state = { search: "", kind: "", date_from: "", date_to: "" });
  const h = await api("GET", "/api/history?" + new URLSearchParams(state));
  app.innerHTML = `
    <h1>交易歷史查詢</h1>
    <div class="toolbar">
      <input id="h-search" placeholder="客戶、料號、品名、規格、單號（空白分隔可多條件）" value="${esc(state.search)}" style="flex:1;min-width:240px">
      <select id="h-kind">${[["", "銷貨＋進貨"], ["sales", "只看銷貨"], ["purchase", "只看進貨"]].map(([v, t]) =>
        `<option value="${v}" ${v === state.kind ? "selected" : ""}>${t}</option>`).join("")}</select>
      <input id="h-from" type="date" value="${esc(state.date_from)}" title="起日">
      <input id="h-to" type="date" value="${esc(state.date_to)}" title="迄日">
    </div>
    <p class="muted">共 ${n(h.count)} 筆，金額合計 ${money(h.amount)}${h.count > h.rows.length ? `（顯示最近 ${h.rows.length} 筆）` : ""}。
      包含舊系統匯入的紀錄與本系統的訂單。</p>
    <div class="card">${historyTable(h.rows)}</div>`;
  let timer;
  $("#h-search").oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.search = e.target.value; refresh(); }, 350);
  };
  if (state.search) { $("#h-search").focus(); $("#h-search").setSelectionRange(state.search.length, state.search.length); }
  $("#h-kind").onchange = (e) => { state.kind = e.target.value; refresh(); };
  $("#h-from").onchange = (e) => { state.date_from = e.target.value; refresh(); };
  $("#h-to").onchange = (e) => { state.date_to = e.target.value; refresh(); };
  wireHistoryLinks();
};

async function partnerForm(p) {
  const all = await api("GET", "/api/partners");
  const groups = [...new Set(all.map((x) => x.grp).filter(Boolean))].sort();
  modal({
    title: p.id ? `編輯 ${p.name}` : "新增" + PTYPE[p.type],
    body: `<div class="fields">
      ${selectField("類型", "type", Object.entries(PTYPE), p.type)}
      ${field("名稱（全名）*", "name", p.name, "required")}
      ${field("簡稱", "short_name", p.short_name, 'placeholder="平常叫的名字"')}
      ${field("分類", "grp", p.grp, 'list="dl-pgrp" placeholder="例：A級客戶、汽車零件、外包熱處理"')}
      ${field("聯絡人", "contact", p.contact)}
      ${field("電話", "phone", p.phone)}
      ${field("傳真", "fax", p.fax)}
      ${field("Email", "email", p.email)}
      ${field("負責人", "owner", p.owner)}
      <label>統一編號<span style="display:flex;gap:6px"><input name="tax_id" value="${esc(p.tax_id || "")}">
        <button type="button" class="small ghost" id="pf-lookup" title="用統編查經濟部登記資料">查詢</button></span></label>
      ${field("地址", "address", p.address, "", true)}
      ${field("備註（付款條件、交貨習慣…）", "note", p.note, "", true)}
    </div>${datalist("dl-pgrp", groups)}`,
    onOpen: (form) => {
      $("#pf-lookup").onclick = async () => {
        const tax = form.elements.tax_id.value.trim();
        if (!tax) return ($("#modal-error").textContent = "請先輸入統一編號");
        $("#modal-error").textContent = "查詢中…";
        try {
          const r = await api("GET", "/api/company-lookup?tax_id=" + encodeURIComponent(tax));
          if (!r.found) {
            $("#modal-error").innerHTML = `查不到資料或無法連線。可到 <a href="${esc(r.manual_url)}" target="_blank" rel="noopener">經濟部商工登記查詢</a> 手動查。`;
            return;
          }
          const el = form.elements;
          if (!el.name.value) el.name.value = r.name;
          if (!el.owner.value) el.owner.value = r.owner;
          if (!el.address.value) el.address.value = r.address;
          form.dataset.reg = JSON.stringify({ reg_status: r.status, reg_capital: r.capital, reg_date: r.setup_date, reg_checked: today() });
          $("#modal-error").textContent = "";
          toast(`查到：${r.name}（${r.status || "狀態不明"}），已補上空白欄位`);
        } catch (e) {
          $("#modal-error").textContent = e.message;
        }
      };
    },
    onSubmit: async (form) => {
      const data = { ...formData(form), ...(form.dataset.reg ? JSON.parse(form.dataset.reg) : {}) };
      delete form.dataset.reg;
      if (p.id) await api("PUT", "/api/partners/" + p.id, data);
      else await api("POST", "/api/partners", data);
      customerCache = null;
      toast("已儲存");
      refresh();
    },
  });
}

// ---- 品項挑選：品項上千筆時下拉選單不好用，改成「打字搜尋」（料號 / 品名 / 圖號都可以）

const itemLabel = (i) => [i.code, i.name, i.drawing_no].filter(Boolean).join("｜");

function itemDatalistHtml(id, items) {
  return `<datalist id="${id}">${items.map((i) =>
    `<option value="${esc(itemLabel(i))}">${i.stock != null ? `庫存 ${n(i.stock)}` : ""}</option>`).join("")}</datalist>`;
}

function itemPicker(name, listId, items, selectedId, placeholder = "打料號、品名或圖號搜尋") {
  const sel = items.find((i) => String(i.id) === String(selectedId));
  return `<input class="ipick" list="${listId}" value="${esc(sel ? itemLabel(sel) : "")}" placeholder="${placeholder}" autocomplete="off">
    <input type="hidden" name="${name}" value="${sel ? sel.id : ""}">`;
}

// 依輸入內容找出品項：完整標籤 → 料號完全相同 → 圖號完全相同
function resolvePick(input, items) {
  const v = input.value.trim();
  const code = v.split("｜")[0];
  const it = items.find((i) => itemLabel(i) === v) || items.find((i) => i.code === code)
    || (v && items.find((i) => i.drawing_no && i.drawing_no === v));
  input.nextElementSibling.value = it ? it.id : "";
  input.classList.toggle("invalid", !!v && !it);
  if (it && input.value !== itemLabel(it)) input.value = itemLabel(it);
  return it || null;
}

// ---------------------------------------------------------------- 訂單（銷貨 / 採購共用）

const KIND = {
  sales: { title: "銷貨訂單", partner: "客戶", ptype: "customer", deliver: "出貨", pay: "收款", due: "交期" },
  purchase: { title: "採購進貨", partner: "供應商", ptype: "supplier", deliver: "進貨", pay: "付款", due: "預計到貨" },
};

function orderListView(kind) {
  return async () => {
    const k = KIND[kind];
    const state = orderListView[kind] || (orderListView[kind] = { status: "active" });
    let orders = await api("GET", "/api/orders?kind=" + kind);
    if (state.status === "active") orders = orders.filter((o) => ["open", "partial"].includes(o.status));
    else if (state.status) orders = orders.filter((o) => o.status === state.status);
    app.innerHTML = `
      <h1>${k.title} <span class="spacer"></span><button id="add">＋ 新增${kind === "sales" ? "訂單" : "採購單"}</button></h1>
      <div class="toolbar">
        <select id="status">
          ${[["active", "未結案"], ["", "全部"], ["done", "已結案"], ["cancelled", "已作廢"]].map(([v, t]) =>
            `<option value="${v}" ${v === state.status ? "selected" : ""}>${t}</option>`).join("")}
        </select>
      </div>
      <div class="card">${table(
        ["單號", k.partner, kind === "sales" ? "客戶單號" : "下單日", k.due, ["金額", "num"], ["未" + k.pay, "num"], "狀態"],
        orders.map((o) => `<tr class="clickable" data-order="${o.id}">
          <td>${esc(o.no)}</td><td>${esc(o.partner_name)}</td>
          <td>${esc(kind === "sales" ? o.customer_po : o.order_date)}</td><td>${dueTag(o)}</td>
          <td class="num">${money(o.total)}</td><td class="num">${money(o.total - o.paid)}</td>
          <td>${statusTag(o.status)}</td></tr>`))}</div>`;
    $("#status").onchange = (e) => { state.status = e.target.value; refresh(); };
    $("#add").onclick = () => orderForm(kind);
    $$("[data-order]").forEach((tr) => (tr.onclick = () => showOrder(tr.dataset.order)));
  };
}
views.sales = orderListView("sales");
views.purchase = orderListView("purchase");

async function orderForm(kind, order, presetPartner) {
  const k = KIND[kind];
  const [partners, items] = await Promise.all([
    api("GET", "/api/partners?type=" + k.ptype), api("GET", "/api/items?lite=1")]);
  if (!partners.length) return alert(`請先到「客戶/廠商」新增${k.partner}`);
  if (!items.length) return alert("請先到「庫存品項」新增品項");
  const o = order || { order_date: today(), lines: [{}], partner_id: presetPartner };
  const lineRow = (l = {}) => `<tr>
      <td style="min-width:260px">${itemPicker("item_id", "dl-order-items", items, l.item_id)}<div class="hint muted"></div></td>
      <td><input name="qty" type="number" step="any" min="0" value="${l.qty ?? ""}" placeholder="數量"></td>
      <td><input name="unit_price" type="number" step="any" min="0" value="${l.unit_price ?? ""}" placeholder="單價"></td>
      <td class="num sub"></td>
      <td><button type="button" class="small ghost rm">✕</button></td></tr>`;
  const lockedLines = order && order.lines.some((l) => l.delivered_qty);
  modal({
    title: (order ? "修改 " + order.no : "新增" + (kind === "sales" ? "銷貨訂單" : "採購單")),
    body: `<div class="fields">
        ${selectField(k.partner + " *", "partner_id", [["", "— 請選擇 —"], ...partners.map((p) => [p.id, pname(p)])], o.partner_id)}
        ${field("下單日期", "order_date", o.order_date, 'type="date"')}
        ${field(k.due, "due_date", o.due_date, 'type="date"')}
        ${kind === "sales" ? field("客戶訂單號 / 圖號", "customer_po", o.customer_po) : ""}
        ${field("備註", "note", o.note, "", true)}
      </div>
      <h3 style="margin-top:16px">明細</h3>
      ${lockedLines ? '<p class="muted">已有交貨紀錄，明細不能修改。</p>' : `
      <div class="table-wrap"><table class="lines"><thead><tr><th>品項</th><th>數量</th><th>單價</th><th class="num">小計</th><th></th></tr></thead>
        <tbody id="lines">${o.lines.map(lineRow).join("")}</tbody></table></div>${itemDatalistHtml("dl-order-items", items)}
      <div class="actions" style="justify-content:space-between">
        <button type="button" class="small ghost" id="add-line">＋ 加一行</button>
        <b id="total"></b></div>`}`,
    onOpen: () => {
      if (lockedLines) return;
      const body = $("#lines");
      const recalc = () => {
        let total = 0;
        $$("tr", body).forEach((tr) => {
          const sub = Number($("[name=qty]", tr).value || 0) * Number($("[name=unit_price]", tr).value || 0);
          total += sub;
          $(".sub", tr).textContent = sub ? money(sub) : "";
        });
        $("#total").textContent = "合計 " + money(total);
      };
      const wire = () => {
        $$("tr", body).forEach((tr) => {
          $(".rm", tr).onclick = () => { if ($$("tr", body).length > 1) tr.remove(); recalc(); };
          $(".ipick", tr).onchange = async (e) => {
            const it = resolvePick(e.target, items);
            const price = $("[name=unit_price]", tr);
            const hint = $(".hint", tr);
            hint.textContent = "";
            if (!it) return recalc();
            // 先找這個客戶 / 廠商以前的成交價，沒有才用品項預設價
            const pid = $("[name=partner_id]").value;
            let past = [];
            try { past = await api("GET", `/api/last-prices?kind=${kind}&item_id=${it.id}&partner_id=${pid}`); } catch {}
            const same = past.find((x) => x.same_partner);
            if (!price.value) price.value = same ? same.unit_price : (kind === "sales" ? it.price : it.cost);
            hint.innerHTML = past.slice(0, 3).map((x) =>
              `${x.same_partner ? "📌 " : ""}${esc(x.doc_date)} ${esc(x.partner_name)} ${n(x.qty)}×<b>${n(x.unit_price)}</b>`).join("<br>")
              || "沒有過去成交紀錄";
            recalc();
          };
          $$("input", tr).forEach((inp) => (inp.oninput = recalc));
        });
      };
      $("#add-line").onclick = () => { body.insertAdjacentHTML("beforeend", lineRow()); wire(); };
      wire();
      recalc();
    },
    onSubmit: async (form) => {
      const data = formData(form);
      delete data.item_id; delete data.qty; delete data.unit_price;
      if (!lockedLines) {
        $$("#lines .ipick").forEach((inp) => resolvePick(inp, items));
        data.lines = $$("#lines tr").map((tr) => ({
          item_id: $("[name=item_id]", tr).value, qty: $("[name=qty]", tr).value,
          unit_price: $("[name=unit_price]", tr).value,
        })).filter((l) => l.item_id || l.qty);
      }
      if (order) {
        await api("PUT", "/api/orders/" + order.id, data);
        toast("已更新");
      } else {
        const r = await api("POST", "/api/orders", { kind, ...data });
        toast("已建立 " + r.no);
      }
      refresh();
    },
  });
}

async function showOrder(id) {
  const o = await api("GET", "/api/orders/" + id);
  const k = KIND[o.kind];
  const active = ["open", "partial"].includes(o.status);
  const delivered = o.lines.reduce((s, l) => s + l.delivered_qty * l.unit_price, 0);
  modal({
    title: `${o.kind === "sales" ? "銷貨訂單" : "採購單"} ${o.no}`,
    hideOk: true,
    body: `
      <div class="detail-head">
        <div><span>${k.partner}</span>${esc(o.partner_name)}</div>
        <div><span>狀態</span>${statusTag(o.status)}</div>
        <div><span>下單日</span>${esc(o.order_date)}</div>
        <div><span>${k.due}</span>${dueTag(o)}</div>
        ${o.customer_po ? `<div><span>客戶單號</span>${esc(o.customer_po)}</div>` : ""}
        ${o.note ? `<div><span>備註</span>${esc(o.note)}</div>` : ""}
      </div>
      ${table(["品項", "規格", ["訂購", "num"], ["已" + k.deliver, "num"], ["單價", "num"], ["小計", "num"], active ? "本次" + k.deliver : ""],
        o.lines.map((l) => {
          const left = l.qty - l.delivered_qty;
          return `<tr><td>${esc(l.code)} ${esc(l.name)}</td><td>${esc(l.spec)}</td>
            <td class="num">${n(l.qty)} ${esc(l.unit)}</td><td class="num">${n(l.delivered_qty)}</td>
            <td class="num">${n(l.unit_price)}</td><td class="num">${money(l.qty * l.unit_price)}</td>
            <td>${active && left > 0 ? `<input type="number" step="any" min="0" max="${left}" value="${left}"
                data-line="${l.id}" style="width:100px">` : ""}</td></tr>`;
        }))}
      <p style="text-align:right">訂單金額 <b>${money(o.total)}</b>　已${k.deliver}金額 <b>${money(delivered)}</b>
        　已${k.pay} <b>${money(o.paid)}</b>　未${k.pay} <b>${money(o.total - o.paid)}</b></p>
      ${o.payments.length ? `<h3>${k.pay}紀錄</h3>` + table(["日期", ["金額", "num"], "備註"],
        o.payments.map((p) => `<tr><td>${esc(p.pay_date)}</td><td class="num">${money(p.amount)}</td><td>${esc(p.note)}</td></tr>`)) : ""}
      <div class="actions" style="justify-content:flex-start">
        ${active ? `<button type="button" id="deliver">確認${k.deliver}</button>` : ""}
        ${o.status !== "cancelled" ? `<button type="button" class="ghost" id="pay">登記${k.pay}</button>` : ""}
        ${o.status !== "cancelled" ? `<button type="button" class="ghost" id="edit">修改</button>` : ""}
        ${o.kind === "sales" && active ? `<button type="button" class="ghost" id="mkwo">開工單</button>` : ""}
        <button type="button" class="ghost" id="print">列印${o.kind === "sales" ? "出貨單" : "採購單"}</button>
        ${o.status === "open" ? `<button type="button" class="ghost" id="cancel" style="color:var(--danger)">作廢</button>` : ""}
      </div>
      <p class="error" id="detail-error"></p>`,
    onOpen: () => {
      const err = (e) => ($("#detail-error").textContent = e.message);
      const close = () => { $("#modal").close(); refresh(); };
      if ($("#deliver")) $("#deliver").onclick = async () => {
        const lines = $$("[data-line]").map((i) => ({ line_id: i.dataset.line, qty: i.value || 0 }));
        if (!confirm(`確認${k.deliver}？庫存會自動${o.kind === "sales" ? "扣除" : "增加"}。`)) return;
        try { await api("POST", `/api/orders/${o.id}/deliver`, { lines }); toast(k.deliver + "完成"); close(); }
        catch (e) { err(e); }
      };
      if ($("#pay")) $("#pay").onclick = () => paymentForm(o);
      if ($("#edit")) $("#edit").onclick = () => orderForm(o.kind, o);
      if ($("#mkwo")) $("#mkwo").onclick = () => workOrderForm({ sales_order_id: o.id, item_id: o.lines[0]?.item_id,
        qty: o.lines[0] ? o.lines[0].qty - o.lines[0].delivered_qty : "", due_date: o.due_date });
      $("#print").onclick = () => printOrder(o);
      if ($("#cancel")) $("#cancel").onclick = async () => {
        if (!confirm("確定作廢這張單？")) return;
        try { await api("POST", `/api/orders/${o.id}/cancel`); toast("已作廢"); close(); } catch (e) { err(e); }
      };
    },
  });
}

function paymentForm(o) {
  const k = KIND[o.kind];
  modal({
    title: `登記${k.pay}：${o.no}`,
    body: `<p>未${k.pay}金額：<b>${money(o.total - o.paid)}</b></p><div class="fields">
      ${field("金額 *", "amount", Math.max(o.total - o.paid, 0), 'type="number" step="any" min="0.01" required')}
      ${field("日期", "pay_date", today(), 'type="date"')}
      ${field("備註（現金 / 匯款 / 支票號碼）", "note", "", "", true)}</div>`,
    onSubmit: async (form) => {
      await api("POST", `/api/orders/${o.id}/payments`, formData(form));
      toast("已登記" + k.pay);
      refresh();
    },
  });
}

function printOrder(o) {
  let area = $("#print-area");
  if (!area) {
    area = document.createElement("div");
    area.id = "print-area";
    document.body.appendChild(area);
  }
  const isSales = o.kind === "sales";
  area.innerHTML = `
    <h2>${isSales ? "出 貨 單" : "採 購 單"}</h2>
    <p>單號：${esc(o.no)}　日期：${today()}　${isSales ? "客戶" : "供應商"}：${esc(o.partner_name)}
      ${o.customer_po ? "　客戶單號：" + esc(o.customer_po) : ""}</p>
    <table><thead><tr><th>料號</th><th>品名</th><th>規格</th><th>數量</th><th>單位</th><th>單價</th><th>金額</th></tr></thead>
    <tbody>${o.lines.map((l) => `<tr><td>${esc(l.code)}</td><td>${esc(l.name)}</td><td>${esc(l.spec)}</td>
      <td style="text-align:right">${n(l.qty)}</td><td>${esc(l.unit)}</td>
      <td style="text-align:right">${n(l.unit_price)}</td><td style="text-align:right">${money(l.qty * l.unit_price)}</td></tr>`).join("")}
    </tbody></table>
    <p style="text-align:right">合計：${money(o.total)}</p>
    ${o.note ? `<p>備註：${esc(o.note)}</p>` : ""}
    <p style="margin-top:48px">${isSales ? "客戶簽收" : "廠商確認"}：＿＿＿＿＿＿＿＿　　經手人：＿＿＿＿＿＿＿＿</p>`;
  window.print();
}

// ---------------------------------------------------------------- 工單

views.workorders = async () => {
  const state = views.workorders.state || (views.workorders.state = { status: "active" });
  let list = await api("GET", "/api/workorders");
  if (state.status === "active") list = list.filter((w) => ["pending", "in_progress"].includes(w.status));
  else if (state.status) list = list.filter((w) => w.status === state.status);
  app.innerHTML = `
    <h1>生產工單 <span class="spacer"></span><button id="add">＋ 開工單</button></h1>
    <div class="toolbar"><select id="status">
      ${[["active", "未完工"], ["", "全部"], ["done", "已完工"], ["cancelled", "已取消"]].map(([v, t]) =>
        `<option value="${v}" ${v === state.status ? "selected" : ""}>${t}</option>`).join("")}</select></div>
    <div class="card">${table(
      ["工單號", "品名", ["數量", "num"], "機台", "客戶 / 訂單", "交期", "狀態"],
      list.map((w) => `<tr class="clickable" data-wo="${w.id}"><td>${esc(w.no)}</td>
        <td>${esc(w.code)} ${esc(w.name)}</td>
        <td class="num">${w.status === "done" ? `${n(w.good_qty)} / ${n(w.qty)}` : n(w.qty)}</td>
        <td>${esc(w.machine)}</td><td>${esc(w.customer || "")} ${esc(w.sales_no || "庫存備貨")}</td>
        <td>${dueTag(w)}</td><td>${statusTag(w.status)}</td></tr>`))}</div>`;
  $("#status").onchange = (e) => { state.status = e.target.value; refresh(); };
  $("#add").onclick = () => workOrderForm();
  $$("[data-wo]").forEach((tr) => (tr.onclick = () => showWorkOrder(tr.dataset.wo)));
};

async function workOrderForm(pre = {}) {
  const [items, sales] = await Promise.all([api("GET", "/api/items?lite=1"), api("GET", "/api/orders?kind=sales")]);
  const openSales = sales.filter((o) => ["open", "partial"].includes(o.status));
  const products = items.filter((i) => ["成品", "半成品"].includes(i.category));
  const materials = items.filter((i) => !["成品"].includes(i.category));
  const machines = [...new Set((await api("GET", "/api/workorders")).map((w) => w.machine).filter(Boolean))].sort();
  const matRow = () => `<tr><td style="min-width:260px">${itemPicker("mat_id", "dl-mats", materials, "", "打材料料號或名稱")}</td>
    <td><input name="per" type="number" step="any" min="0" placeholder="每件用量"></td>
    <td><button type="button" class="small ghost rm">✕</button></td></tr>`;
  modal({
    title: "開立生產工單",
    body: `<div class="fields">
        <label>生產品項 *${itemPicker("item_id", "dl-products", products, pre.item_id, "打料號、品名或圖號搜尋")}</label>
        ${field("生產數量 *", "qty", pre.qty ?? "", 'type="number" step="any" min="0" required')}
        ${selectField("對應訂單", "sales_order_id", [["", "（庫存備貨，不對應訂單）"],
          ...openSales.map((o) => [o.id, `${o.no} ${o.partner_name}`])], pre.sales_order_id)}
        ${field("機台", "machine", "", 'placeholder="例：CNC-02 車床" list="dl-machines"')}
        ${field("交期", "due_date", pre.due_date || "", 'type="date"')}
        ${field("備註（圖號、加工注意事項）", "note", "", "", true)}
      </div>
      <h3 style="margin-top:16px">用料（完工時自動扣庫存）</h3>
      <p class="muted" style="margin-top:0">例：一支 3 米棒料可做 20 件 → 每件用量填 0.05</p>
      <table class="lines"><tbody id="mats">${matRow()}</tbody></table>
      <button type="button" class="small ghost" id="add-mat">＋ 加材料</button>
      ${itemDatalistHtml("dl-products", products)}${itemDatalistHtml("dl-mats", materials)}${datalist("dl-machines", machines)}`,
    onOpen: () => {
      const wirePick = (root, list) => $$(".ipick", root).forEach((inp) => (inp.onchange = () => resolvePick(inp, list)));
      wirePick($("#modal-body .fields"), products);
      wirePick($("#mats"), materials);
      const wire = () => $$("#mats .rm").forEach((b) => (b.onclick = () => b.closest("tr").remove()));
      $("#add-mat").onclick = () => { $("#mats").insertAdjacentHTML("beforeend", matRow()); wire(); wirePick($("#mats"), materials); };
      wire();
      if (!products.length) $("#modal-error").textContent = "還沒有分類為「成品」的品項，請先到庫存品項新增。";
    },
    onSubmit: async (form) => {
      $$(".ipick", form).forEach((inp) => resolvePick(inp, inp.list && inp.list.id === "dl-products" ? products : materials));
      const data = formData(form);
      if (!data.item_id) throw new Error("請選擇要生產的品項（從清單點選）");
      data.materials = $$("#mats tr").map((tr) => ({
        item_id: $("[name=mat_id]", tr).value, qty_per_unit: $("[name=per]", tr).value || 0,
      })).filter((m) => m.item_id);
      delete data.mat_id; delete data.per;
      const r = await api("POST", "/api/workorders", data);
      toast("已開立 " + r.no);
      if (location.hash === "#workorders") refresh(); else go("workorders");
    },
  });
}

async function showWorkOrder(id) {
  const w = await api("GET", "/api/workorders/" + id);
  const active = ["pending", "in_progress"].includes(w.status);
  modal({
    title: `工單 ${w.no}`,
    hideOk: true,
    body: `<div class="detail-head">
        <div><span>品項</span>${esc(w.code)} ${esc(w.name)}</div>
        <div><span>數量</span>${n(w.qty)} ${esc(w.unit)}</div>
        <div><span>狀態</span>${statusTag(w.status)}</div>
        <div><span>機台</span>${esc(w.machine || "—")}</div>
        <div><span>交期</span>${dueTag(w)}</div>
        <div><span>訂單</span>${esc(w.customer || "")} ${esc(w.sales_no || "庫存備貨")}</div>
        ${w.status === "done" ? `<div><span>良品 / 不良</span>${n(w.good_qty)} / ${n(w.scrap_qty)}（${esc(w.finished)} 完工）</div>` : ""}
        ${w.note ? `<div><span>備註</span>${esc(w.note)}</div>` : ""}
      </div>
      <h3>用料</h3>
      ${table(["材料", ["每件用量", "num"], ["本單需求", "num"], ["目前庫存", "num"]], w.materials.map((m) => {
        const need = m.qty_per_unit * w.qty;
        const short = active && m.stock < need;
        return `<tr class="${short ? "low" : ""}"><td>${esc(m.code)} ${esc(m.name)}</td>
          <td class="num">${n(m.qty_per_unit)}</td><td class="num">${n(need)} ${esc(m.unit)}</td>
          <td class="num">${n(m.stock)}${short ? " ⚠️ 不足" : ""}</td></tr>`;
      }), "未設定用料")}
      ${active ? `<h3 style="margin-top:16px">完工回報</h3><div class="fields">
        <label>良品數<input id="good" type="number" step="any" min="0" value="${w.qty}"></label>
        <label>不良品數<input id="scrap" type="number" step="any" min="0" value="0"></label></div>` : ""}
      <div class="actions" style="justify-content:flex-start">
        ${w.status === "pending" ? `<button type="button" id="start">開始生產</button>` : ""}
        ${active ? `<button type="button" id="done">完工入庫</button>` : ""}
        ${active ? `<button type="button" class="ghost" id="cancel" style="color:var(--danger)">取消工單</button>` : ""}
      </div><p class="error" id="detail-error"></p>`,
    onOpen: () => {
      const err = (e) => ($("#detail-error").textContent = e.message);
      const close = () => { $("#modal").close(); refresh(); };
      if ($("#start")) $("#start").onclick = async () => {
        try { await api("POST", `/api/workorders/${w.id}/start`); toast("已開工"); close(); } catch (e) { err(e); }
      };
      if ($("#done")) $("#done").onclick = async () => {
        const good = $("#good").value, scrap = $("#scrap").value;
        if (!confirm(`良品 ${good}、不良 ${scrap}，確認完工？\n原料會自動扣除，良品自動入庫。`)) return;
        try {
          await api("POST", `/api/workorders/${w.id}/complete`, { good_qty: good, scrap_qty: scrap });
          toast("完工入庫");
          close();
        } catch (e) { err(e); }
      };
      if ($("#cancel")) $("#cancel").onclick = async () => {
        if (!confirm("確定取消此工單？")) return;
        try { await api("POST", `/api/workorders/${w.id}/cancel`); toast("已取消"); close(); } catch (e) { err(e); }
      };
    },
  });
}

// ---------------------------------------------------------------- 庫存異動

views.moves = async () => {
  const state = views.moves.state || (views.moves.state = { item_id: "" });
  const [items, moves] = await Promise.all([
    api("GET", "/api/items?lite=1"), api("GET", "/api/stock/moves?" + new URLSearchParams(state))]);
  app.innerHTML = `
    <h1>庫存異動紀錄</h1>
    <div class="toolbar" id="mv-bar">${itemPicker("item_id", "dl-mv", items, state.item_id, "全部品項（最近 300 筆）；打料號篩選")}
      ${state.item_id ? '<button type="button" class="small ghost" id="mv-all">看全部</button>' : ""}</div>
    ${itemDatalistHtml("dl-mv", items)}
    <div class="card">${table(["日期", "品項", "類型", ["數量", "num"], "單據", "備註"],
      moves.map((m) => `<tr><td>${esc(m.move_date)}</td><td>${esc(m.code)} ${esc(m.name)}</td><td>${esc(m.kind)}</td>
        <td class="num" style="color:${m.qty < 0 ? "var(--danger)" : "var(--ok)"}">${m.qty > 0 ? "+" : ""}${n(m.qty)} ${esc(m.unit)}</td>
        <td>${esc(m.ref)}</td><td>${esc(m.note)}</td></tr>`))}</div>`;
  $("#mv-bar .ipick").onchange = (e) => {
    const it = resolvePick(e.target, items);
    if (it) { state.item_id = it.id; refresh(); }
  };
  if ($("#mv-all")) $("#mv-all").onclick = () => { state.item_id = ""; refresh(); };
};

// ---------------------------------------------------------------- 備份 / 匯出

// ---------------------------------------------------------------- 凌越備份轉入

const LY_KINDS = [["sales", "銷貨"], ["sales_return", "銷貨退回"], ["purchase", "進貨"],
  ["purchase_return", "進貨退出"], ["skip", "不匯入"]];

function wireLingyue() {
  const out = $("#ly-result");
  let source = {};
  let preview = null;

  async function load(promise) {
    out.innerHTML = '<p class="muted">讀取中…檔案較大時約需數十秒</p>';
    try {
      preview = await promise;
      renderPreview();
    } catch (e) {
      out.innerHTML = `<p class="error">${esc(e.message)}</p>`;
    }
  }

  $("#ly-file").onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    source = {};
    $("#ly-path").value = "";
    load(fetch("/api/lingyue/upload", { method: "POST", body: file }).then(async (res) => {
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "讀取失敗");
      return data;
    }));
  };
  $("#ly-read").onclick = () => {
    const path = $("#ly-path").value.trim();
    if (!path) return ($("#ly-path").focus());
    source = { path };
    load(api("POST", "/api/lingyue/preview", source));
  };

  function renderPreview() {
    const p = preview;
    const t = (k) => (p.tables.find((x) => x.name === k) || {}).records ?? 0;
    const kindSel = (ty) => `<select data-type="${esc(ty.key)}">${LY_KINDS.map(([v, l]) =>
      `<option value="${v}" ${v === ty.guess ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    const clsSel = (c) => `<select data-class="${esc(c.class)}">${[["customer", "客戶"], ["supplier", "供應商"], ["skip", "不匯入"]].map(([v, l]) =>
      `<option value="${v}" ${v === c.guess ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    out.innerHTML = `
      <p class="tag ok" style="font-size:15px;padding:6px 12px">✅ 讀到凌越資料${p.company ? "：" + esc(p.company) : ""}</p>
      <div class="kpis" style="margin-top:12px">
        <div class="kpi"><div class="label">客戶 / 廠商</div><div class="value">${n(p.partners)}</div></div>
        <div class="kpi"><div class="label">產品 / 材料</div><div class="value">${n(p.items.count)}</div></div>
        <div class="kpi"><div class="label">過去單據</div><div class="value">${n(t("SLIP"))}</div></div>
        <div class="kpi"><div class="label">單據明細</div><div class="value">${n(t("SLIPDT"))}</div></div>
      </div>
      ${p.prefixes.length > 1 ? `<p>這個備份有多組資料：<select id="ly-prefix">${p.prefixes.map((x) =>
        `<option ${x === p.prefix ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></p>` : ""}

      <h3>① 單據種類是什麼？</h3>
      <p class="muted" style="margin-top:0">凌越用代碼區分單據。系統依「往來對象」和「售價 / 成本」先猜好了，
        <b>請看看最常往來的對象對不對</b>，不對就改。不確定的種類（例如報價單、調撥單）選「不匯入」。</p>
      ${table(["凌越代碼", "這是", ["單據數", "num"], ["明細", "num"], "期間", ["金額", "num"], "最常往來"],
        p.types.map((ty) => `<tr><td><code>${esc(ty.class)} / ${esc(ty.slip_fg)}</code></td><td>${kindSel(ty)}</td>
          <td class="num">${n(ty.docs)}</td><td class="num">${n(ty.lines)}</td>
          <td>${esc(ty.date_from)} ～ ${esc(ty.date_to)}</td><td class="num">${money(ty.amount)}</td>
          <td style="white-space:normal">${ty.top_partners.map(esc).join("、")}</td></tr>`), "備份裡沒有單據")}

      <h3 style="margin-top:16px">② 客戶還是廠商？</h3>
      ${table(["凌越代碼", "這是", ["筆數", "num"], "例如"], p.partner_classes.map((c) => `<tr>
        <td><code>${esc(c.class || "（空白）")}</code></td><td>${clsSel(c)}</td><td class="num">${n(c.count)}</td>
        <td style="white-space:normal">${c.samples.map(esc).join("、")}</td></tr>`))}

      <h3 style="margin-top:16px">③ 要轉哪些資料</h3>
      <div class="toolbar">
        <label><input type="checkbox" id="ly-partners" checked style="width:auto"> 客戶 / 廠商</label>
        <label><input type="checkbox" id="ly-items" checked style="width:auto"> 產品、材料與目前庫存（${n(p.items.with_stock)} 項有庫存）</label>
        <label><input type="checkbox" id="ly-history" checked style="width:auto"> 過去的進銷貨紀錄</label>
      </div>
      <p class="muted">產品分類：賣過的 → 成品、只買過的 → 原料、其他 →
        <select id="ly-cat" style="width:auto">${CATEGORIES.map((c) => `<option ${c === "其他" ? "selected" : ""}>${c}</option>`).join("")}</select>
        （之後都可以再改）。庫存會設成凌越備份當時的數量。</p>
      ${p.open_orders.length ? `<details><summary>凌越裡還有 ${p.open_orders.length} 張未結訂單（請在新系統重開）</summary>
        ${table(["單號", "對象", "日期", "交期", "品項"], p.open_orders.map((o) => `<tr><td>${esc(o.no)}</td>
          <td>${esc(o.partner)}</td><td>${esc(o.date)}</td><td>${esc(o.due)}</td>
          <td style="white-space:normal">${o.lines.map((l) => `${esc(l.code)} ${esc(l.name)} ${n(l.qty)}（已交 ${n(l.delivered)}）× ${n(l.price)}`).join("<br>")}</td></tr>`))}
        </details>` : ""}
      <div class="actions" style="justify-content:flex-start">
        <button type="button" id="ly-go">④ 開始轉入</button>
        <span class="muted" style="align-self:center">轉入前建議先按下方「下載備份檔」</span>
      </div>`;
    if ($("#ly-prefix")) $("#ly-prefix").onchange = (e) =>
      load(api("POST", "/api/lingyue/preview", { ...source, prefix: e.target.value }));
    $("#ly-go").onclick = async () => {
      const type_map = Object.fromEntries($$("[data-type]", out).map((s) => [s.dataset.type, s.value]));
      const partner_map = Object.fromEntries($$("[data-class]", out).map((s) => [s.dataset.class, s.value]));
      const parts = { partners: $("#ly-partners").checked, items: $("#ly-items").checked, history: $("#ly-history").checked };
      const chosen = p.types.filter((ty) => type_map[ty.key] !== "skip").map((ty) =>
        `${ty.class}/${ty.slip_fg} → ${LY_KINDS.find((k) => k[0] === type_map[ty.key])[1]}`);
      if (!confirm(`確定轉入？\n\n${chosen.join("\n") || "（不匯入任何單據）"}`)) return;
      $("#ly-go").disabled = true;
      $("#ly-go").textContent = "轉入中，請稍候…";
      try {
        const r = await api("POST", "/api/lingyue/import", { ...source, prefix: p.prefix, type_map, partner_map, parts,
          default_category: $("#ly-cat").value });
        const pt = r.partners || {}, it = r.items, h = r.history || {};
        const sum = (o, k) => Object.values(o).reduce((a, x) => a + (x[k] || 0), 0);
        out.innerHTML = `<div class="card" style="background:var(--ok-bg)"><h3>✅ 轉入完成</h3><ul>
          ${r.partners ? `<li>客戶 / 廠商：新增 ${sum(pt, "created")}、更新 ${sum(pt, "updated")}</li>` : ""}
          ${it ? `<li>產品 / 材料：新增 ${n(it.created)}、更新 ${n(it.updated)}，設定庫存 ${n(it.stock_set)} 項</li>` : ""}
          ${r.history ? `<li>交易紀錄：新增 ${n(sum(h, "created"))} 筆${sum(h, "skipped") ? `（${n(sum(h, "skipped"))} 筆之前已轉過，略過）` : ""}</li>` : ""}
          </ul><p>可以到「客戶/廠商」點進任一家客戶，或用「歷史查詢」查看以前的紀錄。</p></div>`;
        toast("轉入完成");
      } catch (e) {
        $("#ly-go").disabled = false;
        $("#ly-go").textContent = "④ 開始轉入";
        out.insertAdjacentHTML("beforeend", `<p class="error">${esc(e.message)}（資料沒有寫入任何一筆，可以修正後再試）</p>`);
      }
    };
  }
}

views.tools = async () => {
  app.innerHTML = `
    <h1>備份 / 匯出 / 匯入</h1>
    <div class="card" id="ly-card"><h3>⭐ 從凌越備份檔一次轉入（建議）</h3>
      <p>在凌越做一次「備份」，會產生 <code>wstkBKUP.001</code> 這種檔案。把它交給系統，
        <b>客戶、廠商、產品、目前庫存、所有過去的進銷貨紀錄</b>一次轉進來。先預覽、確認後才寫入；重複轉入不會重複。</p>
      <div class="toolbar">
        <input type="file" id="ly-file" accept=".001,.002,.bak,*">
        <span class="muted" style="align-self:center">或輸入檔案位置：</span>
        <input id="ly-path" placeholder="" style="flex:1;min-width:260px">
        <button type="button" class="ghost" id="ly-read">讀取</button>
      </div>
      <div id="ly-result"></div>
    </div>
    <div class="card"><h3>📥 從 Excel / CSV 匯入</h3>
      <p>在舊系統把<b>產品資料（含庫存）</b>、<b>客戶資料</b>、<b>廠商資料</b>分別匯出成 Excel 或 CSV，再從這裡匯入。
        系統會自動辨識欄位，先給你預覽，確認沒問題才寫入。</p>
      <p class="muted"><b>過去的銷貨 / 進貨紀錄</b>：用舊系統的「銷貨明細表」「進貨明細表」，有日期、客戶、品名、數量、單價即可，
        民國年日期也看得懂。匯入後在客戶頁面和「歷史查詢」都查得到，<b>不會影響目前庫存</b>；同一份檔案重複匯入不會重複。</p>
      <p class="muted">支援 .xlsx、.csv（Big5 或 UTF-8 都可以）、.dbf。舊版 .xls 請先用 Excel「另存新檔」成 .xlsx。
        同料號 / 同名稱的資料會更新，不會重複建立；庫存會直接設成檔案裡的數字。<b>匯入前建議先下載一次備份。</b></p>
      <div class="toolbar">
        <select id="imp-target">
          <option value="items">產品 / 材料（含庫存）</option>
          <option value="customer">客戶</option>
          <option value="supplier">供應商</option>
          <option value="sales_history">過去的銷貨紀錄（客戶歷史）</option>
          <option value="purchase_history">過去的進貨紀錄</option>
        </select>
        <select id="imp-cat" title="分類對不上時用這個">
          ${CATEGORIES.map((c) => `<option value="${c}">分類對不上時預設：${c}</option>`).join("")}
        </select>
        <input type="file" id="imp-file" accept=".xlsx,.csv,.txt,.dbf,.xls">
      </div>
      <div id="imp-result"></div>
    </div>
    <div class="card"><h3>💾 備份資料庫</h3>
      <p>所有資料都在一個檔案裡。建議<b>每天下班前按一次</b>，把檔案存到隨身碟或雲端硬碟。</p>
      <a class="btn" href="/api/backup">下載備份檔</a>
      <p class="muted">還原方式：關閉系統 → 把備份檔改名為 <code>cnc.db</code> 覆蓋程式資料夾中的同名檔案 → 重新啟動。</p></div>
    <div class="card"><h3>📊 匯出庫存表（Excel 可開）</h3>
      <p>包含料號、品名、目前庫存、成本與庫存金額，可用來給會計或月底盤點。</p>
      <a class="btn" href="/api/export/stock.csv">下載庫存 CSV</a></div>`;

  $("#ly-path").placeholder = "例：\\\\YHH\\homes\\HSU_HOME\\公司\\wstkBKUP.001";
  wireLingyue();
  const imp = { file: null, data: null, mapping: null, header_row: null };
  const out = $("#imp-result");
  const syncCat = () => ($("#imp-cat").hidden = $("#imp-target").value !== "items");
  syncCat();

  async function preview(commit = false) {
    if (!imp.data) return;
    const body = {
      target: $("#imp-target").value, filename: imp.file.name, data: imp.data,
      mapping: imp.mapping, header_row: imp.header_row, default_category: $("#imp-cat").value, commit,
    };
    out.innerHTML = '<p class="muted">處理中…</p>';
    let r;
    try {
      r = await api("POST", "/api/import", body);
    } catch (e) {
      out.innerHTML = `<p class="error">${esc(e.message)}</p>`;
      return;
    }
    if (r.summary) {
      const s = r.summary;
      out.innerHTML = `<p class="tag ok" style="font-size:15px;padding:6px 12px">✅ 匯入完成：新增 ${s.created} 筆${s.updated !== undefined ? `、更新 ${s.updated} 筆` : ""}
        ${s.stock_set !== undefined ? `、設定庫存 ${s.stock_set} 筆` : ""}
        ${s.skipped !== undefined ? `、已存在略過 ${s.skipped} 筆、自動建立客戶/廠商 ${s.new_partners} 家` : ""}</p>`;
      imp.data = null;
      $("#imp-file").value = "";
      toast("匯入完成");
      return;
    }
    imp.mapping = r.mapping;
    imp.header_row = r.header_row;
    const colOpts = (sel) => `<option value="">（不匯入）</option>` + r.headers.map((h, i) =>
      `<option value="${i}" ${sel === i ? "selected" : ""}>${esc(h || "第 " + (i + 1) + " 欄")}</option>`).join("");
    out.innerHTML = `
      <h3 style="margin-top:12px">① 確認欄位對應</h3>
      <p class="muted" style="margin-top:0">左邊是本系統的欄位，右邊選舊檔案裡對應的欄位。表頭在檔案第
        <input id="imp-hdr" type="number" min="1" value="${r.header_row + 1}" style="width:70px"> 列。</p>
      <div class="fields">${r.fields.map((f) => `<label>${esc(f.label)}
        <select data-map="${f.key}">${colOpts(r.mapping[f.key])}</select></label>`).join("")}</div>
      <h3 style="margin-top:16px">② 預覽（共 ${r.count} 筆，顯示前 ${r.sample.length} 筆）</h3>
      ${table(r.fields.map((f) => f.label.replace(" *", "")), r.sample.map((rec) =>
        `<tr>${r.fields.map((f) => `<td>${esc(rec[f.key] ?? "")}</td>`).join("")}</tr>`), "對應後沒有資料，請檢查欄位或表頭列")}
      ${r.problem_count ? `<details style="margin-top:8px"><summary class="muted">${r.problem_count} 列有問題（點開看）</summary>
        <ul>${r.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></details>` : ""}
      <div class="actions" style="justify-content:flex-start">
        <button type="button" id="imp-go" ${r.missing_required.length || !r.count ? "disabled" : ""}>③ 確認匯入 ${r.count} 筆</button>
        ${r.missing_required.length ? '<span class="error" style="align-self:center">標 * 的欄位一定要選</span>' : ""}
      </div>`;
    $$("[data-map]", out).forEach((sel) => (sel.onchange = () => {
      imp.mapping[sel.dataset.map] = sel.value === "" ? null : Number(sel.value);
      preview();
    }));
    $("#imp-hdr").onchange = (e) => { imp.header_row = Math.max(Number(e.target.value) - 1, 0); imp.mapping = null; preview(); };
    $("#imp-go").onclick = () => {
      if (confirm(`確定匯入 ${r.count} 筆？`)) preview(true);
    };
  }

  $("#imp-file").onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      imp.file = file;
      imp.data = reader.result.split(",")[1];
      imp.mapping = null;
      imp.header_row = null;
      preview();
    };
    reader.readAsDataURL(file);
  };
  $("#imp-target").onchange = () => { syncCat(); imp.mapping = null; imp.header_row = null; preview(); };
};

render();
