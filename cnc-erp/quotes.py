"""圖面報價與附件（圖面、照片、PDF、CAD 檔）。

流程：收到圖面（手機拍照 / 檔案）→ 建立詢價單並附上圖面 → 估價（材料、工時、架機、外包、利潤）
→ 報價（可列印）→ 成交後一鍵轉成銷貨訂單（沒有的零件自動建立，圖面一起帶過去）。

附件存在資料庫旁的 files/ 資料夾，資料庫只記錄路徑。
"""
import mimetypes
import os
import re
from datetime import datetime

import server
from server import ApiError, rows, num, today

QUOTE_STATUS = ("new", "quoted", "won", "lost")
OWNER_TYPES = ("quote", "item", "partner", "order", "workorder")
MAX_FILE = 60 * 1024 * 1024

LINE_FIELDS = ["item_id", "part_name", "drawing_no", "revision", "material", "finish", "qty",
               "material_cost", "cycle_min", "setup_min", "outsource_cost", "margin_pct", "unit_price",
               "lead_days", "note"]
NUM_FIELDS = {"qty", "material_cost", "cycle_min", "setup_min", "outsource_cost", "margin_pct",
              "unit_price", "lead_days"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    stored TEXT NOT NULL,
    mime TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    uploaded TEXT NOT NULL,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_type, owner_id);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    no TEXT NOT NULL UNIQUE,
    partner_id INTEGER REFERENCES partners(id),
    customer_name TEXT DEFAULT '',
    contact TEXT DEFAULT '',
    inquiry_date TEXT NOT NULL,
    reply_by TEXT DEFAULT '',
    valid_until TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    category TEXT DEFAULT '',
    note TEXT DEFAULT '',
    lost_reason TEXT DEFAULT '',
    sales_order_id INTEGER REFERENCES orders(id),
    quoted_date TEXT DEFAULT '',
    closed_date TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS quote_lines (
    id INTEGER PRIMARY KEY,
    quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES items(id),
    part_name TEXT DEFAULT '',
    drawing_no TEXT DEFAULT '',
    revision TEXT DEFAULT '',
    material TEXT DEFAULT '',
    finish TEXT DEFAULT '',
    qty REAL DEFAULT 1,
    material_cost REAL DEFAULT 0,
    cycle_min REAL DEFAULT 0,
    setup_min REAL DEFAULT 0,
    outsource_cost REAL DEFAULT 0,
    margin_pct REAL DEFAULT 20,
    unit_price REAL DEFAULT 0,
    lead_days REAL DEFAULT 0,
    note TEXT DEFAULT ''
);
"""


# ---------------------------------------------------------------- 附件

def files_dir():
    return os.path.join(os.path.dirname(os.path.abspath(server.DB_PATH)), "files")


def _safe_name(name):
    name = os.path.basename(name.replace("\\", "/")) or "file"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ") or "file"
    return name[-120:]


def save_file(conn, owner_type, owner_id, filename, data, note=""):
    if owner_type not in OWNER_TYPES:
        raise ApiError("附件類型錯誤")
    if not data:
        raise ApiError("檔案是空的")
    if len(data) > MAX_FILE:
        raise ApiError("檔案太大（上限 60MB）")
    filename = _safe_name(filename)
    sub = datetime.now().strftime("%Y-%m")
    folder = os.path.join(files_dir(), sub)
    os.makedirs(folder, exist_ok=True)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    cur = conn.execute(
        "INSERT INTO files (owner_type, owner_id, filename, stored, mime, size, uploaded, note) VALUES (?,?,?,?,?,?,?,?)",
        (owner_type, int(owner_id), filename, "", mime, len(data), datetime.now().strftime("%Y-%m-%d %H:%M"), note))
    stored = f"{sub}/{cur.lastrowid}_{filename}"
    with open(os.path.join(files_dir(), stored), "wb") as f:
        f.write(data)
    conn.execute("UPDATE files SET stored = ? WHERE id = ?", (stored, cur.lastrowid))
    return {"id": cur.lastrowid, "filename": filename, "mime": mime}


def list_files(conn, owner_type, owner_id):
    return rows(conn.execute("SELECT * FROM files WHERE owner_type = ? AND owner_id = ? ORDER BY id",
                             (owner_type, int(owner_id))))


def file_path(conn, fid):
    r = conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    if not r:
        raise ApiError("找不到檔案", 404)
    path = os.path.join(files_dir(), r["stored"])
    if not os.path.isfile(path):
        raise ApiError("檔案已不存在", 404)
    return dict(r), path


def delete_file(conn, fid):
    r, path = file_path(conn, fid)
    conn.execute("DELETE FROM files WHERE id = ?", (fid,))
    # 同一個實體檔可能被多處引用（報價轉訂單時會把圖面掛到零件上）
    if not conn.execute("SELECT 1 FROM files WHERE stored = ?", (r["stored"],)).fetchone():
        os.remove(path)
    return {"ok": True}


def link_files(conn, from_type, from_id, to_type, to_id):
    """把附件也掛到另一個對象（不複製實體檔）。"""
    have = {r["stored"] for r in list_files(conn, to_type, to_id)}
    for f in list_files(conn, from_type, from_id):
        if f["stored"] in have:
            continue
        conn.execute(
            "INSERT INTO files (owner_type, owner_id, filename, stored, mime, size, uploaded, note) VALUES (?,?,?,?,?,?,?,?)",
            (to_type, int(to_id), f["filename"], f["stored"], f["mime"], f["size"], f["uploaded"], f["note"]))


# ---------------------------------------------------------------- 報價

def hourly_rate(conn):
    return float(server.get_setting(conn, "hourly_rate", "800") or 800)


def suggest_price(line, rate):
    """建議單價 =（材料 + 加工工時 + 架機時間分攤 + 外包）×（1 + 利潤%）"""
    qty = max(line.get("qty") or 1, 1)
    machining = ((line.get("cycle_min") or 0) + (line.get("setup_min") or 0) / qty) / 60 * rate
    cost = (line.get("material_cost") or 0) + machining + (line.get("outsource_cost") or 0)
    return round(cost * (1 + (line.get("margin_pct") or 0) / 100), 1), round(cost, 1)


QUOTE_SQL = """
SELECT q.*, COALESCE(NULLIF(p.short_name, ''), p.name, q.customer_name) AS customer,
       (SELECT COUNT(*) FROM quote_lines l WHERE l.quote_id = q.id) AS lines,
       (SELECT COALESCE(SUM(qty * unit_price), 0) FROM quote_lines l WHERE l.quote_id = q.id) AS total,
       (SELECT GROUP_CONCAT(COALESCE(NULLIF(drawing_no, ''), part_name), '、') FROM quote_lines l WHERE l.quote_id = q.id) AS parts,
       (SELECT id FROM files f WHERE f.owner_type = 'quote' AND f.owner_id = q.id AND f.mime LIKE 'image/%' ORDER BY id LIMIT 1) AS thumb,
       (SELECT COUNT(*) FROM files f WHERE f.owner_type = 'quote' AND f.owner_id = q.id) AS files,
       o.no AS order_no
FROM quotes q LEFT JOIN partners p ON p.id = q.partner_id LEFT JOIN orders o ON o.id = q.sales_order_id
"""


def list_quotes(conn, q):
    where, args = [], []
    if q.get("status"):
        where.append("q.status = ?")
        args.append(q["status"])
    if q.get("category"):
        where.append("q.category = ?")
        args.append(q["category"])
    if q.get("partner_id"):
        where.append("q.partner_id = ?")
        args.append(int(q["partner_id"]))
    for w in (q.get("search") or "").split():
        where.append("""(q.no LIKE ? OR q.customer_name LIKE ? OR q.note LIKE ? OR q.category LIKE ?
                         OR p.name LIKE ? OR p.short_name LIKE ? OR EXISTS (SELECT 1 FROM quote_lines l
                         WHERE l.quote_id = q.id AND (l.part_name LIKE ? OR l.drawing_no LIKE ? OR l.material LIKE ?)))""")
        args += [f"%{w}%"] * 9
    sql = QUOTE_SQL + (" WHERE " + " AND ".join(where) if where else "")
    data = rows(conn.execute(sql + " ORDER BY q.inquiry_date DESC, q.id DESC LIMIT 500", args))
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM quotes GROUP BY status").fetchall())
    cats = [r[0] for r in conn.execute(
        "SELECT category FROM quotes WHERE category != '' GROUP BY category ORDER BY COUNT(*) DESC")]
    return {"rows": data, "counts": counts, "categories": cats}


def get_quote(conn, qid):
    r = conn.execute(QUOTE_SQL + " WHERE q.id = ?", (qid,)).fetchone()
    if not r:
        raise ApiError("找不到報價單", 404)
    quote = dict(r)
    rate = hourly_rate(conn)
    quote["lines"] = []
    for ln in rows(conn.execute("SELECT * FROM quote_lines WHERE quote_id = ? ORDER BY id", (qid,))):
        ln["suggested"], ln["cost"] = suggest_price(ln, rate)
        ln["past"] = _past_prices(conn, ln)
        quote["lines"].append(ln)
    quote["files"] = list_files(conn, "quote", qid)
    quote["hourly_rate"] = rate
    if quote["partner_id"]:
        p = conn.execute("SELECT * FROM partners WHERE id = ?", (quote["partner_id"],)).fetchone()
        quote["partner"] = dict(p) if p else None
    return quote


def _past_prices(conn, ln):
    """同圖號 / 同零件以前的報價與成交價，給估價參考。"""
    keys = [k for k in (ln.get("drawing_no"),) if k]
    code = None
    if ln.get("item_id"):
        it = conn.execute("SELECT code, drawing_no FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
        if it:
            code = it["code"]
            if it["drawing_no"]:
                keys.append(it["drawing_no"])
    out = []
    if code or keys:
        cond = " OR ".join(["item_code = ?"] * bool(code) + ["item_code IN (SELECT code FROM items WHERE drawing_no = ?)"] * len(keys))
        out += rows(conn.execute(
            f"""SELECT '成交' AS type, doc_date AS date, partner_name AS who, qty, unit_price FROM ({server.HISTORY_UNION})
                WHERE kind = 'sales' AND ({cond}) ORDER BY doc_date DESC LIMIT 5""", ([code] if code else []) + keys))
    if keys:
        out += rows(conn.execute(
            f"""SELECT CASE q.status WHEN 'won' THEN '報價(成交)' WHEN 'lost' THEN '報價(未成交)' ELSE '報價' END AS type,
                       COALESCE(NULLIF(q.quoted_date, ''), q.inquiry_date) AS date,
                       COALESCE(p.short_name, p.name, q.customer_name) AS who, l.qty, l.unit_price
                FROM quote_lines l JOIN quotes q ON q.id = l.quote_id LEFT JOIN partners p ON p.id = q.partner_id
                WHERE l.drawing_no IN ({','.join('?' * len(keys))}) AND l.id != ? AND l.unit_price > 0
                ORDER BY date DESC LIMIT 5""", keys + [ln.get("id") or 0]))
    return out[:8]


def _clean_lines(conn, lines):
    out = []
    for ln in lines or []:
        rec = {}
        for f in LINE_FIELDS:
            v = ln.get(f)
            if f in NUM_FIELDS:
                rec[f] = num(v or 0, f, 0)
            elif f == "item_id":
                rec[f] = int(v) if str(v or "").strip() else None
            else:
                rec[f] = str(v or "").strip()
        if not (rec["part_name"] or rec["drawing_no"] or rec["item_id"]):
            continue
        if rec["item_id"]:
            it = conn.execute("SELECT * FROM items WHERE id = ?", (rec["item_id"],)).fetchone()
            if not it:
                raise ApiError("明細中有不存在的品項")
            for f, src in (("part_name", "name"), ("drawing_no", "drawing_no"), ("revision", "revision"),
                           ("material", "material"), ("finish", "finish")):
                rec[f] = rec[f] or (it[src] or "")
        rec["qty"] = rec["qty"] or 1
        out.append(rec)
    return out


def _header(conn, body):
    pid = int(body["partner_id"]) if str(body.get("partner_id") or "").strip() else None
    name = (body.get("customer_name") or "").strip()
    if not pid and not name:
        raise ApiError("請選擇客戶，或輸入新客戶名稱")
    if pid and not conn.execute("SELECT 1 FROM partners WHERE id = ?", (pid,)).fetchone():
        raise ApiError("客戶不存在")
    return {"partner_id": pid, "customer_name": name, "contact": body.get("contact", ""),
            "inquiry_date": body.get("inquiry_date") or today(), "reply_by": body.get("reply_by", ""),
            "valid_until": body.get("valid_until", ""), "category": (body.get("category") or "").strip(),
            "note": body.get("note", "")}


def create_quote(conn, body):
    h = _header(conn, body)
    no = server.next_no(conn, "quotes", "QT")
    cur = conn.execute(
        f"INSERT INTO quotes (no, {', '.join(h)}) VALUES (?, {', '.join('?' * len(h))})", [no] + list(h.values()))
    _save_lines(conn, cur.lastrowid, _clean_lines(conn, body.get("lines")))
    return {"id": cur.lastrowid, "no": no}


def _save_lines(conn, qid, lines):
    conn.execute("DELETE FROM quote_lines WHERE quote_id = ?", (qid,))
    for ln in lines:
        conn.execute(f"INSERT INTO quote_lines (quote_id, {', '.join(LINE_FIELDS)}) VALUES (?, {', '.join('?' * len(LINE_FIELDS))})",
                     [qid] + [ln[f] for f in LINE_FIELDS])


def update_quote(conn, qid, body):
    q = get_quote(conn, qid)
    if q["status"] == "won":
        raise ApiError("已成交轉訂單的報價不能修改")
    h = _header(conn, {**q, **body})
    conn.execute(f"UPDATE quotes SET {', '.join(k + ' = ?' for k in h)} WHERE id = ?", list(h.values()) + [qid])
    if "lines" in body:
        _save_lines(conn, qid, _clean_lines(conn, body["lines"]))
    return {"ok": True}


def set_status(conn, qid, body):
    q = get_quote(conn, qid)
    status = body.get("status")
    if status not in ("new", "quoted", "lost"):
        raise ApiError("狀態錯誤")
    if q["status"] == "won":
        raise ApiError("已成交的報價不能改狀態")
    if status == "quoted" and not any(ln["unit_price"] for ln in q["lines"]):
        raise ApiError("還沒有填單價，不能標記為已報價")
    conn.execute("""UPDATE quotes SET status = ?, lost_reason = ?,
                    quoted_date = CASE WHEN ? = 'quoted' AND quoted_date = '' THEN ? ELSE quoted_date END,
                    closed_date = CASE WHEN ? = 'lost' THEN ? ELSE '' END WHERE id = ?""",
                 (status, body.get("lost_reason", "") if status == "lost" else "", status, today(), status, today(), qid))
    return {"ok": True}


def convert_to_order(conn, qid, body):
    """成交：建立（或沿用）客戶與零件，開銷貨訂單，圖面跟著掛到零件與訂單上。"""
    q = get_quote(conn, qid)
    if q["status"] == "won":
        raise ApiError("這張報價已經轉過訂單了")
    if not q["lines"]:
        raise ApiError("報價沒有明細")
    if any(not ln["unit_price"] for ln in q["lines"]):
        raise ApiError("有明細還沒有單價")
    pid = q["partner_id"]
    if not pid:
        existing = conn.execute("SELECT id FROM partners WHERE type = 'customer' AND name = ?",
                                (q["customer_name"],)).fetchone()
        pid = existing["id"] if existing else server.create_partner(conn, {
            "type": "customer", "name": q["customer_name"], "contact": q["contact"], "note": f"由報價 {q['no']} 建立"})["id"]
        conn.execute("UPDATE quotes SET partner_id = ? WHERE id = ?", (pid, qid))
    lines = []
    for ln in q["lines"]:
        item_id = ln["item_id"]
        if not item_id:
            code = (ln["drawing_no"] or "").strip() or f"{q['no']}-{len(lines) + 1}"
            found = conn.execute("SELECT id FROM items WHERE code = ? OR (drawing_no != '' AND drawing_no = ?)",
                                 (code, ln["drawing_no"] or "\0")).fetchone()
            if found:
                item_id = found["id"]
            else:
                item_id = server.create_item(conn, {
                    "code": code, "name": ln["part_name"] or code, "category": "成品", "unit": "個",
                    "drawing_no": ln["drawing_no"], "revision": ln["revision"], "material": ln["material"],
                    "finish": ln["finish"], "customer_id": pid, "cycle_min": ln["cycle_min"],
                    "price": ln["unit_price"], "note": f"由報價 {q['no']} 建立"})["id"]
            conn.execute("UPDATE quote_lines SET item_id = ? WHERE id = ?", (item_id, ln["id"]))
        link_files(conn, "quote", qid, "item", item_id)
        lines.append({"item_id": item_id, "qty": ln["qty"], "unit_price": ln["unit_price"]})
    lead = max([ln["lead_days"] for ln in q["lines"]] + [0])
    due = body.get("due_date") or (server._plus_days(today(), int(lead)) if lead else "")
    order = server.create_order(conn, {"kind": "sales", "partner_id": pid, "due_date": due,
                                       "customer_po": body.get("customer_po", ""),
                                       "note": f"報價 {q['no']}" + (f"　{q['note']}" if q["note"] else ""), "lines": lines})
    link_files(conn, "quote", qid, "order", order["id"])
    conn.execute("UPDATE quotes SET status = 'won', sales_order_id = ?, closed_date = ? WHERE id = ?",
                 (order["id"], today(), qid))
    return {"order_id": order["id"], "order_no": order["no"]}


def quote_stats(conn):
    """成交率等，給總覽用。"""
    since = server._plus_days(today(), -180)
    r = conn.execute("""SELECT SUM(status = 'won'), SUM(status = 'lost'), SUM(status = 'new'), SUM(status = 'quoted')
                        FROM quotes WHERE inquiry_date >= ?""", (since,)).fetchone()
    won, lost = r[0] or 0, r[1] or 0
    waiting = rows(conn.execute(QUOTE_SQL + " WHERE q.status = 'new' ORDER BY q.reply_by = '', q.reply_by, q.inquiry_date LIMIT 8"))
    return {"won": won, "lost": lost, "new": r[2] or 0, "quoted": r[3] or 0,
            "win_rate": round(won / (won + lost) * 100) if won + lost else None, "waiting": waiting}
