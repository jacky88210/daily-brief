"""品項分頁、客戶統計 / 分類、公司登記查詢、總覽趨勢：python -m unittest test_features"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

import importer
import server


def ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


class FeatureTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.init_db(self.path)
        self.conn = server.connect(self.path)
        c = self.conn
        self.cus = server.create_partner(c, dict(type="customer", name="永豐機械股份有限公司", short_name="永豐",
                                                 tax_id="12345678", phone="04-1"))["id"]
        self.cus2 = server.create_partner(c, dict(type="customer", name="台中自動化"))["id"]
        self.sup = server.create_partner(c, dict(type="supplier", name="大成金屬"))["id"]
        self.items = [server.create_item(c, dict(code=f"P{i:03d}", name=f"零件{i}", cost=10, opening_stock=i,
                                                 drawing_no=f"DW-{i}", material="SUS304" if i % 2 else "AL6061"))["id"]
                      for i in range(1, 121)]

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_migration_adds_columns_to_old_database(self):
        fd, old = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            raw = sqlite3.connect(old)
            raw.executescript("""CREATE TABLE items (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                                     category TEXT, spec TEXT, unit TEXT, safety_stock REAL, cost REAL, price REAL,
                                     location TEXT, note TEXT, active INTEGER DEFAULT 1);
                                 INSERT INTO items (code, name) VALUES ('OLD', '舊品項');""")
            raw.close()
            server.init_db(old)
            server.init_db(old)  # 跑兩次也不會出錯
            c = server.connect(old)
            cols = {r[1] for r in c.execute("PRAGMA table_info(items)")}
            self.assertTrue({"drawing_no", "material", "cycle_min", "customer_id"} <= cols)
            self.assertEqual(c.execute("SELECT name FROM items").fetchone()[0], "舊品項")
            c.close()
        finally:
            os.remove(old)

    def test_page_items_paging_sorting_search(self):
        p = server.page_items(self.conn, {"per": 50, "page": 3})
        self.assertEqual((p["total"], len(p["rows"])), (120, 20))
        p = server.page_items(self.conn, {"sort": "stock", "dir": "desc", "per": 10})
        self.assertEqual([r["stock"] for r in p["rows"][:3]], [120, 119, 118])
        self.assertEqual(p["value"], sum(range(1, 121)) * 10)
        p = server.page_items(self.conn, {"search": "SUS304 DW-1"})
        self.assertTrue(all("SUS304" == r["material"] and "DW-1" in r["drawing_no"] for r in p["rows"]))
        self.conn.execute("UPDATE items SET safety_stock = 5")
        self.assertEqual(server.page_items(self.conn, {"low": "1"})["total"], 4)

    def test_update_keeps_fields_not_sent(self):
        iid = self.items[0]
        server.update_item(self.conn, iid, dict(code="P001", name="改名"))
        it = self.conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
        self.assertEqual((it["name"], it["drawing_no"], it["material"]), ("改名", "DW-1", "SUS304"))
        server.update_partner(self.conn, self.cus, dict(type="customer", name="永豐機械股份有限公司", grp="A級"))
        p = self.conn.execute("SELECT * FROM partners WHERE id = ?", (self.cus,)).fetchone()
        self.assertEqual((p["grp"], p["tax_id"], p["short_name"]), ("A級", "12345678", "永豐"))

    def _history(self):
        importer.apply_history(self.conn, [
            dict(doc_date=ago(400 + i * 30), doc_no=f"OLD{i}", partner="永豐機械股份有限公司", item_code="P001",
                 item_name="零件1", qty=10, unit_price=100, amount=1000) for i in range(5)] + [
            dict(doc_date=ago(10), doc_no="NEW1", partner="台中自動化", item_code="P001", item_name="零件1",
                 qty=5, unit_price=120, amount=600),
            dict(doc_date=ago(20), doc_no="NEW2", partner="台中自動化", item_code="P001", item_name="零件1",
                 qty=5, unit_price=120, amount=600),
            dict(doc_date=ago(30), doc_no="NEW3", partner="台中自動化", item_code="P001", item_name="零件1",
                 qty=5, unit_price=120, amount=600),
        ], "sales")

    def test_partner_stats_and_batch(self):
        self._history()
        ps = {p["id"]: p for p in server.list_partners(self.conn, {"type": "customer", "stats": "1"})}
        self.assertEqual(ps[self.cus]["docs"], 5)
        self.assertEqual(ps[self.cus]["amount_total"], 5000)
        self.assertEqual(ps[self.cus2]["last_date"], ago(10))
        server.batch_partners(self.conn, {"ids": [self.cus, self.cus2], "set": {"grp": "汽車零件"}})
        server.batch_partners(self.conn, {"ids": [self.cus2], "set": {"type": "supplier"}})
        rows = {r["id"]: r for r in server.list_partners(self.conn, {})}
        self.assertEqual(rows[self.cus]["grp"], "汽車零件")
        self.assertEqual(rows[self.cus2]["type"], "supplier")
        with self.assertRaises(server.ApiError):
            server.batch_partners(self.conn, {"ids": [], "set": {"grp": "x"}})

    def test_item_detail(self):
        self._history()
        d = server.item_detail(self.conn, self.items[0])
        self.assertEqual(d["item"]["stock"], 1)
        self.assertEqual(d["history"]["count"], 8)
        names = {p["partner_name"]: p for p in d["partners"]}
        self.assertEqual(names["台中自動化"]["last_price"], 120)
        self.assertEqual(names["永豐機械股份有限公司"]["times"], 5)

    def test_dashboard_trends(self):
        self._history()
        d = server.dashboard(self.conn, {})
        self.assertEqual(len(d["months"]), 12)
        self.assertEqual(sum(m["amount"] for m in d["months"]), 1800)
        self.assertEqual(d["top_customers"][0]["name"], "台中自動化")
        self.assertEqual(d["dormant"][0]["name"], "永豐")  # 5 張單、最後一張 400 天前
        self.assertEqual(d["repeat_parts"][0]["item_code"], "P001")

    def test_machine_load(self):
        self.conn.execute("UPDATE items SET cycle_min = 6 WHERE id = ?", (self.items[0],))
        server.create_work_order(self.conn, dict(item_id=self.items[0], qty=100, machine="CNC-01"))
        server.create_work_order(self.conn, dict(item_id=self.items[1], qty=10, machine="CNC-01"))
        m = server.dashboard(self.conn, {})["machines"][0]
        self.assertEqual((m["machine"], m["orders"], m["hours"], m["no_cycle"]), ("CNC-01", 2, 10, 1))

    def test_company_lookup_parsing(self):
        calls = []

        def fake(guid, filt):
            calls.append(guid)
            if guid == server.GCIS_COMPANY:
                return None  # 不是公司 → 改查商號
            return {"President_No": "12345678", "Business_Name": "永豐機械行", "Responsible_Name": "王大明",
                    "Business_Address": "臺中市西屯區", "Business_Register_Funds": "500000",
                    "Business_Setup_Approve_Date": "0850510", "Business_Current_Status_Desc": "核准設立"}
        old = server._gcis_get
        server._gcis_get = fake
        try:
            r = server.company_lookup(self.conn, {"tax_id": "1234-5678"})
        finally:
            server._gcis_get = old
        self.assertEqual(calls, [server.GCIS_COMPANY, server.GCIS_BUSINESS])
        self.assertEqual((r["found"], r["kind"], r["name"], r["owner"], r["capital"], r["setup_date"]),
                         (True, "商號", "永豐機械行", "王大明", "500,000", "1996-05-10"))
        company = server.parse_gcis({"Company_Name": "永豐機械股份有限公司", "Capital_Stock_Amount": 10000000,
                                     "Company_Location": "臺中市", "Company_Status_Desc": "核准設立",
                                     "Company_Setup_Date": "0750101", "Responsible_Name": "王"})
        self.assertEqual((company["capital"], company["setup_date"]), ("10,000,000", "1986-01-01"))

    def test_company_lookup_offline(self):
        def boom(guid, filt):
            raise OSError("沒有網路")
        old = server._gcis_get
        server._gcis_get = boom
        try:
            r = server.company_lookup(self.conn, {"tax_id": "12345678"})
        finally:
            server._gcis_get = old
        self.assertFalse(r["found"])
        self.assertIn("沒有網路", r["errors"][0])
        self.assertTrue(r["manual_url"].startswith("https://"))
        with self.assertRaises(server.ApiError):
            server.company_lookup(self.conn, {"tax_id": "123"})


if __name__ == "__main__":
    unittest.main()
