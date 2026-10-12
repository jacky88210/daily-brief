#!/usr/bin/env python3
"""CNC 加工廠進銷存系統（輕量版）

只需要 Python 3.7 以上，不用安裝任何套件。
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
-- 舊系統的歷史交易明細：只做查詢用，不影響庫存
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('purchase', 'sales')),
    doc_date TEXT DEFAULT '',
    doc_no TEXT DEFAULT '',
    partner_id INTEGER REFERENCES partners(id),
    partner_name TEXT DEFAULT '',
    item_code TEXT DEFAULT '',
    item_name TEXT DEFAULT '',
    spec TEXT DEFAULT '',
    unit TEXT DEFAULT '',
    qty REAL,
    unit_price REAL,
    amount REAL,
    note TEXT DEFAULT '',
    fingerprint TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_history_partner ON history(partner_id);
CREATE INDEX IF NOT EXISTS idx_history_item ON history(item_code);
-- 每次出貨 / 進貨一張單（可分批），單據列印用
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY,
    no TEXT NOT NULL UNIQUE,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    ship_date TEXT NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shipment_lines (
    id INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    order_line_id INTEGER NOT NULL REFERENCES order_lines(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL,
    unit_price REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ship_order ON shipments(order_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- 存取紀錄：登入成功 / 失敗、改密碼、下載備份…（機密資料要查得到誰在什麼時候做了什麼）
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, time TEXT NOT NULL, ip TEXT DEFAULT '',
                                  action TEXT NOT NULL, detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, created TEXT NOT NULL, last_seen TEXT NOT NULL, client TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_moves_item ON stock_moves(item_id);
CREATE INDEX IF NOT EXISTS idx_lines_order ON order_lines(order_id);
"""

ITEM_FIELDS = ["code", "name", "category", "spec", "unit", "safety_stock",
               "cost", "price", "location", "note",
               # CNC 加工用：客戶圖號、版次、材質、表面 / 熱處理、所屬客戶、單件加工時間（分鐘）
               "drawing_no", "revision", "material", "finish", "customer_id", "cycle_min"]
PARTNER_FIELDS = ["type", "name", "short_name", "grp", "contact", "phone", "fax", "email", "owner",
                  "tax_id", "address", "note",
                  # 經濟部商工登記查到的資料
                  "reg_status", "reg_capital", "reg_date", "reg_checked"]

# 舊版資料庫自動補上新欄位（ALTER TABLE ADD COLUMN），已有的資料不受影響
MIGRATIONS = {
    "items": [("drawing_no", "TEXT DEFAULT ''"), ("revision", "TEXT DEFAULT ''"), ("material", "TEXT DEFAULT ''"),
              ("finish", "TEXT DEFAULT ''"), ("customer_id", "INTEGER"), ("cycle_min", "REAL DEFAULT 0")],
    "partners": [("short_name", "TEXT DEFAULT ''"), ("grp", "TEXT DEFAULT ''"), ("fax", "TEXT DEFAULT ''"),
                 ("email", "TEXT DEFAULT ''"), ("owner", "TEXT DEFAULT ''"), ("reg_status", "TEXT DEFAULT ''"),
                 ("reg_capital", "TEXT DEFAULT ''"), ("reg_date", "TEXT DEFAULT ''"),
                 ("reg_checked", "TEXT DEFAULT ''")],
    # 營業稅：exclusive = 外加、inclusive = 內含、none = 免稅（舊資料維持不含稅，金額不會變）
    "orders": [("tax_type", "TEXT DEFAULT 'none'"), ("tax_rate", "REAL DEFAULT 5")],
    "sessions": [("user_id", "INTEGER")],
    "audit": [("username", "TEXT DEFAULT ''")],
}
TAX_TYPES = {"exclusive": "外加", "inclusive": "內含", "none": "免稅"}
# 含稅總額 = 小計 × 這個係數（內含與免稅的單價本身就是總額）
TAXF = "(CASE o.tax_type WHEN 'exclusive' THEN 1 + o.tax_rate / 100.0 ELSE 1 END)"
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
        import accounts
        import production
        import quotes
        conn.executescript(SCHEMA)
        conn.executescript(accounts.SCHEMA)
        conn.executescript(quotes.SCHEMA)
        conn.executescript(production.SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols:
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        production.ensure_ops(conn)
        accounts.migrate_single_password(conn)
        conn.commit()
    finally:
        conn.close()


def today():
    return date.today().isoformat()


# ---------------------------------------------------------------- 設定與登入

PUBLIC_SETTINGS = ["brand_name", "company_name", "company_address", "company_phone", "company_fax", "company_tax_id",
                   "company_email", "bank_info", "hourly_rate", "workdays", "quote_terms",
                   "tax_rate", "default_tax_type", "print_paper", "print_copies", "backup_dir", "backup_dir2", "backup_keep"]


def get_setting(conn, key, default=""):
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r and r[0] is not None else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def _read_version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "dev"


VERSION = _read_version()


def brand_name(conn):
    """畫面上方顯示的名稱：「系統顯示名稱」→ 公司名稱 → 預設。登入前也會顯示，所以只回傳名稱。"""
    return (get_setting(conn, "brand_name") or get_setting(conn, "company_name") or "CNC 進銷存").strip()


def system_info(conn):
    files_root = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "files")
    n_files = size_files = 0
    for root, _, names in os.walk(files_root):
        for name in names:
            n_files += 1
            size_files += os.path.getsize(os.path.join(root, name))
    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return {"version": VERSION, "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
            "db_path": os.path.abspath(DB_PATH), "db_size": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
            "files": n_files, "files_size": size_files, "program_dir": BASE_DIR,
            "counts": {k: count(t) for k, t in (("品項", "items"), ("客戶 / 廠商", "partners"), ("訂單", "orders"),
                                                ("報價", "quotes"), ("工單", "work_orders"), ("歷史紀錄", "history"))}}


def get_settings(conn, q=None):
    out = {k: get_setting(conn, k) for k in PUBLIC_SETTINGS}
    out["hourly_rate"] = out["hourly_rate"] or "800"
    out["workdays"] = out["workdays"] or "123456"
    out["tax_rate"] = out["tax_rate"] or "5"
    out["default_tax_type"] = out["default_tax_type"] or "exclusive"
    out["print_paper"] = out["print_paper"] or "A4"
    out["backup_keep"] = out["backup_keep"] or "30"
    try:
        out["backup_status"] = json.loads(get_setting(conn, "backup_status") or "{}")
    except ValueError:
        out["backup_status"] = {}
    out["db_path"] = os.path.abspath(DB_PATH)
    import accounts
    out["has_users"] = accounts.has_users(conn)
    return out


def save_settings(conn, body):
    for k in PUBLIC_SETTINGS:
        if k in body:
            if k == "hourly_rate":
                num(body[k] or 0, "工時費率", 0)
            set_setting(conn, k, str(body[k]).strip())
    return {"ok": True}


def _hash_password(password, salt=None):
    import hashlib
    import secrets
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


SESSION_DAYS = 30
LOCAL_IPS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
MAX_FAILS, LOCK_MINUTES = 5, 10
_fails = {}  # ip → [失敗時間]，連續輸錯鎖定用
import threading as _threading
CURRENT = _threading.local()  # 目前這個請求是哪個使用者（每個請求一個執行緒）


def current_user():
    return getattr(CURRENT, "user", None)


def audit(conn, ip, action, detail="", username=None):
    from datetime import datetime
    if username is None:
        u = current_user()
        username = u["display_name"] if u else ""
    conn.execute("INSERT INTO audit (time, ip, action, detail, username) VALUES (?,?,?,?,?)",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip or "", action, detail, username))
    conn.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id) - 5000 FROM audit)")  # 只留最近 5000 筆


def list_audit(conn, q):
    return rows(conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200"))


def login_locked(ip):
    import time
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < LOCK_MINUTES * 60]
    _fails[ip] = recent
    return len(recent) >= MAX_FAILS


def login_failed(ip):
    import time
    _fails.setdefault(ip, []).append(time.time())


def new_session(conn, client="", user_id=None):
    import secrets
    from datetime import datetime
    token = secrets.token_urlsafe(32)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("INSERT INTO sessions (token, created, last_seen, client, user_id) VALUES (?,?,?,?,?)",
                 (token, now, now, client, user_id))
    return token


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

LAST_DATE_SQL = """MAX(COALESCE((SELECT MAX(h.doc_date) FROM history h WHERE h.item_code = i.code), ''),
                        COALESCE((SELECT MAX(o.order_date) FROM order_lines l JOIN orders o ON o.id = l.order_id
                                  WHERE l.item_id = i.id AND o.status != 'cancelled'), ''))"""


def _item_where(q):
    where, args = ["i.active = 1"], []
    if q.get("category"):
        where.append("i.category = ?")
        args.append(q["category"])
    if q.get("customer_id"):
        where.append("i.customer_id = ?")
        args.append(int(q["customer_id"]))
    for word in (q.get("search") or "").split():
        where.append("(i.code LIKE ? OR i.name LIKE ? OR i.spec LIKE ? OR i.drawing_no LIKE ? OR i.material LIKE ?)")
        args += [f"%{word}%"] * 5
    return " AND ".join(where), args


def list_items(conn, q):
    """全部品項（下拉選單、開單用）。lite=1 只回傳必要欄位。"""
    where, args = _item_where(q)
    cols = ("i.id, i.code, i.name, i.spec, i.unit, i.category, i.price, i.cost, i.customer_id, i.drawing_no"
            if q.get("lite") else "i.*")
    sql = f"""SELECT {cols}, COALESCE((SELECT SUM(qty) FROM stock_moves m WHERE m.item_id = i.id), 0) AS stock
              FROM items i WHERE {where} ORDER BY i.category, i.code"""
    return rows(conn.execute(sql, args))


ITEM_SORTS = {"code": "i.code", "name": "i.name", "stock": "stock", "value": "stock * i.cost",
              "last": "last_date", "category": "i.category, i.code", "drawing": "i.drawing_no"}


def page_items(conn, q):
    """庫存品項頁：分頁、排序、篩選。"""
    where, args = _item_where(q)
    base = f"""SELECT i.*, p.name AS customer_name, p.short_name AS customer_short,
                      COALESCE((SELECT SUM(qty) FROM stock_moves m WHERE m.item_id = i.id), 0) AS stock,
                      {LAST_DATE_SQL} AS last_date
               FROM items i LEFT JOIN partners p ON p.id = i.customer_id WHERE {where}"""
    if q.get("low"):
        base = f"SELECT * FROM ({base}) i WHERE i.safety_stock > 0 AND i.stock < i.safety_stock"
    else:
        base = f"SELECT * FROM ({base}) i"
    order = ITEM_SORTS.get(q.get("sort") or "code", "i.code")
    direction = "DESC" if q.get("dir") == "desc" else "ASC"
    per = min(max(int(q.get("per") or 50), 10), 500)
    page = max(int(q.get("page") or 1), 1)
    total, value = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(stock * cost), 0) FROM ({base})", args).fetchone()
    data = rows(conn.execute(f"{base} ORDER BY {order} {direction}, i.code LIMIT ? OFFSET ?",
                             args + [per, (page - 1) * per]))
    return {"rows": data, "total": total, "value": value, "page": page, "per": per}


def item_detail(conn, item_id):
    it = conn.execute(
        """SELECT i.*, p.name AS customer_name, COALESCE((SELECT SUM(qty) FROM stock_moves m
               WHERE m.item_id = i.id), 0) AS stock FROM items i LEFT JOIN partners p ON p.id = i.customer_id
           WHERE i.id = ?""", (item_id,)).fetchone()
    if not it:
        raise ApiError("找不到品項", 404)
    it = dict(it)
    hist = list_history(conn, {"item_code": it["code"], "limit": 30})
    sub = f"SELECT * FROM ({HISTORY_UNION}) WHERE item_code = ?"
    buyers = rows(conn.execute(
        f"""SELECT partner_id, partner_name, kind, COUNT(*) AS times, SUM(qty) AS qty, MAX(doc_date) AS last_date,
                   (SELECT h2.unit_price FROM ({sub}) h2 WHERE h2.partner_name = h.partner_name
                        AND h2.kind = h.kind ORDER BY h2.doc_date DESC LIMIT 1) AS last_price
            FROM ({sub}) h GROUP BY partner_name, kind ORDER BY last_date DESC LIMIT 15""",
        (it["code"], it["code"])))
    open_lines = rows(conn.execute(
        """SELECT o.id AS order_id, o.no, o.kind, o.due_date, p.name AS partner_name, l.qty, l.delivered_qty, l.unit_price
           FROM order_lines l JOIN orders o ON o.id = l.order_id JOIN partners p ON p.id = o.partner_id
           WHERE l.item_id = ? AND o.status IN ('open', 'partial') ORDER BY o.due_date""", (item_id,)))
    wos = rows(conn.execute(WO_SQL + " WHERE w.item_id = ? AND w.status IN ('pending', 'in_progress')", (item_id,)))
    import production
    files = rows(conn.execute("SELECT * FROM files WHERE owner_type = 'item' AND owner_id = ? ORDER BY id", (item_id,)))
    return {"item": it, "history": hist, "partners": buyers, "open_lines": open_lines, "work_orders": wos,
            "files": files, "routes": production.get_routes(conn, item_id)}


def clean_item(body):
    data = {f: body.get(f, "") for f in ITEM_FIELDS}
    if not str(data["code"]).strip() or not str(data["name"]).strip():
        raise ApiError("料號與品名為必填")
    for f in ("safety_stock", "cost", "price", "cycle_min"):
        data[f] = num(data[f] or 0, f, 0)
    data["customer_id"] = int(data["customer_id"]) if str(data["customer_id"] or "").strip() else None
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
    existing = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not existing:
        raise ApiError("找不到品項", 404)
    # 沒送來的欄位保留原值（例如舊畫面、匯入只帶部分欄位）
    data = clean_item({f: body.get(f, existing[f]) for f in ITEM_FIELDS})
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
    data = rows(conn.execute(sql + " ORDER BY name", args))
    if q.get("stats"):
        year = today()[:4]
        last_year = str(int(year) - 1)
        stats = {r["partner_id"]: r for r in rows(conn.execute(
            f"""SELECT partner_id, MAX(doc_date) AS last_date, COUNT(DISTINCT doc_no) AS docs,
                       COALESCE(SUM(amount), 0) AS amount_total,
                       COALESCE(SUM(CASE WHEN doc_date LIKE ? THEN amount END), 0) AS amount_year,
                       COALESCE(SUM(CASE WHEN doc_date LIKE ? THEN amount END), 0) AS amount_last_year
                FROM ({HISTORY_UNION}) WHERE partner_id IS NOT NULL GROUP BY partner_id""",
            (year + "%", last_year + "%")))}
        for p in data:
            st = stats.get(p["id"], {})
            for k in ("last_date", "docs", "amount_total", "amount_year", "amount_last_year"):
                p[k] = st.get(k) or (0 if k != "last_date" else "")
    return data


def batch_partners(conn, body):
    """多選後一次改分類或改成客戶 / 供應商。"""
    ids = [int(i) for i in body.get("ids") or []]
    if not ids:
        raise ApiError("請先勾選")
    changes = body.get("set") or {}
    if "type" in changes and changes["type"] not in ("customer", "supplier"):
        raise ApiError("類型必須是客戶或供應商")
    for col in ("type", "grp"):
        if col in changes:
            conn.executemany(f"UPDATE partners SET {col} = ? WHERE id = ?",
                             [(str(changes[col]).strip(), i) for i in ids])
    return {"ok": True, "count": len(ids)}


def clean_partner(body):
    data = {f: (body.get(f) or "") for f in PARTNER_FIELDS}
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
    existing = conn.execute("SELECT * FROM partners WHERE id = ?", (pid,)).fetchone()
    if not existing:
        raise ApiError("找不到客戶 / 廠商", 404)
    data = clean_partner({f: body.get(f, existing[f]) for f in PARTNER_FIELDS})
    conn.execute(f"UPDATE partners SET {','.join(f + '=?' for f in PARTNER_FIELDS)} WHERE id = ?",
                 [data[f] for f in PARTNER_FIELDS] + [pid])
    return {"ok": True}


def delete_partner(conn, pid):
    conn.execute("UPDATE partners SET active = 0 WHERE id = ?", (pid,))
    return {"ok": True}


# ---------------------------------------------------------------- orders

ORDER_SUMMARY_SQL = """
SELECT o.*, p.name AS partner_name,
       COALESCE((SELECT SUM(qty * unit_price) FROM order_lines WHERE order_id = o.id), 0) AS subtotal,
       ROUND(COALESCE((SELECT SUM(qty * unit_price) FROM order_lines WHERE order_id = o.id), 0) * """ + TAXF + """) AS total,
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
    o.update(tax_breakdown(o["subtotal"], o["tax_type"], o["tax_rate"]))
    o["shipments"] = rows(conn.execute(
        """SELECT s.*, COALESCE((SELECT SUM(qty * unit_price) FROM shipment_lines WHERE shipment_id = s.id), 0) AS subtotal
           FROM shipments s WHERE s.order_id = ? ORDER BY s.id""", (oid,)))
    return o


def tax_breakdown(subtotal, tax_type, rate):
    """回傳 {tax_label, net, tax, total}：未稅金額、稅額、含稅總額（四捨五入到元）。"""
    rate = (rate or 0) / 100
    r = lambda x: int(x + 0.5) if x >= 0 else -int(-x + 0.5)  # 四捨五入（不用 Python 的銀行家捨入）
    if tax_type == "exclusive":
        tax = r(subtotal * rate)
        net, total = subtotal, subtotal + tax
    elif tax_type == "inclusive":
        total = r(subtotal)
        net = r(subtotal / (1 + rate))
        tax = total - net
    else:
        net, tax, total = subtotal, 0, subtotal
    return {"tax_label": TAX_TYPES.get(tax_type, "免稅"), "net": net, "tax": tax, "total": total}


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
    tax_type = body.get("tax_type") or get_setting(conn, "default_tax_type", "exclusive") or "exclusive"
    if tax_type not in TAX_TYPES:
        raise ApiError("稅別錯誤")
    tax_rate = num(body.get("tax_rate") if body.get("tax_rate") not in (None, "") else (get_setting(conn, "tax_rate", "5") or 5),
                   "稅率", 0)
    return (int(body["partner_id"]), body.get("order_date") or today(),
            body.get("due_date", ""), body.get("customer_po", ""), body.get("note", ""), tax_type, tax_rate)


def create_order(conn, body):
    kind = body.get("kind")
    if kind not in ORDER_PREFIX:
        raise ApiError("單據類型錯誤")
    header = save_order_header(conn, body, kind)
    lines = clean_lines(conn, body.get("lines"))
    no = next_no(conn, "orders", ORDER_PREFIX[kind])
    cur = conn.execute(
        """INSERT INTO orders (kind, no, partner_id, order_date, due_date, customer_po, note, tax_type, tax_rate)
           VALUES (?,?,?,?,?,?,?,?,?)""", (kind, no) + header)
    conn.executemany("INSERT INTO order_lines (order_id, item_id, qty, unit_price) VALUES (?,?,?,?)",
                     [(cur.lastrowid,) + ln for ln in lines])
    return {"id": cur.lastrowid, "no": no}


def update_order(conn, oid, body):
    o = get_order(conn, oid)
    if o["status"] == "cancelled":
        raise ApiError("已作廢的單據不能修改")
    header = save_order_header(conn, {"tax_type": o["tax_type"], "tax_rate": o["tax_rate"], **body}, o["kind"])
    conn.execute("UPDATE orders SET partner_id=?, order_date=?, due_date=?, customer_po=?, note=?, tax_type=?, tax_rate=? WHERE id=?",
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
    ship_no = next_no(conn, "shipments", "RC" if o["kind"] == "purchase" else "SH")
    ship_id = conn.execute("INSERT INTO shipments (no, order_id, ship_date, note) VALUES (?,?,?,?)",
                           (ship_no, oid, body.get("ship_date") or today(), body.get("note", ""))).lastrowid
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
        add_move(conn, ln["item_id"], sign * qty, kind, o["no"], ship_no)
        conn.execute("INSERT INTO shipment_lines (shipment_id, order_line_id, item_id, qty, unit_price) VALUES (?,?,?,?,?)",
                     (ship_id, ln["id"], ln["item_id"], qty, ln["unit_price"]))
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
    return {"ok": True, "shipment_id": ship_id, "shipment_no": ship_no}


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
SELECT w.*, i.code, i.name, i.unit, i.drawing_no, i.revision, i.material, i.finish, o.no AS sales_no, p.name AS customer
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
    import production
    w["ops"] = production.wo_ops(conn, wid)
    w["files"] = rows(conn.execute(
        """SELECT * FROM files WHERE (owner_type = 'item' AND owner_id = ?) OR (owner_type = 'workorder' AND owner_id = ?)
           ORDER BY id""", (w["item_id"], wid)))
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
    import production
    production.create_ops(conn, cur.lastrowid, body.get("ops"))
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


# ---------------------------------------------------------------- 交易歷史（舊系統 + 本系統）

# 本系統的訂單明細與舊系統匯入的歷史，整理成同一種格式一起查
HISTORY_UNION = """
SELECT '本系統' AS source, o.kind, o.order_date AS doc_date, o.no AS doc_no, o.id AS order_id,
       o.partner_id, p.name AS partner_name, i.code AS item_code, i.name AS item_name, i.spec, i.unit,
       l.qty, l.unit_price, l.qty * l.unit_price AS amount, o.note
FROM order_lines l JOIN orders o ON o.id = l.order_id JOIN partners p ON p.id = o.partner_id
JOIN items i ON i.id = l.item_id WHERE o.status != 'cancelled'
UNION ALL
SELECT '舊系統', kind, doc_date, doc_no, NULL, partner_id, partner_name, item_code, item_name, spec, unit,
       qty, unit_price, COALESCE(amount, qty * unit_price), note
FROM history
"""


def _history_filter(conn, q):
    where, args = [], []
    if q.get("kind"):
        where.append("kind = ?")
        args.append(q["kind"])
    if q.get("partner_id"):
        p = conn.execute("SELECT name FROM partners WHERE id = ?", (int(q["partner_id"]),)).fetchone()
        where.append("(partner_id = ? OR partner_name = ?)")
        args += [int(q["partner_id"]), p["name"] if p else ""]
    if q.get("item_code"):
        where.append("item_code = ?")
        args.append(q["item_code"])
    if q.get("search"):
        for word in q["search"].split():
            where.append("(partner_name LIKE ? OR item_code LIKE ? OR item_name LIKE ? OR spec LIKE ? "
                         "OR doc_no LIKE ? OR note LIKE ?)")
            args += [f"%{word}%"] * 6
    if q.get("date_from"):
        where.append("doc_date >= ?")
        args.append(q["date_from"])
    if q.get("date_to"):
        where.append("doc_date <= ?")
        args.append(q["date_to"])
    return (" WHERE " + " AND ".join(where)) if where else "", args


def list_history(conn, q):
    where, args = _history_filter(conn, q)
    limit = min(int(q.get("limit") or 500), 5000)
    data = rows(conn.execute(
        f"SELECT * FROM ({HISTORY_UNION}){where} ORDER BY doc_date DESC, doc_no DESC LIMIT ?", args + [limit]))
    total = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM ({HISTORY_UNION}){where}",
                         args).fetchone()
    return {"rows": data, "count": total[0], "amount": total[1]}


def partner_summary(conn, pid):
    p = conn.execute("SELECT * FROM partners WHERE id = ?", (pid,)).fetchone()
    if not p:
        raise ApiError("找不到客戶 / 廠商", 404)
    q = {"partner_id": pid}
    where, args = _history_filter(conn, q)
    sub = f"SELECT * FROM ({HISTORY_UNION}){where}"
    stats = dict(conn.execute(
        f"""SELECT COUNT(DISTINCT doc_no) AS docs, MIN(NULLIF(doc_date, '')) AS first_date,
                   MAX(doc_date) AS last_date, COALESCE(SUM(amount), 0) AS amount FROM ({sub})""",
        args).fetchone())
    stats["by_year"] = rows(conn.execute(
        f"""SELECT SUBSTR(doc_date, 1, 4) AS year, COALESCE(SUM(amount), 0) AS amount,
                   COUNT(DISTINCT doc_no) AS docs FROM ({sub}) WHERE doc_date != ''
            GROUP BY year ORDER BY year DESC""", args))
    stats["top_items"] = rows(conn.execute(
        f"""SELECT item_code, item_name, MAX(spec) AS spec, COUNT(*) AS times, SUM(qty) AS qty,
                   COALESCE(SUM(amount), 0) AS amount, MAX(doc_date) AS last_date,
                   (SELECT h2.unit_price FROM ({sub}) h2 WHERE h2.item_code = h.item_code
                        AND h2.item_name = h.item_name ORDER BY h2.doc_date DESC LIMIT 1) AS last_price
            FROM ({sub}) h GROUP BY item_code, item_name ORDER BY times DESC, amount DESC LIMIT 15""",
        args * 2))
    return {"partner": dict(p), "stats": stats, "history": list_history(conn, dict(q, limit=300))}


def last_prices(conn, q):
    """開單時參考：這個品項之前賣給（或跟誰買）的價格，先列同一個客戶，再列其他人。"""
    item = conn.execute("SELECT code FROM items WHERE id = ?", (int(q.get("item_id") or 0),)).fetchone()
    if not item:
        return []
    kind = q.get("kind") or "sales"
    pid = int(q.get("partner_id") or 0)
    pname = ""
    if pid:
        r = conn.execute("SELECT name FROM partners WHERE id = ?", (pid,)).fetchone()
        pname = r["name"] if r else ""
    return rows(conn.execute(
        f"""SELECT source, doc_date, doc_no, partner_name, qty, unit_price,
                   (partner_id = ? OR partner_name = ?) AS same_partner
            FROM ({HISTORY_UNION}) WHERE kind = ? AND item_code = ? AND unit_price IS NOT NULL
            ORDER BY same_partner DESC, doc_date DESC LIMIT 5""",
        (pid, pname, kind, item["code"])))


# ---------------------------------------------------------------- 公司登記查詢（經濟部商工登記公示資料開放 API）

GCIS_API = "https://data.gcis.nat.gov.tw/od/data/api/"
GCIS_COMPANY = "5F64D864-61CB-4D0D-8AD9-492047CC1EA6"   # 公司登記基本資料-應用一（依統編）
GCIS_BUSINESS = "7E6AFA72-AD6A-46D3-8681-ED77951D912D"  # 商業登記基本資料-應用一（行號，依統編）
FINDBIZ_URL = "https://findbiz.nat.gov.tw/fts/query/QueryBar/queryInit.do"


def _gcis_get(guid, filt):
    import urllib.parse
    import urllib.request
    url = GCIS_API + guid + "?" + urllib.parse.urlencode(
        {"$format": "json", "$filter": filt, "$skip": 0, "$top": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CNC-ERP"})
    with urllib.request.urlopen(req, timeout=10) as res:
        body = res.read().decode("utf-8-sig").strip()
    if not body:
        return None
    data = json.loads(body)
    return data[0] if isinstance(data, list) and data else None


def _roc_date(s):
    s = str(s or "").strip()
    if re.fullmatch(r"\d{7}", s):
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    return s


def parse_gcis(rec):
    """把公司或商業登記的回傳整理成統一格式。欄位名稱兩種都試。"""
    if not rec:
        return None
    pick = lambda *keys: next((str(rec[k]).strip() for k in keys if rec.get(k) not in (None, "")), "")
    capital = pick("Capital_Stock_Amount", "Paid_In_Capital_Amount", "Business_Register_Funds")
    if capital.replace(",", "").isdigit():
        capital = f"{int(capital.replace(',', '')):,}"
    return {
        "name": pick("Company_Name", "Business_Name"),
        "owner": pick("Responsible_Name"),
        "address": pick("Company_Location", "Business_Address"),
        "capital": capital,
        "setup_date": _roc_date(pick("Company_Setup_Date", "Business_Setup_Approve_Date")),
        "status": pick("Company_Status_Desc", "Business_Current_Status_Desc"),
    }


def company_lookup(conn, q):
    tax_id = re.sub(r"\D", "", q.get("tax_id") or "")
    if len(tax_id) != 8:
        raise ApiError("需要 8 碼統一編號才能查詢")
    errors = []
    for guid, filt, kind in ((GCIS_COMPANY, f"Business_Accounting_NO eq {tax_id}", "公司"),
                             (GCIS_BUSINESS, f"President_No eq {tax_id}", "商號")):
        try:
            found = parse_gcis(_gcis_get(guid, filt))
        except Exception as e:  # 沒網路、憑證、對方系統維護…
            errors.append(f"{kind}：{e}")
            continue
        if found and found["name"]:
            found.update({"found": True, "kind": kind, "tax_id": tax_id})
            return found
    return {"found": False, "tax_id": tax_id, "errors": errors, "manual_url": FINDBIZ_URL}


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
                      ROUND(COALESCE((SELECT SUM(delivered_qty * unit_price) FROM order_lines
                                WHERE order_id = o.id), 0) * """ + TAXF + """) AS delivered,
                      COALESCE((SELECT SUM(amount) FROM payments WHERE order_id = o.id), 0) AS paid
               FROM orders o WHERE o.status != 'cancelled')
           GROUP BY kind""").fetchall())
    month_sales = conn.execute(
        """SELECT COALESCE(SUM(-m.qty * (SELECT l.unit_price FROM order_lines l
                                          WHERE l.order_id = o.id AND l.item_id = m.item_id LIMIT 1)), 0)
           FROM stock_moves m JOIN orders o ON o.no = m.ref
           WHERE m.kind = '出貨' AND m.move_date LIKE ?""", (month + "%",)).fetchone()[0]
    wos = rows(conn.execute(WO_SQL + " WHERE w.status IN ('pending', 'in_progress') ORDER BY w.due_date"))
    for w in wos:
        cyc = conn.execute("SELECT cycle_min FROM items WHERE id = ?", (w["item_id"],)).fetchone()[0] or 0
        w["est_hours"] = round(cyc * w["qty"] / 60, 1) if cyc else None
    extra = dashboard_trends(conn, t)
    extra["backup_alerts"] = backup_alerts(conn)
    return {
        **extra,
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


def backup_alerts(conn):
    """給總覽用：沒設備份、只設一處、失敗、超過 2 天沒成功 → 提醒。"""
    dirs = [d for d in (get_setting(conn, "backup_dir"), get_setting(conn, "backup_dir2")) if d.strip()]
    try:
        status = json.loads(get_setting(conn, "backup_status") or "{}")
    except ValueError:
        status = {}
    if not dirs:
        return ["還沒有設定自動備份。客戶資料很重要，請到「設定」設定兩個備份位置（例：NAS + 外接硬碟）。"]
    alerts = []
    if len(dirs) == 1:
        alerts.append("只設定了一個備份位置，建議再設定第二個（例：外接硬碟），NAS 故障時才有另一份。")
    limit = _plus_days(today(), -2)
    for d in dirs:
        st = status.get(d.strip().strip('"'))
        if not st:
            alerts.append(f"{d}：還沒有備份紀錄（系統啟動後幾分鐘內會自動備份）。")
        elif not st.get("ok"):
            alerts.append(f"{d}：最近一次備份失敗（{st.get('time', '')}）：{st.get('error', '')}")
        elif st.get("time", "")[:10] < limit:
            alerts.append(f"{d}：已經超過 2 天沒有成功備份（最後一次 {st.get('time')}）。")
    return alerts


def dashboard_trends(conn, t):
    """總覽的經營數字：月銷售趨勢、今年 vs 去年、前十大客戶、久未下單的老客戶、機台負荷、常回單零件。
    銷售金額包含舊系統匯入的紀錄與本系統的訂單。"""
    year, month = int(t[:4]), int(t[5:7])
    sales = f"SELECT * FROM ({HISTORY_UNION}) WHERE kind = 'sales' AND doc_date != ''"
    months = []
    for k in range(11, -1, -1):
        y, m = year, month - k
        while m <= 0:
            y, m = y - 1, m + 12
        months.append(f"{y:04d}-{m:02d}")
    by_month = dict(conn.execute(
        f"SELECT SUBSTR(doc_date, 1, 7), COALESCE(SUM(amount), 0) FROM ({sales}) WHERE doc_date >= ? GROUP BY 1",
        (months[0] + "-01",)).fetchall())
    last_year_same = dict(conn.execute(
        f"SELECT SUBSTR(doc_date, 1, 7), COALESCE(SUM(amount), 0) FROM ({sales}) WHERE doc_date >= ? AND doc_date < ? GROUP BY 1",
        (f"{int(months[0][:4]) - 1}{months[0][4:]}-01", months[0] + "-01")).fetchall())
    ytd = conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM ({sales}) WHERE doc_date >= ? AND doc_date <= ?",
                       (f"{year}-01-01", t)).fetchone()[0]
    last_ytd = conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM ({sales}) WHERE doc_date >= ? AND doc_date <= ?",
                            (f"{year - 1}-01-01", f"{year - 1}{t[4:]}")).fetchone()[0]
    since = _plus_days(t, -365)
    top = rows(conn.execute(
        f"""SELECT s.partner_id, COALESCE(NULLIF(p.short_name, ''), s.partner_name) AS name,
                   SUM(s.amount) AS amount, COUNT(DISTINCT s.doc_no) AS docs
            FROM ({sales}) s LEFT JOIN partners p ON p.id = s.partner_id
            WHERE s.doc_date >= ? GROUP BY COALESCE(s.partner_id, s.partner_name) ORDER BY amount DESC LIMIT 10""",
        (since,)))
    total_12m = conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM ({sales}) WHERE doc_date >= ?",
                             (since,)).fetchone()[0]
    # 以前常來（至少 3 張單）、但超過 120 天沒下單的客戶 → 該打電話關心了
    dormant = rows(conn.execute(
        f"""SELECT s.partner_id, COALESCE(NULLIF(p.short_name, ''), s.partner_name) AS name, p.phone,
                   MAX(s.doc_date) AS last_date, COUNT(DISTINCT s.doc_no) AS docs,
                   SUM(CASE WHEN s.doc_date >= ? THEN s.amount ELSE 0 END) AS amount_3y
            FROM ({sales}) s LEFT JOIN partners p ON p.id = s.partner_id
            GROUP BY COALESCE(s.partner_id, s.partner_name)
            HAVING docs >= 3 AND last_date < ? AND last_date >= ?
            ORDER BY amount_3y DESC LIMIT 10""",
        (_plus_days(t, -365 * 3), _plus_days(t, -120), _plus_days(t, -365 * 3))))
    # 近兩年最常回單的零件（適合先備料）
    repeat = rows(conn.execute(
        f"""SELECT item_code, MAX(item_name) AS item_name, COUNT(DISTINCT doc_no) AS times, SUM(qty) AS qty,
                   MAX(doc_date) AS last_date, COUNT(DISTINCT partner_name) AS customers
            FROM ({sales}) WHERE doc_date >= ? AND item_code != ''
            GROUP BY item_code HAVING times >= 3 ORDER BY times DESC LIMIT 10""",
        (_plus_days(t, -730),)))
    import production
    import quotes
    sch = production.schedule(conn)
    machines = [{"id": m["id"], "machine": m["name"], "orders": len(m["queue"]), "hours": m["load_hours"],
                 "free_date": m["free_date"], "late": m["late"],
                 "no_cycle": sum(1 for o in m["queue"] if not o["cycle_min"])}
                for m in sch["machines"] if m["queue"]]
    if sch["unassigned"]:
        machines.append({"id": None, "machine": "（未排機台）", "orders": len(sch["unassigned"]),
                         "hours": round(sum(o["remaining_hours"] for o in sch["unassigned"]), 1),
                         "free_date": "", "late": 0, "no_cycle": 0})
    return {
        "months": [{"month": m, "amount": by_month.get(m, 0),
                    "last_year": last_year_same.get(f"{int(m[:4]) - 1}{m[4:]}", 0)} for m in months],
        "ytd": ytd, "last_ytd": last_ytd,
        "top_customers": top, "total_12m": total_12m,
        "dormant": dormant, "repeat_parts": repeat, "machines": machines,
        "late_work_orders": [w for w in sch["work_orders"] if w["late"]],
        "outsource": sch["outsource"], "quotes": quotes.quote_stats(conn),
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
    """備份：資料庫 + 圖面附件（files 資料夾），打包成一個 zip。"""
    import zipfile
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = connect()
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.close()
        src.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, "cnc.db")
            files_root = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "files")
            for root, _, names in os.walk(files_root):
                for name in names:
                    full = os.path.join(root, name)
                    z.write(full, os.path.join("files", os.path.relpath(full, files_root)), zipfile.ZIP_STORED)
        return buf.getvalue()
    finally:
        os.remove(tmp)


def verify_backup(data):
    """確認備份 zip 能用：壓縮檔沒壞、裡面的資料庫能開、資料庫完整性檢查通過。"""
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        bad = z.testzip()
        if bad:
            raise OSError(f"備份檔內容損毀：{bad}")
        db = z.read("cnc.db")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(db)
        c = sqlite3.connect(tmp)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.execute("SELECT COUNT(*) FROM items").fetchone()
        finally:
            c.close()
        if ok != "ok":
            raise OSError("備份裡的資料庫檢查不通過：" + ok)
    finally:
        os.remove(tmp)


def backup_to_dir(force=False):
    """自動備份到「備份位置 1、2」（例：NAS 資料夾 + 另一顆硬碟）。每天一份、寫完驗證、保留最近 N 份。
    回傳 {資料夾: {"ok": bool, "time": ..., "path"/"error": ...}}。"""
    from datetime import datetime
    conn = connect()
    try:
        targets = [get_setting(conn, k).strip().strip('"') for k in ("backup_dir", "backup_dir2")]
        keep = int(float(get_setting(conn, "backup_keep") or 30))
        try:
            status = json.loads(get_setting(conn, "backup_status") or "{}")
        except ValueError:
            status = {}
    finally:
        conn.close()
    targets = [t for t in targets if t]
    status = {k: v for k, v in status.items() if k in targets}
    previous = {k: dict(v) for k, v in status.items()}
    name = f"cnc-backup-{today()}.zip"
    data = None
    for target in targets:
        path = os.path.join(target, name)
        now = f"{datetime.now():%Y-%m-%d %H:%M}"
        try:
            if not os.path.isdir(target):
                raise OSError(f"找不到資料夾（NAS 或外接硬碟是否有開、有接上？）")
            if not force and os.path.exists(path) and status.get(target, {}).get("ok"):
                continue
            if data is None:
                data = backup_bytes()
                verify_backup(data)
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            with open(tmp, "rb") as f:  # 讀回來比對，確認真的寫進去了
                if f.read() != data:
                    raise OSError("寫入後讀回的內容不一致")
            os.replace(tmp, path)
            old = sorted(n for n in os.listdir(target) if re.fullmatch(r"cnc-backup-\d{4}-\d{2}-\d{2}\.zip", n))
            for n in (old[:-keep] if keep > 0 else []):
                os.remove(os.path.join(target, n))
            status[target] = {"ok": True, "time": now, "path": path, "size": len(data)}
        except Exception as e:  # 備份失敗不能讓系統停掉，記下來顯示在畫面上
            status[target] = {"ok": False, "time": now, "error": str(e)}
    conn = connect()
    try:
        with conn:
            set_setting(conn, "backup_status", json.dumps(status, ensure_ascii=False))
            for t, st in status.items():
                was_ok = previous.get(t, {}).get("ok", True)
                if not st["ok"] and was_ok:  # 只在「由正常變失敗」時記一筆，避免 NAS 沒開時每 30 分鐘記一次
                    audit(conn, "system", "自動備份失敗", f"{t}：{st['error']}")
                elif st["ok"] and not was_ok:
                    audit(conn, "system", "自動備份恢復正常", t)
    finally:
        conn.close()
    return status


def run_backup_now(conn, body):
    conn.commit()
    status = backup_to_dir(force=True)
    if not status:
        raise ApiError("還沒有設定備份位置")
    return {"status": status}


def start_backup_thread():
    """背景每 30 分鐘檢查一次，當天還沒備份就備份。"""
    import threading
    import time

    def loop():
        while True:
            try:
                backup_to_dir()
            except Exception as e:
                sys.stderr.write(f"auto backup error: {e!r}\n")
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True, name="auto-backup").start()


def data_dir():
    return os.path.dirname(os.path.abspath(DB_PATH))


def save_snapshot(keep=10):
    """更新程式前的快照：存在資料夾內 pre-update\，保留最近 10 份。會先驗證能還原。"""
    from datetime import datetime
    data = backup_bytes()
    verify_backup(data)
    folder = os.path.join(data_dir(), "pre-update")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"cnc-before-update-{datetime.now():%Y%m%d-%H%M%S}.zip")
    with open(path, "wb") as f:
        f.write(data)
    old = sorted(n for n in os.listdir(folder) if n.startswith("cnc-before-update-") and n.endswith(".zip"))
    for n in old[:-keep]:
        os.remove(os.path.join(folder, n))
    return path


def write_pid():
    """記下目前執行中系統的程序編號，update.bat 用它來重啟系統。"""
    try:
        with open(os.path.join(data_dir(), "server.pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def on_network_share(path):
    """資料庫放在網路磁碟（\\\\NAS\\... 或對應的磁碟機）上很容易損毀。"""
    p = os.path.abspath(path)
    if p.startswith("\\\\") or p.startswith("//"):
        return True
    if os.name == "nt" and len(p) > 1 and p[1] == ":":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(p[:2] + "\\") == 4  # DRIVE_REMOTE
        except Exception:
            return False
    return False


# ---------------------------------------------------------------- 凌越備份匯入

def lingyue_upload_path():
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "lingyue_upload.001")


def _lingyue_raw(body):
    path = (body.get("path") or "").strip().strip('"')
    target = path or lingyue_upload_path()
    if not os.path.isfile(target):
        raise ApiError(f"找不到檔案：{path}" if path else "請先選擇凌越的備份檔")
    with open(target, "rb") as f:
        return f.read()


def lingyue_preview(conn, body):
    import lingyue
    return lingyue.preview(_lingyue_raw(body), body.get("prefix"))


def lingyue_import(conn, body):
    import lingyue
    return lingyue.run(conn, _lingyue_raw(body), body)


# ---------------------------------------------------------------- routing

ROUTES = [
    ("GET", r"/api/dashboard", lambda c, q, b: dashboard(c, q)),
    ("GET", r"/api/items", lambda c, q, b: list_items(c, q)),
    ("POST", r"/api/items", lambda c, q, b: create_item(c, b)),
    ("GET", r"/api/items/page", lambda c, q, b: page_items(c, q)),
    ("GET", r"/api/items/(\d+)", lambda c, q, b, i: item_detail(c, int(i))),
    ("PUT", r"/api/items/(\d+)", lambda c, q, b, i: update_item(c, int(i), b)),
    ("DELETE", r"/api/items/(\d+)", lambda c, q, b, i: delete_item(c, int(i))),
    ("GET", r"/api/partners", lambda c, q, b: list_partners(c, q)),
    ("POST", r"/api/partners", lambda c, q, b: create_partner(c, b)),
    ("POST", r"/api/partners/batch", lambda c, q, b: batch_partners(c, b)),
    ("GET", r"/api/company-lookup", lambda c, q, b: company_lookup(c, q)),
    ("GET", r"/api/partners/(\d+)/summary", lambda c, q, b, i: partner_summary(c, int(i))),
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
    ("GET", r"/api/history", lambda c, q, b: list_history(c, q)),
    ("GET", r"/api/last-prices", lambda c, q, b: last_prices(c, q)),
    ("GET", r"/api/settings", lambda c, q, b: get_settings(c, q)),
    ("GET", r"/api/system", lambda c, q, b: system_info(c)),
    ("GET", r"/api/shipments", lambda c, q, b: _m("documents").list_shipments(c, q)),
    ("GET", r"/api/shipments/(\d+)", lambda c, q, b, i: _m("documents").get_shipment(c, int(i))),
    ("GET", r"/api/statement", lambda c, q, b: _m("documents").statement(c, q)),
    ("GET", r"/api/statement/partners", lambda c, q, b: _m("documents").statement_partners(c, q)),
    ("PUT", r"/api/settings", lambda c, q, b: save_settings(c, b)),
    ("POST", r"/api/backup/run", lambda c, q, b: run_backup_now(c, b)),
    ("GET", r"/api/audit", lambda c, q, b: list_audit(c, q)),
    ("GET", r"/api/users", lambda c, q, b: _m("accounts").list_users(c, q)),
    ("POST", r"/api/users", lambda c, q, b: _m("accounts").create_user(c, b)),
    ("PUT", r"/api/users/(\d+)", lambda c, q, b, i: _m("accounts").update_user(c, int(i), b)),
    ("GET", r"/api/me", lambda c, q, b: _m("accounts").public(current_user()) or {"local": True}),
    ("POST", r"/api/me/password", lambda c, q, b: _m("accounts").change_own_password(c, current_user(), b)),
    ("GET", r"/api/files", lambda c, q, b: _m("quotes").list_files(c, q.get("owner_type"), q.get("owner_id") or 0)),
    ("DELETE", r"/api/files/(\d+)", lambda c, q, b, i: _m("quotes").delete_file(c, int(i))),
    ("GET", r"/api/quotes", lambda c, q, b: _m("quotes").list_quotes(c, q)),
    ("POST", r"/api/quotes", lambda c, q, b: _m("quotes").create_quote(c, b)),
    ("GET", r"/api/quotes/(\d+)", lambda c, q, b, i: _m("quotes").get_quote(c, int(i))),
    ("PUT", r"/api/quotes/(\d+)", lambda c, q, b, i: _m("quotes").update_quote(c, int(i), b)),
    ("POST", r"/api/quotes/(\d+)/status", lambda c, q, b, i: _m("quotes").set_status(c, int(i), b)),
    ("POST", r"/api/quotes/(\d+)/convert", lambda c, q, b, i: _m("quotes").convert_to_order(c, int(i), b)),
    ("GET", r"/api/machines", lambda c, q, b: _m("production").list_machines(c, q)),
    ("POST", r"/api/machines", lambda c, q, b: _m("production").save_machine(c, b)),
    ("PUT", r"/api/machines/(\d+)", lambda c, q, b, i: _m("production").save_machine(c, b, int(i))),
    ("DELETE", r"/api/machines/(\d+)", lambda c, q, b, i: _m("production").delete_machine(c, int(i))),
    ("GET", r"/api/items/(\d+)/routes", lambda c, q, b, i: _m("production").get_routes(c, int(i))),
    ("PUT", r"/api/items/(\d+)/routes", lambda c, q, b, i: _m("production").save_routes(c, int(i), b)),
    ("PUT", r"/api/workorders/(\d+)/ops", lambda c, q, b, i: {"count": _m("production").create_ops(c, int(i), b.get("ops"))}),
    ("GET", r"/api/schedule", lambda c, q, b: _m("production").schedule(c, q)),
    ("GET", r"/api/floor", lambda c, q, b: _m("production").floor_queue(c, q)),
    ("POST", r"/api/ops/(\d+)/start", lambda c, q, b, i: _m("production").start_op(c, int(i))),
    ("POST", r"/api/ops/(\d+)/report", lambda c, q, b, i: _m("production").report_op(c, int(i), b)),
    ("POST", r"/api/ops/(\d+)/send", lambda c, q, b, i: _m("production").send_out(c, int(i), b)),
    ("POST", r"/api/ops/(\d+)/move", lambda c, q, b, i: _m("production").move_op(c, int(i), b)),
    ("PUT", r"/api/ops/(\d+)", lambda c, q, b, i: _m("production").update_op(c, int(i), b)),
    ("POST", r"/api/lingyue/preview", lambda c, q, b: lingyue_preview(c, b)),
    ("POST", r"/api/lingyue/import", lambda c, q, b: lingyue_import(c, b)),
    ("POST", r"/api/import", lambda c, q, b: __import__("importer").run_import(c, b)),
]
ROUTES = [(m, re.compile("^" + p + "$"), fn) for m, p, fn in ROUTES]


def _m(name):
    import importlib
    return importlib.import_module(name)

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/manifest+json", ".png": "image/png"}


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
        headers = dict(headers or {})
        self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ApiError("檔案太大")
        data = bytearray()
        while len(data) < length:
            chunk = self.rfile.read(min(length - len(data), 1 << 20))
            if not chunk:
                break
            data += chunk
        return bytes(data)

    def _json_body(self):
        try:
            return json.loads(self._read_body(10000).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def _auth(self, url, method):
        """回傳 None 表示可以繼續；否則回傳要送出的 (status, payload, headers)。

        規則：
        - 有帳號之後：所有 /api 都要登入（每台裝置登入一次，30 天有效），並依角色限制功能。
        - 還沒有任何帳號：只有系統電腦本身（127.0.0.1）能用；其他電腦 / 手機只能先建立管理員帳號，看不到任何資料。
        - 同一個 IP 連續輸錯 5 次密碼，鎖 10 分鐘。"""
        import accounts
        ip = self.client_address[0]
        local = ip in LOCAL_IPS
        CURRENT.user = None
        cookie = lambda tok: f"cnc_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
        conn = connect()
        try:
            with conn:
                required = accounts.has_users(conn)
                token = self._cookie("cnc_session")
                user = accounts.session_user(conn, token) if required else None
                CURRENT.user = user
                if url.path == "/api/auth/status":
                    return 200, {"required": required, "logged_in": bool(user) if required else local,
                                 "setup_needed": not required and not local, "local": local,
                                 "user": accounts.public(user), "brand": brand_name(conn), "version": VERSION}, {}
                if url.path == "/api/auth/setup" and method == "POST":
                    if login_locked(ip):
                        return 429, {"error": f"嘗試太多次，請 {LOCK_MINUTES} 分鐘後再試"}, {}
                    try:
                        r = accounts.setup_first_admin(conn, self._json_body())
                    except ApiError as e:
                        login_failed(ip)
                        return e.status, {"error": str(e)}, {}
                    audit(conn, ip, "建立第一個管理員帳號", "", "")
                    return 200, {"ok": True}, {"Set-Cookie": cookie(new_session(conn, ip, r["id"]))}
                if url.path == "/api/auth/login" and method == "POST":
                    if login_locked(ip):
                        return 429, {"error": f"密碼錯誤太多次，請 {LOCK_MINUTES} 分鐘後再試"}, {}
                    body = self._json_body()
                    u = accounts.authenticate(conn, body.get("username"), body.get("password")) if required else None
                    if not u:
                        login_failed(ip)
                        audit(conn, ip, "登入失敗", f"帳號：{(body.get('username') or '')[:30]}", "")
                        return 403, {"error": "帳號或密碼不正確"}, {}
                    _fails.pop(ip, None)
                    audit(conn, ip, "登入", self.headers.get("User-Agent", "")[:120], u["display_name"])
                    return 200, {"ok": True, "user": accounts.public(u)}, {"Set-Cookie": cookie(new_session(conn, ip, u["id"]))}
                if url.path == "/api/auth/logout":
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                    if user:
                        audit(conn, ip, "登出")
                    return 200, {"ok": True}, {"Set-Cookie": "cnc_session=; Path=/; Max-Age=0"}
                if required:
                    if not user:
                        return 401, {"error": "請先登入", "login": True}, {}
                    if not accounts.allowed(user["role"], method, url.path):
                        return 403, {"error": f"您的帳號（{accounts.ROLES.get(user['role'])}）沒有使用這個功能的權限"}, {}
                elif not local:
                    return 403, {"error": "這套系統還沒有建立帳號，為了保護客戶資料，請先建立管理員帳號才能從其他裝置使用。",
                                 "setup": True}, {}
                if url.path == "/api/backup":
                    audit(conn, ip, "下載備份檔")
        finally:
            conn.close()
        return None

    def _audit_write(self, method, path, status):
        """修改資料的動作（新增訂單、出貨、改報價…）記到存取紀錄，查得到是誰做的。"""
        import accounts
        if method == "GET" or status != 200:
            return
        label = accounts.write_label(method, path)
        if not label:
            return
        conn = connect()
        try:
            with conn:
                audit(conn, self.client_address[0], label, path)
        finally:
            conn.close()

    def _dispatch(self, method):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.startswith("/api/"):
            denied = self._auth(url, method)
            if denied:
                status, payload, headers = denied
                return self._send(status, payload, headers=headers)
        if url.path == "/api/files/upload" and method == "POST":
            import quotes
            try:
                data = self._read_body(quotes.MAX_FILE)
                conn = connect()
                try:
                    with conn:
                        r = quotes.save_file(conn, query.get("owner_type", ""), int(query.get("owner_id") or 0),
                                             query.get("name") or "file", data, query.get("note", ""))
                finally:
                    conn.close()
                self._audit_write(method, url.path, 200)
                return self._send(200, r)
            except ApiError as e:
                return self._send(e.status, {"error": str(e)})
        m = re.match(r"^/api/files/(\d+)/raw$", url.path)
        if m and method == "GET":
            import quotes
            from urllib.parse import quote as urlquote
            conn = connect()
            try:
                rec, path = quotes.file_path(conn, int(m.group(1)))
            except ApiError as e:
                return self._send(e.status, {"error": str(e)})
            finally:
                conn.close()
            with open(path, "rb") as f:
                data = f.read()
            disp = "attachment" if query.get("download") else "inline"
            return self._send(200, data, rec["mime"] or "application/octet-stream", {
                "Content-Disposition": f"{disp}; filename*=UTF-8''{urlquote(rec['filename'])}",
                "Cache-Control": "private, max-age=86400"})
        if url.path == "/api/export/stock.csv":
            conn = connect()
            try:
                data = export_stock_csv(conn)
            finally:
                conn.close()
            return self._send(200, data, "text/csv; charset=utf-8", {
                "Content-Disposition": f'attachment; filename="stock-{today()}.csv"'})
        if url.path == "/api/backup":
            return self._send(200, backup_bytes(), "application/zip", {
                "Content-Disposition": f'attachment; filename="cnc-backup-{today()}.zip"'})
        if url.path == "/api/lingyue/upload" and method == "POST":
            # 備份檔可能有幾十 MB，直接收原始檔案存起來，再回傳預覽
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return self._send(400, {"error": "請選擇檔案"})
            tmp = lingyue_upload_path() + ".part"
            with open(tmp, "wb") as f:
                left = length
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(tmp, lingyue_upload_path())
            try:
                status, payload = handle_api("POST", "/api/lingyue/preview", query, {"prefix": query.get("prefix")})
            except Exception as e:
                self.log_error("internal error: %r", e)
                status, payload = 500, {"error": "系統錯誤：" + str(e)}
            return self._send(status, payload)
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
            self._audit_write(method, url.path, status)
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
    ap.add_argument("--snapshot", action="store_true", help="更新程式前用：在資料夾內存一份快照備份（pre-update）")
    ap.add_argument("--reset-admin", "--reset-password", action="store_true", dest="reset_admin",
                    help="忘記管理員密碼時用：產生一組臨時密碼")
    args = ap.parse_args()
    init_db()
    if args.snapshot:
        path = save_snapshot()
        print(f"已備份：{path}")
        return
    if args.reset_admin:
        import accounts
        conn = connect()
        with conn:
            username, temp = accounts.reset_admin(conn)
            audit(conn, "本機指令", "重設管理員密碼（--reset-admin）", username, "")
        conn.close()
        print(f"管理員帳號：{username}")
        print(f"臨時密碼：{temp}")
        print("請用這組密碼登入後，馬上到「設定 → 我的帳號」改成自己的密碼。")
        return
    if args.demo:
        from demo_data import load_demo
        load_demo()
    if on_network_share(DB_PATH):
        print("【警告】資料庫放在網路磁碟（NAS）上！多人同時使用時資料可能損毀。")
        print("        請把系統放在本機硬碟執行，並在「設定」開啟「自動備份到 NAS」；")
        print("        或依說明把整套系統裝在 NAS 上（Container Manager / Docker）。")
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    try:
        srv = ThreadingHTTPServer((host, args.port), Handler)
    except OSError:
        # 通常是系統已經在背景執行（開機自動啟動），start.bat 只要幫忙開瀏覽器就好
        print(f"連接埠 {args.port} 已被使用：系統可能已經在執行中。")
        sys.exit(3)
    print(f"CNC 進銷存系統已啟動：http://127.0.0.1:{args.port}  （資料庫：{DB_PATH}）")
    if args.lan:
        print("已開放區網連線，其他電腦請用這台電腦的 IP 位址加上 :%d 連線" % args.port)
        import accounts
        conn = connect()
        has = accounts.has_users(conn)
        conn.close()
        if not has:
            print("【注意】還沒有建立帳號：其他電腦 / 手機連線時會被要求先建立管理員帳號，建立前看不到任何資料。")
    start_backup_thread()
    write_pid()
    print("關閉此視窗即停止系統")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
