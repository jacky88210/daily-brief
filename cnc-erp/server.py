#!/usr/bin/env python3
"""CNC 加工廠進銷存系統（輕量版）

只需要 Python 3.9 以上，不用安裝任何套件。
資料存在同資料夾的 cnc.db（SQLite），備份只要複製這個檔案。

啟動：python server.py            → 本機瀏覽器開 http://127.0.0.1:8080
      python server.py --lan      → 讓工廠內其他電腦/手機也能連線
"""
import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 直接執行 server.py 時，讓 importer / demo_data 的 `import server` 拿到同一份模組
sys.modules.setdefault("server", sys.modules[__name__])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.environ.get("CNC_DB", os.path.join(BASE_DIR, "cnc.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '原料',
    spec TEXT DEFAULT '',
    unit TEXT DEFAULT '個',
    safety_stock REAL DEFAULT 0,
    cost REAL DEFAULT 0,
    price REAL DEFAULT 0,
    location TEXT DEFAULT '',
    note TEXT DEFAULT '',
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS partners (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('customer', 'supplier')),
    name TEXT NOT NULL,
    contact TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    tax_id TEXT DEFAULT '',
    address TEXT DEFAULT '',
    note TEXT DEFAULT '',
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('purchase', 'sales')),
    no TEXT NOT NULL UNIQUE,
    partner_id INTEGER NOT NULL REFERENCES partners(id),
    order_date TEXT NOT NULL,
    due_date TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    customer_po TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS order_lines (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL,
    unit_price REAL NOT NULL DEFAULT 0,
    delivered_qty REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount REAL NOT NULL,
    pay_date TEXT NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS work_orders (
    id INTEGER PRIMARY KEY,
    no TEXT NOT NULL UNIQUE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL,
    sales_order_id INTEGER REFERENCES orders(id),
    machine TEXT DEFAULT '',
    due_date TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    good_qty REAL DEFAULT 0,
    scrap_qty REAL DEFAULT 0,
    created TEXT NOT NULL,
    finished TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wo_materials (
    id INTEGER PRIMARY KEY,
    wo_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty_per_unit REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_moves (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT DEFAULT '',
    move_date TEXT NOT NULL,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_moves_item ON stock_moves(item_id);
CREATE INDEX IF NOT EXISTS idx_lines_order ON order_lines(order_id);
"""

ITEM_FIELDS = ["code", "name", "category", "spec", "unit", "safety_stock",
               "cost", "price", "location", "note"]
PARTNER_FIELDS = ["type", "name", "contact", "phone", "tax_id", "address", "note"]
ORDER_PREFIX = {"purchase": "PO", "sales": "SO"}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=None):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def today():
    return date.today().isoformat()


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def num(value, field, minimum=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ApiError(f"{field} 必須是數字")
    if minimum is not None and v < minimum:
        raise ApiError(f"{field} 不可小於 {minimum}")
    return v


def stock_of(conn, item_id):
    r = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM stock_moves WHERE item_id = ?",
                     (item_id,)).fetchone()
    return r[0]


def add_move(conn, item_id, qty, kind, ref="", note="", allow_negative=False):
    if not allow_negative and qty < 0:
        have = stock_of(conn, item_id)
        if have + qty < -1e-9:
            item = conn.execute("SELECT code, name FROM items WHERE id = ?", (item_id,)).fetchone()
            raise ApiError(f"庫存不足：{item['code']} {item['name']} 目前 {have:g}，需要 {-qty:g}")
    conn.execute(
        "INSERT INTO stock_moves (item_id, qty, kind, ref, move_date, note) VALUES (?,?,?,?,?,?)",
        (item_id, qty, kind, ref, today(), note))


def next_no(conn, table, prefix):
    stem = f"{prefix}-{date.today().strftime('%Y%m%d')}-"
    r = conn.execute(f"SELECT no FROM {table} WHERE no LIKE ? ORDER BY no DESC LIMIT 1",
                     (stem + "%",)).fetchone()
    seq = int(r["no"].rsplit("-", 1)[1]) + 1 if r else 1
    return f"{stem}{seq:03d}"


# ---------------------------------------------------------------- items

def list_items(conn, q):
    sql = """SELECT i.*, COALESCE(SUM(m.qty), 0) AS stock
             FROM items i LEFT JOIN stock_moves m ON m.item_id = i.id
             WHERE i.active = 1"""
    args = []
    if q.get("category"):
        sql += " AND i.category = ?"
        args.append(q["category"])
    if q.get("search"):
        sql += " AND (i.code LIKE ? OR i.name LIKE ? OR i.spec LIKE ?)"
        args += [f"%{q['search']}%"] * 3
    sql += " GROUP BY i.id ORDER BY i.category, i.code"
    return rows(conn.execute(sql, args))


def clean_item(body):
    data = {f: body.get(f, "") for f in ITEM_FIELDS}
    if not str(data["code"]).strip() or not str(data["name"]).strip():
        raise ApiError("料號與品名為必填")
    for f in ("safety_stock", "cost", "price"):
        data[f] = num(data[f] or 0, f, 0)
    return data


def create_item(conn, body):
    data = clean_item(body)
    try:
        cur = conn.execute(
            f"INSERT INTO items ({','.join(ITEM_FIELDS)}) VALUES ({','.join('?' * len(ITEM_FIELDS))})",
            [data[f] for f in ITEM_FIELDS])
    except sqlite3.IntegrityError:
        raise ApiError(f"料號 {data['code']} 已存在")
    opening = num(body.get("opening_stock") or 0, "期初庫存", 0)
    if opening:
        add_move(conn, cur.lastrowid, opening, "期初", note="建立品項時輸入")
    return {"id": cur.lastrowid}


def update_item(conn, item_id, body):
    data = clean_item(body)
    try:
        conn.execute(f"UPDATE items SET {','.join(f + '=?' for f in ITEM_FIELDS)} WHERE id = ?",
                     [data[f] for f in ITEM_FIELDS] + [item_id])
    except sqlite3.IntegrityError:
        raise ApiError(f"料號 {data['code']} 已存在")
    return {"ok": True}


def delete_item(conn, item_id):
    # 有歷史單據，只做停用不真的刪除
    conn.execute("UPDATE items SET active = 0 WHERE id = ?", (item_id,))
    return {"ok": True}


# ---------------------------------------------------------------- partners

def list_partners(conn, q):
    sql = "SELECT * FROM partners WHERE active = 1"
    args = []
    if q.get("type"):
        sql += " AND type = ?"
        args.append(q["type"])
    return rows(conn.execute(sql + " ORDER BY name", args))


def clean_partner(body):
    data = {f: body.get(f, "") for f in PARTNER_FIELDS}
    if data["type"] not in ("customer", "supplier"):
        raise ApiError("類型必須是客戶或供應商")
    if not str(data["name"]).strip():
        raise ApiError("名稱為必填")
    return data


def create_partner(conn, body):
    data = clean_partner(body)
    cur = conn.execute(
        f"INSERT INTO partners ({','.join(PARTNER_FIELDS)}) VALUES ({','.join('?' * len(PARTNER_FIELDS))})",
        [data[f] for f in PARTNER_FIELDS])
    return {"id": cur.lastrowid}


def update_partner(conn, pid, body):
    data = clean_partner(body)
    conn.execute(f"UPDATE partners SET {','.join(f + '=?' for f in PARTNER_FIELDS)} WHERE id = ?",
                 [data[f] for f in PARTNER_FIELDS] + [pid])
    return {"ok": True}


def delete_partner(conn, pid):
    conn.execute("UPDATE partners SET active = 0 WHERE id = ?", (pid,))
    return {"ok": True}


# ---------------------------------------------------------------- orders

ORDER_SUMMARY_SQL = """
SELECT o.*, p.name AS partner_name,
       COALESCE((SELECT SUM(qty * unit_price) FROM order_lines WHERE order_id = o.id), 0) AS total,
       COALESCE((SELECT SUM(amount) FROM payments WHERE order_id = o.id), 0) AS paid
FROM orders o JOIN partners p ON p.id = o.partner_id
"""


def list_orders(conn, q):
    sql = ORDER_SUMMARY_SQL + " WHERE o.kind = ?"
    args = [q.get("kind", "sales")]
    if q.get("status"):
        sql += " AND o.status = ?"
        args.append(q["status"])
    return rows(conn.execute(sql + " ORDER BY o.order_date DESC, o.id DESC", args))


def get_order(conn, oid):
    o = conn.execute(ORDER_SUMMARY_SQL + " WHERE o.id = ?", (oid,)).fetchone()
    if not o:
        raise ApiError("找不到單據", 404)
    o = dict(o)
    o["lines"] = rows(conn.execute(
        """SELECT l.*, i.code, i.name, i.unit, i.spec FROM order_lines l
           JOIN items i ON i.id = l.item_id WHERE l.order_id = ? ORDER BY l.id""", (oid,)))
    o["payments"] = rows(conn.execute(
        "SELECT * FROM payments WHERE order_id = ? ORDER BY pay_date, id", (oid,)))
    return o


def clean_lines(conn, lines):
    if not lines:
        raise ApiError("至少要有一筆明細")
    out = []
    for ln in lines:
        item_id = int(ln.get("item_id") or 0)
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise ApiError("明細中有不存在的品項")
        out.append((item_id, num(ln.get("qty"), "數量", 0.000001),
                    num(ln.get("unit_price") or 0, "單價", 0)))
    return out


def save_order_header(conn, body, kind):
    partner = conn.execute("SELECT type FROM partners WHERE id = ?",
                           (int(body.get("partner_id") or 0),)).fetchone()
    expected = "supplier" if kind == "purchase" else "customer"
    if not partner or partner["type"] != expected:
        raise ApiError("請選擇" + ("供應商" if kind == "purchase" else "客戶"))
    return (int(body["partner_id"]), body.get("order_date") or today(),
            body.get("due_date", ""), body.get("customer_po", ""), body.get("note", ""))


def create_order(conn, body):
    kind = body.get("kind")
    if kind not in ORDER_PREFIX:
        raise ApiError("單據類型錯誤")
    header = save_order_header(conn, body, kind)
    lines = clean_lines(conn, body.get("lines"))
    no = next_no(conn, "orders", ORDER_PREFIX[kind])
    cur = conn.execute(
        """INSERT INTO orders (kind, no, partner_id, order_date, due_date, customer_po, note)
           VALUES (?,?,?,?,?,?,?)""", (kind, no) + header)
    conn.executemany("INSERT INTO order_lines (order_id, item_id, qty, unit_price) VALUES (?,?,?,?)",
                     [(cur.lastrowid,) + ln for ln in lines])
    return {"id": cur.lastrowid, "no": no}


def update_order(conn, oid, body):
    o = get_order(conn, oid)
    if o["status"] == "cancelled":
        raise ApiError("已作廢的單據不能修改")
    header = save_order_header(conn, body, o["kind"])
    conn.execute("UPDATE orders SET partner_id=?, order_date=?, due_date=?, customer_po=?, note=? WHERE id=?",
                 header + (oid,))
    if "lines" in body:
        if any(ln["delivered_qty"] for ln in o["lines"]):
            raise ApiError("已有進貨/出貨紀錄，明細不能修改（表頭已更新）")
        lines = clean_lines(conn, body["lines"])
        conn.execute("DELETE FROM order_lines WHERE order_id = ?", (oid,))
        conn.executemany("INSERT INTO order_lines (order_id, item_id, qty, unit_price) VALUES (?,?,?,?)",
                         [(oid,) + ln for ln in lines])
    return {"ok": True}


def deliver_order(conn, oid, body):
    """進貨（採購單）或出貨（銷貨單），可分批。body.lines = [{line_id, qty}]；不給就全部交完。"""
    o = get_order(conn, oid)
    if o["status"] in ("done", "cancelled"):
        raise ApiError("此單據已結案或作廢")
    by_id = {ln["id"]: ln for ln in o["lines"]}
    req = body.get("lines")
    if req is None:
        req = [{"line_id": ln["id"], "qty": ln["qty"] - ln["delivered_qty"]} for ln in o["lines"]]
    sign = 1 if o["kind"] == "purchase" else -1
    kind = "進貨" if o["kind"] == "purchase" else "出貨"
    moved = 0
    for r in req:
        ln = by_id.get(int(r.get("line_id") or 0))
        if not ln:
            raise ApiError("明細不存在")
        qty = num(r.get("qty") or 0, "數量", 0)
        if not qty:
            continue
        remaining = ln["qty"] - ln["delivered_qty"]
        if qty > remaining + 1e-9:
            raise ApiError(f"{ln['code']} 超過未交數量 {remaining:g}")
        add_move(conn, ln["item_id"], sign * qty, kind, o["no"], body.get("note", ""))
        conn.execute("UPDATE order_lines SET delivered_qty = delivered_qty + ? WHERE id = ?",
                     (qty, ln["id"]))
        if o["kind"] == "purchase" and ln["unit_price"]:
            conn.execute("UPDATE items SET cost = ? WHERE id = ?", (ln["unit_price"], ln["item_id"]))
        moved += 1
    if not moved:
        raise ApiError("沒有要交貨的數量")
    left = conn.execute("SELECT COUNT(*) FROM order_lines WHERE order_id = ? AND delivered_qty < qty - 1e-9",
                        (oid,)).fetchone()[0]
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", ("done" if not left else "partial", oid))
    return {"ok": True}


def add_payment(conn, oid, body):
    o = get_order(conn, oid)
    if o["status"] == "cancelled":
        raise ApiError("已作廢的單據不能收付款")
    amount = num(body.get("amount"), "金額", 0.01)
    conn.execute("INSERT INTO payments (order_id, amount, pay_date, note) VALUES (?,?,?,?)",
                 (oid, amount, body.get("pay_date") or today(), body.get("note", "")))
    return {"ok": True}


def cancel_order(conn, oid):
    o = get_order(conn, oid)
    if any(ln["delivered_qty"] for ln in o["lines"]):
        raise ApiError("已有進貨/出貨紀錄，不能作廢；請用庫存調整處理")
    conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (oid,))
    return {"ok": True}


# ---------------------------------------------------------------- work orders

WO_SQL = """
SELECT w.*, i.code, i.name, i.unit, o.no AS sales_no, p.name AS customer
FROM work_orders w JOIN items i ON i.id = w.item_id
LEFT JOIN orders o ON o.id = w.sales_order_id
LEFT JOIN partners p ON p.id = o.partner_id
"""


def list_work_orders(conn, q):
    sql = WO_SQL
    args = []
    if q.get("status"):
        sql += " WHERE w.status = ?"
        args.append(q["status"])
    return rows(conn.execute(sql + " ORDER BY (w.status = 'done'), w.due_date, w.id DESC", args))


def get_work_order(conn, wid):
    w = conn.execute(WO_SQL + " WHERE w.id = ?", (wid,)).fetchone()
    if not w:
        raise ApiError("找不到工單", 404)
    w = dict(w)
    w["materials"] = rows(conn.execute(
        """SELECT m.*, i.code, i.name, i.unit, COALESCE((SELECT SUM(qty) FROM stock_moves s
               WHERE s.item_id = m.item_id), 0) AS stock
           FROM wo_materials m JOIN items i ON i.id = m.item_id WHERE m.wo_id = ?""", (wid,)))
    return w


def create_work_order(conn, body):
    item_id = int(body.get("item_id") or 0)
    if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        raise ApiError("請選擇要生產的成品")
    qty = num(body.get("qty"), "生產數量", 0.000001)
    so = body.get("sales_order_id") or None
    no = next_no(conn, "work_orders", "WO")
    cur = conn.execute(
        """INSERT INTO work_orders (no, item_id, qty, sales_order_id, machine, due_date, created, note)
           VALUES (?,?,?,?,?,?,?,?)""",
        (no, item_id, qty, so, body.get("machine", ""), body.get("due_date", ""), today(),
         body.get("note", "")))
    for m in body.get("materials") or []:
        mid = int(m.get("item_id") or 0)
        if not mid:
            continue
        conn.execute("INSERT INTO wo_materials (wo_id, item_id, qty_per_unit) VALUES (?,?,?)",
                     (cur.lastrowid, mid, num(m.get("qty_per_unit"), "每件用量", 0)))
    return {"id": cur.lastrowid, "no": no}


def start_work_order(conn, wid):
    w = get_work_order(conn, wid)
    if w["status"] != "pending":
        raise ApiError("只有待生產的工單可以開工")
    conn.execute("UPDATE work_orders SET status = 'in_progress' WHERE id = ?", (wid,))
    return {"ok": True}


def complete_work_order(conn, wid, body):
    """完工：依 (良品 + 不良品) × 每件用量 扣原料，良品入庫。"""
    w = get_work_order(conn, wid)
    if w["status"] in ("done", "cancelled"):
        raise ApiError("此工單已結案")
    good = num(body.get("good_qty", w["qty"]), "良品數", 0)
    scrap = num(body.get("scrap_qty") or 0, "不良品數", 0)
    if good + scrap <= 0:
        raise ApiError("良品與不良品數量不可都為 0")
    for m in w["materials"]:
        use = m["qty_per_unit"] * (good + scrap)
        if use:
            add_move(conn, m["item_id"], -use, "領料", w["no"])
    if good:
        add_move(conn, w["item_id"], good, "完工入庫", w["no"])
    conn.execute(
        "UPDATE work_orders SET status='done', good_qty=?, scrap_qty=?, finished=? WHERE id=?",
        (good, scrap, today(), wid))
    return {"ok": True}


def cancel_work_order(conn, wid):
    w = get_work_order(conn, wid)
    if w["status"] == "done":
        raise ApiError("已完工的工單不能取消")
    conn.execute("UPDATE work_orders SET status = 'cancelled' WHERE id = ?", (wid,))
    return {"ok": True}


# ---------------------------------------------------------------- stock

def list_moves(conn, q):
    sql = """SELECT m.*, i.code, i.name, i.unit FROM stock_moves m
             JOIN items i ON i.id = m.item_id"""
    args = []
    if q.get("item_id"):
        sql += " WHERE m.item_id = ?"
        args.append(int(q["item_id"]))
    return rows(conn.execute(sql + " ORDER BY m.id DESC LIMIT 300", args))


def adjust_stock(conn, body):
    """盤點：輸入實際數量，系統自動算差異。"""
    item_id = int(body.get("item_id") or 0)
    if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        raise ApiError("品項不存在")
    actual = num(body.get("actual"), "實盤數量", 0)
    diff = actual - stock_of(conn, item_id)
    if abs(diff) > 1e-9:
        add_move(conn, item_id, diff, "盤點調整", note=body.get("note", ""), allow_negative=True)
    return {"ok": True, "diff": diff}


# ---------------------------------------------------------------- dashboard

def dashboard(conn, q):
    t = today()
    month = t[:7]
    low = rows(conn.execute(
        """SELECT i.id, i.code, i.name, i.unit, i.safety_stock, COALESCE(SUM(m.qty), 0) AS stock
           FROM items i LEFT JOIN stock_moves m ON m.item_id = i.id
           WHERE i.active = 1 AND i.safety_stock > 0
           GROUP BY i.id HAVING stock < i.safety_stock ORDER BY i.code"""))
    open_orders = rows(conn.execute(
        ORDER_SUMMARY_SQL + " WHERE o.status IN ('open', 'partial') ORDER BY o.due_date = '', o.due_date"))
    overdue = [o for o in open_orders if o["kind"] == "sales" and o["due_date"] and o["due_date"] < t]
    due_soon = [o for o in open_orders if o["kind"] == "sales" and o["due_date"]
                and t <= o["due_date"] <= _plus_days(t, 7)]
    # 應收/應付：以「已交貨金額 − 已收付款」計算，尚未交貨的部分不算帳款
    balances = dict(conn.execute(
        """SELECT kind, COALESCE(SUM(MAX(delivered - paid, 0)), 0) FROM (
               SELECT o.kind,
                      COALESCE((SELECT SUM(delivered_qty * unit_price) FROM order_lines
                                WHERE order_id = o.id), 0) AS delivered,
                      COALESCE((SELECT SUM(amount) FROM payments WHERE order_id = o.id), 0) AS paid
               FROM orders o WHERE o.status != 'cancelled')
           GROUP BY kind""").fetchall())
    month_sales = conn.execute(
        """SELECT COALESCE(SUM(-m.qty * (SELECT l.unit_price FROM order_lines l
                                          WHERE l.order_id = o.id AND l.item_id = m.item_id LIMIT 1)), 0)
           FROM stock_moves m JOIN orders o ON o.no = m.ref
           WHERE m.kind = '出貨' AND m.move_date LIKE ?""", (month + "%",)).fetchone()[0]
    wos = rows(conn.execute(WO_SQL + " WHERE w.status IN ('pending', 'in_progress') ORDER BY w.due_date"))
    return {
        "today": t,
        "low_stock": low,
        "overdue_sales": overdue,
        "due_soon_sales": due_soon,
        "open_purchase": [o for o in open_orders if o["kind"] == "purchase"],
        "receivable": balances.get("sales", 0),
        "payable": balances.get("purchase", 0),
        "month_sales": month_sales,
        "work_orders": wos,
    }


def _plus_days(iso, n):
    from datetime import timedelta
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- export

def export_stock_csv(conn):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["料號", "品名", "分類", "規格", "單位", "庫存", "安全庫存", "成本", "庫存金額", "儲位"])
    for it in list_items(conn, {}):
        w.writerow([it["code"], it["name"], it["category"], it["spec"], it["unit"], it["stock"],
                    it["safety_stock"], it["cost"], round(it["stock"] * it["cost"], 2), it["location"]])
    # 加 BOM 讓 Excel 正確顯示中文
    return ("﻿" + buf.getvalue()).encode("utf-8")


def backup_bytes():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = connect()
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.close()
        src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


# ---------------------------------------------------------------- routing

ROUTES = [
    ("GET", r"/api/dashboard", lambda c, q, b: dashboard(c, q)),
    ("GET", r"/api/items", lambda c, q, b: list_items(c, q)),
    ("POST", r"/api/items", lambda c, q, b: create_item(c, b)),
    ("PUT", r"/api/items/(\d+)", lambda c, q, b, i: update_item(c, int(i), b)),
    ("DELETE", r"/api/items/(\d+)", lambda c, q, b, i: delete_item(c, int(i))),
    ("GET", r"/api/partners", lambda c, q, b: list_partners(c, q)),
    ("POST", r"/api/partners", lambda c, q, b: create_partner(c, b)),
    ("PUT", r"/api/partners/(\d+)", lambda c, q, b, i: update_partner(c, int(i), b)),
    ("DELETE", r"/api/partners/(\d+)", lambda c, q, b, i: delete_partner(c, int(i))),
    ("GET", r"/api/orders", lambda c, q, b: list_orders(c, q)),
    ("POST", r"/api/orders", lambda c, q, b: create_order(c, b)),
    ("GET", r"/api/orders/(\d+)", lambda c, q, b, i: get_order(c, int(i))),
    ("PUT", r"/api/orders/(\d+)", lambda c, q, b, i: update_order(c, int(i), b)),
    ("POST", r"/api/orders/(\d+)/deliver", lambda c, q, b, i: deliver_order(c, int(i), b)),
    ("POST", r"/api/orders/(\d+)/payments", lambda c, q, b, i: add_payment(c, int(i), b)),
    ("POST", r"/api/orders/(\d+)/cancel", lambda c, q, b, i: cancel_order(c, int(i))),
    ("GET", r"/api/workorders", lambda c, q, b: list_work_orders(c, q)),
    ("POST", r"/api/workorders", lambda c, q, b: create_work_order(c, b)),
    ("GET", r"/api/workorders/(\d+)", lambda c, q, b, i: get_work_order(c, int(i))),
    ("POST", r"/api/workorders/(\d+)/start", lambda c, q, b, i: start_work_order(c, int(i))),
    ("POST", r"/api/workorders/(\d+)/complete", lambda c, q, b, i: complete_work_order(c, int(i), b)),
    ("POST", r"/api/workorders/(\d+)/cancel", lambda c, q, b, i: cancel_work_order(c, int(i))),
    ("GET", r"/api/stock/moves", lambda c, q, b: list_moves(c, q)),
    ("POST", r"/api/stock/adjust", lambda c, q, b: adjust_stock(c, b)),
    ("POST", r"/api/import", lambda c, q, b: __import__("importer").run_import(c, b)),
]
ROUTES = [(m, re.compile("^" + p + "$"), fn) for m, p, fn in ROUTES]

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def handle_api(method, path, query, body):
    """回傳 (status, payload)。payload 為 dict/list。"""
    for m, pattern, fn in ROUTES:
        match = pattern.match(path)
        if m == method and match:
            conn = connect()
            try:
                with conn:  # 交易：出錯就整筆回滾，庫存不會只扣一半
                    return 200, fn(conn, query, body, *match.groups())
            except ApiError as e:
                return e.status, {"error": str(e)}
            finally:
                conn.close()
    return 404, {"error": "找不到這個功能"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CNC-ERP/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status, data, ctype="application/json; charset=utf-8", headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/export/stock.csv":
            conn = connect()
            try:
                data = export_stock_csv(conn)
            finally:
                conn.close()
            return self._send(200, data, "text/csv; charset=utf-8", {
                "Content-Disposition": f'attachment; filename="stock-{today()}.csv"'})
        if url.path == "/api/backup":
            return self._send(200, backup_bytes(), "application/octet-stream", {
                "Content-Disposition": f'attachment; filename="cnc-backup-{today()}.db"'})
        if url.path.startswith("/api/"):
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return self._send(400, {"error": "資料格式錯誤"})
            try:
                status, payload = handle_api(method, url.path, query, body)
            except Exception as e:  # 不讓單一錯誤把伺服器弄掛
                self.log_error("internal error: %r", e)
                status, payload = 500, {"error": "系統錯誤：" + str(e)}
            return self._send(status, payload)
        if method != "GET":
            return self._send(405, {"error": "method not allowed"})
        name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        with open(full, "rb") as f:
            self._send(200, f.read(), STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    ap = argparse.ArgumentParser(description="CNC 加工廠進銷存系統")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--lan", action="store_true", help="開放區網內其他電腦連線")
    ap.add_argument("--demo", action="store_true", help="資料庫是空的時候，放入示範資料")
    args = ap.parse_args()
    init_db()
    if args.demo:
        from demo_data import load_demo
        load_demo()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, args.port), Handler)
    print(f"CNC 進銷存系統已啟動：http://127.0.0.1:{args.port}  （資料庫：{DB_PATH}）")
    if args.lan:
        print("已開放區網連線，其他電腦請用這台電腦的 IP 位址加上 :%d 連線" % args.port)
    print("關閉此視窗即停止系統")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
