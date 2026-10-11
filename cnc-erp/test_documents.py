"""單據：稅別、分批出貨單、月結對帳單：python -m unittest test_documents"""
import os
import sqlite3
import tempfile
import unittest

import documents
import server


class DocTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.init_db(self.path)
        self.conn = server.connect(self.path)
        c = self.conn
        self.cus = server.create_partner(c, dict(type="customer", name="永豐機械"))["id"]
        self.item = server.create_item(c, dict(code="SH-1", name="軸", opening_stock=1000, drawing_no="YF-1"))["id"]

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def order(self, qty=100, price=105, **kw):
        return server.create_order(self.conn, dict(kind="sales", partner_id=self.cus,
                                                   lines=[dict(item_id=self.item, qty=qty, unit_price=price)], **kw))["id"]

    def ship(self, oid, qty, date):
        line = server.get_order(self.conn, oid)["lines"][0]
        return server.deliver_order(self.conn, oid, dict(ship_date=date, lines=[dict(line_id=line["id"], qty=qty)]))

    def test_tax_types(self):
        ex = server.get_order(self.conn, self.order())
        self.assertEqual((ex["tax_label"], ex["net"], ex["tax"], ex["total"]), ("外加", 10500, 525, 11025))
        self.assertEqual(ex["total"], 11025)  # 清單上的金額也是含稅
        inc = server.get_order(self.conn, self.order(tax_type="inclusive"))
        self.assertEqual((inc["net"], inc["tax"], inc["total"]), (10000, 500, 10500))
        free = server.get_order(self.conn, self.order(tax_type="none"))
        self.assertEqual((free["tax"], free["total"]), (0, 10500))
        server.set_setting(self.conn, "default_tax_type", "none")
        self.assertEqual(server.get_order(self.conn, self.order())["tax_type"], "none")
        with self.assertRaises(server.ApiError):
            self.order(tax_type="bogus")

    def test_each_delivery_is_a_numbered_shipment(self):
        oid = self.order()
        a = self.ship(oid, 30, "2026-09-25")
        b = self.ship(oid, 70, "2026-10-03")
        self.assertTrue(a["shipment_no"].startswith("SH-") and a["shipment_no"] != b["shipment_no"])
        s = documents.get_shipment(self.conn, b["shipment_id"])
        self.assertEqual((s["ship_date"], s["lines"][0]["qty"], s["lines"][0]["drawing_no"]), ("2026-10-03", 70, "YF-1"))
        self.assertEqual((s["net"], s["tax"], s["total"]), (7350, 368, 7718))
        self.assertEqual(len(server.get_order(self.conn, oid)["shipments"]), 2)
        # 庫存異動的備註記著出貨單號，查得到是哪一張
        move = self.conn.execute("SELECT note FROM stock_moves WHERE kind = '出貨' ORDER BY id DESC").fetchone()
        self.assertEqual(move[0], b["shipment_no"])

    def test_purchase_receipt_number(self):
        sup = server.create_partner(self.conn, dict(type="supplier", name="材料行"))["id"]
        po = server.create_order(self.conn, dict(kind="purchase", partner_id=sup, lines=[dict(item_id=self.item, qty=5, unit_price=10)]))
        r = server.deliver_order(self.conn, po["id"], {})
        self.assertTrue(r["shipment_no"].startswith("RC-"))

    def test_monthly_statement(self):
        o1 = self.order(qty=100, price=100)           # 外加 5%
        self.ship(o1, 40, "2026-09-20")               # 上期：4,200
        server.add_payment(self.conn, o1, dict(amount=2000, pay_date="2026-09-28"))
        self.ship(o1, 60, "2026-10-05")               # 本期：6,300
        o2 = self.order(qty=10, price=210, tax_type="inclusive")
        self.ship(o2, 10, "2026-10-20")               # 本期：2,100（內含）
        server.add_payment(self.conn, o1, dict(amount=2200, pay_date="2026-10-10"))
        st = documents.statement(self.conn, dict(partner_id=self.cus, date_from="2026-10-01", date_to="2026-10-31"))
        self.assertEqual(st["prev_balance"], 4200 - 2000)
        self.assertEqual([s["total"] for s in st["shipments"]], [6300, 2100])
        self.assertEqual(st["period"], {"net": 6000 + 2000, "tax": 300 + 100, "total": 8400, "paid": 2200})
        self.assertEqual(st["balance"], 2200 + 8400 - 2200)
        self.assertEqual(st["shipments"][0]["lines"][0]["code"], "SH-1")
        lst = documents.statement_partners(self.conn, dict(date_from="2026-10-01", date_to="2026-10-31"))
        self.assertEqual([(p["name"], p["balance"]) for p in lst], [("永豐機械", 8400)])
        with self.assertRaises(server.ApiError):
            documents.statement(self.conn, dict(partner_id=self.cus, date_from="2026-11-01", date_to="2026-10-01"))

    def test_existing_orders_stay_untaxed_after_upgrade(self):
        fd, old = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            raw = sqlite3.connect(old)
            raw.executescript("""
                CREATE TABLE partners (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, contact TEXT DEFAULT '',
                    phone TEXT DEFAULT '', tax_id TEXT DEFAULT '', address TEXT DEFAULT '', note TEXT DEFAULT '', active INTEGER DEFAULT 1);
                CREATE TABLE orders (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, no TEXT NOT NULL UNIQUE, partner_id INTEGER NOT NULL,
                    order_date TEXT NOT NULL, due_date TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
                    customer_po TEXT DEFAULT '', note TEXT DEFAULT '');
                INSERT INTO partners (type, name) VALUES ('customer', 'A');
                INSERT INTO orders (kind, no, partner_id, order_date) VALUES ('sales', 'SO-OLD', 1, '2026-01-01');""")
            raw.close()
            server.init_db(old)
            c = server.connect(old)
            self.assertEqual(c.execute("SELECT tax_type FROM orders").fetchone()[0], "none")
            c.close()
        finally:
            os.remove(old)


if __name__ == "__main__":
    unittest.main()
