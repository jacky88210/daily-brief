"""單據列印用的資料：出貨單 / 進貨單、月結對帳單。

畫面上的列印（版面、紙張大小、一式多聯）在 static/app.js 的「單據列印」區塊。
"""
import server
from server import ApiError, rows, today


def _partner(conn, pid):
    p = conn.execute("SELECT * FROM partners WHERE id = ?", (pid,)).fetchone()
    return dict(p) if p else {}


def get_shipment(conn, sid):
    s = conn.execute(
        """SELECT s.*, o.no AS order_no, o.kind, o.partner_id, o.customer_po, o.tax_type, o.tax_rate,
                  o.order_date, o.due_date
           FROM shipments s JOIN orders o ON o.id = s.order_id WHERE s.id = ?""", (sid,)).fetchone()
    if not s:
        raise ApiError("找不到出貨單", 404)
    s = dict(s)
    s["lines"] = rows(conn.execute(
        """SELECT sl.qty, sl.unit_price, i.code, i.name, i.spec, i.unit, i.drawing_no, i.revision, i.material,
                  ol.qty AS order_qty, ol.delivered_qty
           FROM shipment_lines sl JOIN items i ON i.id = sl.item_id JOIN order_lines ol ON ol.id = sl.order_line_id
           WHERE sl.shipment_id = ? ORDER BY sl.id""", (sid,)))
    s["subtotal"] = sum(l["qty"] * l["unit_price"] for l in s["lines"])
    s.update(server.tax_breakdown(s["subtotal"], s["tax_type"], s["tax_rate"]))
    s["partner"] = _partner(conn, s["partner_id"])
    return s


def _shipment_totals(conn, pid, kind, date_from=None, date_to=None):
    """某客戶 / 廠商在期間內每張出貨單的未稅、稅額、含稅金額。"""
    where, args = ["o.partner_id = ?", "o.kind = ?", "o.status != 'cancelled'"], [pid, kind]
    if date_from:
        where.append("s.ship_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("s.ship_date <= ?")
        args.append(date_to)
    out = []
    for s in rows(conn.execute(
            f"""SELECT s.id, s.no, s.ship_date, o.no AS order_no, o.customer_po, o.tax_type, o.tax_rate,
                       COALESCE((SELECT SUM(qty * unit_price) FROM shipment_lines WHERE shipment_id = s.id), 0) AS subtotal
                FROM shipments s JOIN orders o ON o.id = s.order_id
                WHERE {' AND '.join(where)} ORDER BY s.ship_date, s.id""", args)):
        s.update(server.tax_breakdown(s["subtotal"], s["tax_type"], s["tax_rate"]))
        out.append(s)
    return out


def _payments(conn, pid, kind, date_from=None, date_to=None):
    where, args = ["o.partner_id = ?", "o.kind = ?"], [pid, kind]
    if date_from:
        where.append("pay_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("pay_date <= ?")
        args.append(date_to)
    return rows(conn.execute(
        f"""SELECT p.pay_date, p.amount, p.note, o.no AS order_no FROM payments p JOIN orders o ON o.id = p.order_id
            WHERE {' AND '.join(where)} ORDER BY p.pay_date, p.id""", args))


def statement(conn, q):
    """月結對帳單：上期未收 + 本期出貨（含稅）− 本期收款 = 本期應收。"""
    pid = int(q.get("partner_id") or 0)
    partner = _partner(conn, pid)
    if not partner:
        raise ApiError("請選擇客戶或廠商")
    kind = "sales" if partner["type"] == "customer" else "purchase"
    d1 = q.get("date_from") or today()[:8] + "01"
    d2 = q.get("date_to") or today()
    if d1 > d2:
        raise ApiError("起日不能晚於迄日")
    before = server._plus_days(d1, -1)
    prev_ship = sum(s["total"] for s in _shipment_totals(conn, pid, kind, date_to=before))
    prev_paid = sum(p["amount"] for p in _payments(conn, pid, kind, date_to=before))
    ships = _shipment_totals(conn, pid, kind, d1, d2)
    for s in ships:
        s["lines"] = rows(conn.execute(
            """SELECT sl.qty, sl.unit_price, i.code, i.name, i.spec, i.unit, i.drawing_no
               FROM shipment_lines sl JOIN items i ON i.id = sl.item_id WHERE sl.shipment_id = ? ORDER BY sl.id""",
            (s["id"],)))
    pays = _payments(conn, pid, kind, d1, d2)
    period = {"net": sum(s["net"] for s in ships), "tax": sum(s["tax"] for s in ships),
              "total": sum(s["total"] for s in ships), "paid": sum(p["amount"] for p in pays)}
    prev_balance = prev_ship - prev_paid
    return {"partner": partner, "kind": kind, "date_from": d1, "date_to": d2, "shipments": ships, "payments": pays,
            "prev_balance": prev_balance, "period": period,
            "balance": prev_balance + period["total"] - period["paid"]}


def statement_partners(conn, q):
    """期間內有出貨或還有未收款的客戶（批次列印對帳單用）。"""
    ptype = q.get("type") or "customer"
    kind = "sales" if ptype == "customer" else "purchase"
    d1 = q.get("date_from") or today()[:8] + "01"
    d2 = q.get("date_to") or today()
    out = []
    for p in rows(conn.execute("SELECT * FROM partners WHERE type = ? AND active = 1 ORDER BY name", (ptype,))):
        st = statement(conn, {"partner_id": p["id"], "date_from": d1, "date_to": d2})
        if st["shipments"] or abs(st["balance"]) >= 1:
            out.append({"id": p["id"], "name": p["name"], "short_name": p["short_name"],
                        "shipments": len(st["shipments"]), "period_total": st["period"]["total"],
                        "paid": st["period"]["paid"], "balance": st["balance"]})
    return out


def list_shipments(conn, q):
    """出貨單 / 進貨單查詢（補印用）。"""
    where, args = ["o.kind = ?"], [q.get("kind") or "sales"]
    if q.get("date_from"):
        where.append("s.ship_date >= ?")
        args.append(q["date_from"])
    if q.get("date_to"):
        where.append("s.ship_date <= ?")
        args.append(q["date_to"])
    for w in (q.get("search") or "").split():
        where.append("""(s.no LIKE ? OR o.no LIKE ? OR o.customer_po LIKE ? OR p.name LIKE ? OR p.short_name LIKE ?
                         OR EXISTS (SELECT 1 FROM shipment_lines sl JOIN items i ON i.id = sl.item_id
                                    WHERE sl.shipment_id = s.id AND (i.code LIKE ? OR i.name LIKE ? OR i.drawing_no LIKE ?)))""")
        args += [f"%{w}%"] * 8
    out = rows(conn.execute(
        f"""SELECT s.id, s.no, s.ship_date, o.no AS order_no, o.customer_po, o.tax_type, o.tax_rate,
                   COALESCE(NULLIF(p.short_name, ''), p.name) AS partner,
                   (SELECT GROUP_CONCAT(i.name, '、') FROM shipment_lines sl JOIN items i ON i.id = sl.item_id
                    WHERE sl.shipment_id = s.id) AS items,
                   COALESCE((SELECT SUM(qty * unit_price) FROM shipment_lines WHERE shipment_id = s.id), 0) AS subtotal
            FROM shipments s JOIN orders o ON o.id = s.order_id JOIN partners p ON p.id = o.partner_id
            WHERE {' AND '.join(where)} ORDER BY s.ship_date DESC, s.id DESC LIMIT 300""", args))
    for s in out:
        s.update(server.tax_breakdown(s["subtotal"], s["tax_type"], s["tax_rate"]))
    return out
