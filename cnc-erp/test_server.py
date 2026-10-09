"""後端邏輯測試：python -m unittest test_server"""
import os
import tempfile
import unittest

import server


class ErpTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.init_db(self.path)
        self.conn = server.connect(self.path)
        c = self.conn
        self.bar = server.create_item(c, dict(code="RM-1", name="鋁棒", category="原料", unit="支",
                                              safety_stock=5, opening_stock=10))["id"]
        self.part = server.create_item(c, dict(code="FG-1", name="軸", category="成品", price=100))["id"]
        self.sup = server.create_partner(c, dict(type="supplier", name="材料行"))["id"]
        self.cus = server.create_partner(c, dict(type="customer", name="客戶A"))["id"]

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def stock(self, item_id):
        return server.stock_of(self.conn, item_id)

    def test_purchase_receive_partial_then_full(self):
        o = server.create_order(self.conn, dict(kind="purchase", partner_id=self.sup,
                                                lines=[dict(item_id=self.bar, qty=20, unit_price=800)]))
        line = server.get_order(self.conn, o["id"])["lines"][0]
        server.deliver_order(self.conn, o["id"], dict(lines=[dict(line_id=line["id"], qty=5)]))
        self.assertEqual(self.stock(self.bar), 15)
        self.assertEqual(server.get_order(self.conn, o["id"])["status"], "partial")
        server.deliver_order(self.conn, o["id"], {})
        self.assertEqual(self.stock(self.bar), 30)
        self.assertEqual(server.get_order(self.conn, o["id"])["status"], "done")
        # 進貨會更新成本
        cost = self.conn.execute("SELECT cost FROM items WHERE id=?", (self.bar,)).fetchone()[0]
        self.assertEqual(cost, 800)

    def test_cannot_over_deliver(self):
        o = server.create_order(self.conn, dict(kind="purchase", partner_id=self.sup,
                                                lines=[dict(item_id=self.bar, qty=2)]))
        line = server.get_order(self.conn, o["id"])["lines"][0]
        with self.assertRaises(server.ApiError):
            server.deliver_order(self.conn, o["id"], dict(lines=[dict(line_id=line["id"], qty=3)]))

    def test_sales_ship_requires_stock(self):
        o = server.create_order(self.conn, dict(kind="sales", partner_id=self.cus,
                                                lines=[dict(item_id=self.part, qty=10, unit_price=100)]))
        with self.assertRaises(server.ApiError) as ctx:
            server.deliver_order(self.conn, o["id"], {})
        self.assertIn("庫存不足", str(ctx.exception))

    def test_partner_type_checked(self):
        with self.assertRaises(server.ApiError):
            server.create_order(self.conn, dict(kind="sales", partner_id=self.sup,
                                                lines=[dict(item_id=self.part, qty=1)]))

    def test_work_order_consumes_material_and_produces(self):
        w = server.create_work_order(self.conn, dict(item_id=self.part, qty=100, materials=[
            dict(item_id=self.bar, qty_per_unit=0.05)]))
        server.start_work_order(self.conn, w["id"])
        server.complete_work_order(self.conn, w["id"], dict(good_qty=95, scrap_qty=5))
        self.assertAlmostEqual(self.stock(self.bar), 10 - 100 * 0.05)
        self.assertEqual(self.stock(self.part), 95)
        with self.assertRaises(server.ApiError):
            server.complete_work_order(self.conn, w["id"], {})

    def test_work_order_blocked_when_material_short(self):
        w = server.create_work_order(self.conn, dict(item_id=self.part, qty=100, materials=[
            dict(item_id=self.bar, qty_per_unit=1)]))
        with self.assertRaises(server.ApiError):
            server.complete_work_order(self.conn, w["id"], {})

    def test_api_rolls_back_on_error(self):
        # 第二行出貨失敗時，第一行不能被扣掉
        old = server.DB_PATH
        server.DB_PATH = self.path
        try:
            self.conn.commit()
            server.create_item(self.conn, dict(code="FG-2", name="蓋", opening_stock=0))
            fg2 = self.conn.execute("SELECT id FROM items WHERE code='FG-2'").fetchone()[0]
            server.add_move(self.conn, self.part, 10, "期初")
            o = server.create_order(self.conn, dict(kind="sales", partner_id=self.cus, lines=[
                dict(item_id=self.part, qty=5), dict(item_id=fg2, qty=5)]))
            self.conn.commit()
            status, payload = server.handle_api("POST", f"/api/orders/{o['id']}/deliver", {}, {})
            self.assertEqual(status, 400)
            self.assertEqual(self.stock(self.part), 10)
        finally:
            server.DB_PATH = old

    def test_stock_count_and_dashboard(self):
        r = server.adjust_stock(self.conn, dict(item_id=self.bar, actual=3))
        self.assertEqual(r["diff"], -7)
        d = server.dashboard(self.conn, {})
        self.assertEqual([i["code"] for i in d["low_stock"]], ["RM-1"])

    def test_receivable_counts_only_shipped_minus_paid(self):
        server.add_move(self.conn, self.part, 10, "期初")
        o = server.create_order(self.conn, dict(kind="sales", partner_id=self.cus,
                                                lines=[dict(item_id=self.part, qty=10, unit_price=100)]))
        line = server.get_order(self.conn, o["id"])["lines"][0]
        server.deliver_order(self.conn, o["id"], dict(lines=[dict(line_id=line["id"], qty=4)]))
        server.add_payment(self.conn, o["id"], dict(amount=100))
        d = server.dashboard(self.conn, {})
        self.assertEqual(d["receivable"], 300)
        self.assertEqual(d["month_sales"], 400)

    def test_order_numbers_increment(self):
        a = server.create_order(self.conn, dict(kind="purchase", partner_id=self.sup, lines=[dict(item_id=self.bar, qty=1)]))
        b = server.create_order(self.conn, dict(kind="purchase", partner_id=self.sup, lines=[dict(item_id=self.bar, qty=1)]))
        self.assertTrue(a["no"].endswith("-001") and b["no"].endswith("-002"))


if __name__ == "__main__":
    unittest.main()
