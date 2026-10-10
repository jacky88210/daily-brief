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

views.dashboard = async () => {
  const d = await api("GET", "/api/dashboard");
  const orderRow = (o) => `<tr class="clickable" data-order="${o.id}">
      <td>${esc(o.no)}</td><td>${esc(o.partner_name)}</td><td>${dueTag(o)}</td><td>${statusTag(o.status)}</td></tr>`;
  app.innerHTML = `
    <h1>今日總覽 <span class="muted" style="font-size:16px;font-weight:400">${d.today}</span></h1>
    <div class="kpis">
      <div class="kpi"><div class="label">本月出貨金額</div><div class="value">${money(d.month_sales)}</div></div>
      <div class="kpi"><div class="label">應收帳款（已出貨未收）</div><div class="value">${money(d.receivable)}</div></div>
      <div class="kpi"><div class="label">應付帳款（已進貨未付）</div><div class="value">${money(d.payable)}</div></div>
      <div class="kpi ${d.overdue_sales.length ? "alert" : ""}"><div class="label">逾期未交訂單</div><div class="value">${d.overdue_sales.length}</div></div>
      <div class="kpi ${d.low_stock.length ? "alert" : ""}"><div class="label">低於安全庫存</div><div class="value">${d.low_stock.length}</div></div>
    </div>
    <div class="grid">
      <div class="card"><h3>⚠️ 逾期未交貨</h3>
        ${table(["單號", "客戶", "交期", "狀態"], d.overdue_sales.map(orderRow), "沒有逾期訂單 👍")}</div>
      <div class="card"><h3>📅 7 天內要交貨</h3>
        ${table(["單號", "客戶", "交期", "狀態"], d.due_soon_sales.map(orderRow), "近期沒有要交的訂單")}</div>
      <div class="card"><h3>🏭 進行中工單</h3>
        ${table(["工單", "品名", "數量", "機台", "交期", "狀態"], d.work_orders.map((w) => `
          <tr class="clickable" data-wo="${w.id}"><td>${esc(w.no)}</td><td>${esc(w.name)}</td>
          <td class="num">${n(w.qty)}</td><td>${esc(w.machine)}</td><td>${dueTag(w)}</td><td>${statusTag(w.status)}</td></tr>`),
          "目前沒有工單")}</div>
      <div class="card"><h3>📦 需要補貨（低於安全庫存）</h3>
        ${table(["料號", "品名", ["庫存", "num"], ["安全量", "num"]], d.low_stock.map((i) => `
          <tr><td>${esc(i.code)}</td><td>${esc(i.name)}</td><td class="num">${n(i.stock)} ${esc(i.unit)}</td>
          <td class="num">${n(i.safety_stock)}</td></tr>`), "庫存都充足")}</div>
      <div class="card"><h3>🚚 等待供應商交貨</h3>
        ${table(["單號", "供應商", "預計到貨", "狀態"], d.open_purchase.map(orderRow), "沒有未到貨的採購單")}</div>
    </div>`;
  $$("[data-order]").forEach((tr) => (tr.onclick = () => showOrder(tr.dataset.order)));
  $$("[data-wo]").forEach((tr) => (tr.onclick = () => showWorkOrder(tr.dataset.wo)));
};

// ---------------------------------------------------------------- 品項

views.items = async () => {
  const state = views.items.state || (views.items.state = { search: "", category: "" });
  const qs = new URLSearchParams(state).toString();
  const items = await api("GET", "/api/items?" + qs);
  const totalValue = items.reduce((s, i) => s + i.stock * i.cost, 0);
  app.innerHTML = `
    <h1>庫存品項 <span class="spacer"></span><button id="add">＋ 新增品項</button></h1>
    <div class="toolbar">
      <input id="search" placeholder="搜尋料號 / 品名 / 規格" value="${esc(state.search)}">
      <select id="cat"><option value="">全部分類</option>${CATEGORIES.map((c) =>
        `<option ${c === state.category ? "selected" : ""}>${c}</option>`).join("")}</select>
      <span class="muted" style="align-self:center">共 ${items.length} 項，庫存金額約 ${money(totalValue)}</span>
    </div>
    <div class="card">${table(
      ["料號", "品名", "分類", "規格", ["庫存", "num"], ["安全量", "num"], "儲位", ""],
      items.map((i) => `<tr class="${i.safety_stock && i.stock < i.safety_stock ? "low" : ""}">
        <td>${esc(i.code)}</td><td>${esc(i.name)}</td><td>${esc(i.category)}</td><td>${esc(i.spec)}</td>
        <td class="num">${n(i.stock)} ${esc(i.unit)}</td><td class="num">${n(i.safety_stock)}</td>
        <td>${esc(i.location)}</td>
        <td style="white-space:nowrap"><button class="small ghost" data-edit="${i.id}">編輯</button>
          <button class="small ghost" data-count="${i.id}">盤點</button>
          <button class="small ghost" data-moves="${i.id}">異動</button></td></tr>`),
      "還沒有品項，請按「新增品項」")}</div>`;
  let timer;
  $("#search").oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.search = e.target.value; refresh(); }, 300);
  };
  if (state.search) $("#search").focus();
  $("#search").setSelectionRange(state.search.length, state.search.length);
  $("#cat").onchange = (e) => { state.category = e.target.value; refresh(); };
  $("#add").onclick = () => itemForm();
  const byId = Object.fromEntries(items.map((i) => [i.id, i]));
  $$("[data-edit]").forEach((b) => (b.onclick = () => itemForm(byId[b.dataset.edit])));
  $$("[data-count]").forEach((b) => (b.onclick = () => stockCount(byId[b.dataset.count])));
  $$("[data-moves]").forEach((b) => (b.onclick = () => {
    views.moves.state = { item_id: b.dataset.moves };
    go("moves");
  }));
};

function itemForm(item) {
  const i = item || { category: "原料", unit: "個" };
  modal({
    title: item ? "編輯品項" : "新增品項",
    body: `<div class="fields">
      ${field("料號 *", "code", i.code, "required")}
      ${field("品名 *", "name", i.name, "required")}
      ${selectField("分類", "category", CATEGORIES.map((c) => [c, c]), i.category)}
      ${field("規格（材質 / 尺寸 / 圖號）", "spec", i.spec)}
      ${field("單位", "unit", i.unit)}
      ${field("安全庫存（低於會提醒）", "safety_stock", i.safety_stock ?? 0, 'type="number" step="any" min="0"')}
      ${field("成本單價", "cost", i.cost ?? 0, 'type="number" step="any" min="0"')}
      ${field("售價", "price", i.price ?? 0, 'type="number" step="any" min="0"')}
      ${field("儲位", "location", i.location)}
      ${item ? "" : field("期初庫存", "opening_stock", 0, 'type="number" step="any" min="0"')}
      ${field("備註", "note", i.note, "", true)}
    </div>
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
        ${field("實盤數量 *", "actual", item.stock, 'type="number" step="any" min="0" required')}
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

views.partners = async () => {
  const list = await api("GET", "/api/partners");
  const section = (type, title) => `<div class="card"><h3>${title}</h3>${table(
    ["名稱", "聯絡人", "電話", "統編", ""],
    list.filter((p) => p.type === type).map((p) => `<tr class="clickable" data-partner="${p.id}">
      <td>${esc(p.name)}</td><td>${esc(p.contact)}</td>
      <td>${esc(p.phone)}</td><td>${esc(p.tax_id)}</td>
      <td><button class="small ghost" data-edit="${p.id}">編輯</button></td></tr>`))}</div>`;
  app.innerHTML = `
    <h1>客戶 / 廠商 <span class="spacer"></span>
      <button id="add-c">＋ 新增客戶</button><button id="add-s" class="ghost">＋ 新增供應商</button></h1>
    ${section("customer", "客戶")}${section("supplier", "供應商")}`;
  $("#add-c").onclick = () => partnerForm({ type: "customer" });
  $("#add-s").onclick = () => partnerForm({ type: "supplier" });
  const byId = Object.fromEntries(list.map((p) => [p.id, p]));
  $$("[data-edit]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); partnerForm(byId[b.dataset.edit]); }));
  $$("[data-partner]").forEach((tr) => (tr.onclick = () => go("partner:" + tr.dataset.partner)));
};

// 單一客戶 / 廠商：基本資料 + 所有往來紀錄（舊系統 + 本系統）
views.partner = async (id) => {
  const { partner: p, stats, history } = await api("GET", `/api/partners/${id}/summary`);
  const isCus = p.type === "customer";
  app.innerHTML = `
    <h1><a class="muted" style="cursor:pointer" id="back">客戶/廠商</a> › ${esc(p.name)}
      <span class="spacer"></span><button class="ghost" id="edit">編輯資料</button>
      ${isCus ? '<button id="new-order">＋ 開新訂單</button>' : '<button id="new-order">＋ 開採購單</button>'}</h1>
    <div class="card"><div class="detail-head">
      <div><span>聯絡人</span>${esc(p.contact || "—")}</div><div><span>電話</span>${esc(p.phone || "—")}</div>
      <div><span>統編</span>${esc(p.tax_id || "—")}</div><div><span>地址</span>${esc(p.address || "—")}</div>
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
  $("#back").onclick = () => go("partners");
  $("#edit").onclick = () => partnerForm(p);
  $("#new-order").onclick = () => orderForm(isCus ? "sales" : "purchase", null, p.id);
  $$("[data-search]").forEach((tr) => (tr.onclick = () => {
    views.history.state = { search: tr.dataset.search, kind: "", date_from: "", date_to: "" };
    go("history");
  }));
  wireHistoryLinks();
};

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

function partnerForm(p) {
  const label = p.type === "customer" ? "客戶" : "供應商";
  modal({
    title: (p.id ? "編輯" : "新增") + label,
    body: `<div class="fields">
      <input type="hidden" name="type" value="${p.type}">
      ${field("名稱 *", "name", p.name, "required")}
      ${field("聯絡人", "contact", p.contact)}
      ${field("電話", "phone", p.phone)}
      ${field("統一編號", "tax_id", p.tax_id)}
      ${field("地址", "address", p.address, "", true)}
      ${field("備註（付款條件、交貨習慣…）", "note", p.note, "", true)}
    </div>`,
    onSubmit: async (form) => {
      if (p.id) await api("PUT", "/api/partners/" + p.id, formData(form));
      else await api("POST", "/api/partners", formData(form));
      toast("已儲存");
      refresh();
    },
  });
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
    api("GET", "/api/partners?type=" + k.ptype), api("GET", "/api/items")]);
  if (!partners.length) return alert(`請先到「客戶/廠商」新增${k.partner}`);
  if (!items.length) return alert("請先到「庫存品項」新增品項");
  const o = order || { order_date: today(), lines: [{}], partner_id: presetPartner };
  const itemOpts = (sel) => `<option value="">— 選擇品項 —</option>` + items.map((i) =>
    `<option value="${i.id}" ${String(i.id) === String(sel) ? "selected" : ""}>${esc(i.code)} ${esc(i.name)}（庫存 ${n(i.stock)}）</option>`).join("");
  const lineRow = (l = {}) => `<tr>
      <td style="min-width:220px"><select name="item_id">${itemOpts(l.item_id)}</select><div class="hint muted"></div></td>
      <td><input name="qty" type="number" step="any" min="0" value="${l.qty ?? ""}" placeholder="數量"></td>
      <td><input name="unit_price" type="number" step="any" min="0" value="${l.unit_price ?? ""}" placeholder="單價"></td>
      <td class="num sub"></td>
      <td><button type="button" class="small ghost rm">✕</button></td></tr>`;
  const lockedLines = order && order.lines.some((l) => l.delivered_qty);
  modal({
    title: (order ? "修改 " + order.no : "新增" + (kind === "sales" ? "銷貨訂單" : "採購單")),
    body: `<div class="fields">
        ${selectField(k.partner + " *", "partner_id", [["", "— 請選擇 —"], ...partners.map((p) => [p.id, p.name])], o.partner_id)}
        ${field("下單日期", "order_date", o.order_date, 'type="date"')}
        ${field(k.due, "due_date", o.due_date, 'type="date"')}
        ${kind === "sales" ? field("客戶訂單號 / 圖號", "customer_po", o.customer_po) : ""}
        ${field("備註", "note", o.note, "", true)}
      </div>
      <h3 style="margin-top:16px">明細</h3>
      ${lockedLines ? '<p class="muted">已有交貨紀錄，明細不能修改。</p>' : `
      <div class="table-wrap"><table class="lines"><thead><tr><th>品項</th><th>數量</th><th>單價</th><th class="num">小計</th><th></th></tr></thead>
        <tbody id="lines">${o.lines.map(lineRow).join("")}</tbody></table></div>
      <div class="actions" style="justify-content:space-between">
        <button type="button" class="small ghost" id="add-line">＋ 加一行</button>
        <b id="total"></b></div>`}`,
    onOpen: () => {
      if (lockedLines) return;
      const body = $("#lines");
      const byId = Object.fromEntries(items.map((i) => [i.id, i]));
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
          $("[name=item_id]", tr).onchange = async (e) => {
            const it = byId[e.target.value];
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
  const [items, sales] = await Promise.all([api("GET", "/api/items"), api("GET", "/api/orders?kind=sales")]);
  const openSales = sales.filter((o) => ["open", "partial"].includes(o.status));
  const products = items.filter((i) => ["成品", "半成品"].includes(i.category));
  const materials = items.filter((i) => !["成品"].includes(i.category));
  const matRow = () => `<tr><td style="min-width:240px"><select name="mat_id"><option value="">— 選擇材料 —</option>
      ${materials.map((i) => `<option value="${i.id}">${esc(i.code)} ${esc(i.name)} ${esc(i.spec)}（庫存 ${n(i.stock)} ${esc(i.unit)}）</option>`).join("")}
    </select></td><td><input name="per" type="number" step="any" min="0" placeholder="每件用量"></td>
    <td><button type="button" class="small ghost rm">✕</button></td></tr>`;
  modal({
    title: "開立生產工單",
    body: `<div class="fields">
        ${selectField("生產品項 *", "item_id", [["", "— 請選擇成品 —"], ...products.map((i) => [i.id, `${i.code} ${i.name}`])], pre.item_id)}
        ${field("生產數量 *", "qty", pre.qty ?? "", 'type="number" step="any" min="0" required')}
        ${selectField("對應訂單", "sales_order_id", [["", "（庫存備貨，不對應訂單）"],
          ...openSales.map((o) => [o.id, `${o.no} ${o.partner_name}`])], pre.sales_order_id)}
        ${field("機台", "machine", "", 'placeholder="例：CNC-02 車床"')}
        ${field("交期", "due_date", pre.due_date || "", 'type="date"')}
        ${field("備註（圖號、加工注意事項）", "note", "", "", true)}
      </div>
      <h3 style="margin-top:16px">用料（完工時自動扣庫存）</h3>
      <p class="muted" style="margin-top:0">例：一支 3 米棒料可做 20 件 → 每件用量填 0.05</p>
      <table class="lines"><tbody id="mats">${matRow()}</tbody></table>
      <button type="button" class="small ghost" id="add-mat">＋ 加材料</button>`,
    onOpen: () => {
      const wire = () => $$("#mats .rm").forEach((b) => (b.onclick = () => b.closest("tr").remove()));
      $("#add-mat").onclick = () => { $("#mats").insertAdjacentHTML("beforeend", matRow()); wire(); };
      wire();
      if (!products.length) $("#modal-error").textContent = "還沒有分類為「成品」的品項，請先到庫存品項新增。";
    },
    onSubmit: async (form) => {
      const data = formData(form);
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
    api("GET", "/api/items"), api("GET", "/api/stock/moves?" + new URLSearchParams(state))]);
  app.innerHTML = `
    <h1>庫存異動紀錄</h1>
    <div class="toolbar"><select id="item"><option value="">全部品項（最近 300 筆）</option>
      ${items.map((i) => `<option value="${i.id}" ${String(i.id) === String(state.item_id) ? "selected" : ""}>
        ${esc(i.code)} ${esc(i.name)}</option>`).join("")}</select></div>
    <div class="card">${table(["日期", "品項", "類型", ["數量", "num"], "單據", "備註"],
      moves.map((m) => `<tr><td>${esc(m.move_date)}</td><td>${esc(m.code)} ${esc(m.name)}</td><td>${esc(m.kind)}</td>
        <td class="num" style="color:${m.qty < 0 ? "var(--danger)" : "var(--ok)"}">${m.qty > 0 ? "+" : ""}${n(m.qty)} ${esc(m.unit)}</td>
        <td>${esc(m.ref)}</td><td>${esc(m.note)}</td></tr>`))}</div>`;
  $("#item").onchange = (e) => { state.item_id = e.target.value; refresh(); };
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
