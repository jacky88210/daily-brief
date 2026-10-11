"""報價 → 訂單 → 工單 / 工序 → 排程 → 報工 → 外包；附件、登入：python -m unittest test_workflow"""
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

import production
import quotes
import server


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old_db = server.DB_PATH
        server.DB_PATH = os.path.join(self.dir, "cnc.db")
        server.init_db()
        self.conn = server.connect()
        c = self.conn
        self.cus = server.create_partner(c, dict(type="customer", name="永豐機械股份有限公司", short_name="永豐"))["id"]
        self.vendor = server.create_partner(c, dict(type="supplier", name="新光熱處理"))["id"]
        self.bar = server.create_item(c, dict(code="RM-AL", name="鋁棒", category="原料", opening_stock=100))["id"]
        self.lathe = production.save_machine(c, dict(name="CNC-01 車床", daily_hours=8))["id"]
        self.mill = production.save_machine(c, dict(name="CNC-02 銑床", daily_hours=10))["id"]

    def tearDown(self):
        self.conn.close()
        server.DB_PATH = self.old_db
        shutil.rmtree(self.dir)


class QuoteTest(Base):
    def test_quote_with_drawings_to_order(self):
        c = self.conn
        q = quotes.create_quote(c, dict(customer_name="新客戶精密", category="車床件", lines=[
            dict(part_name="傳動軸", drawing_no="YF-2231", revision="C", material="S45C", qty=100,
                 material_cost=40, cycle_min=6, setup_min=60, outsource_cost=10, margin_pct=25, lead_days=14)]))
        quotes.save_file(c, "quote", q["id"], "圖面 1.jpg", b"\xff\xd8fakejpeg")
        quotes.save_file(c, "quote", q["id"], "../../evil?.pdf", b"%PDF-1.4")
        got = quotes.get_quote(c, q["id"])
        ln = got["lines"][0]
        # (40 + (6 + 60/100)/60*800 + 10) * 1.25 = (40 + 88 + 10) * 1.25 = 172.5
        self.assertEqual((ln["cost"], ln["suggested"]), (138.0, 172.5))
        self.assertEqual(len(got["files"]), 2)
        self.assertEqual(got["files"][1]["filename"], "evil_.pdf")  # 路徑字元被清掉，不會寫到資料夾外
        self.assertTrue(all(os.path.isfile(quotes.file_path(c, f["id"])[1]) for f in got["files"]))
        with self.assertRaises(server.ApiError):
            quotes.set_status(c, q["id"], {"status": "quoted"})  # 還沒填單價
        quotes.update_quote(c, q["id"], {"lines": [dict(ln, unit_price=170)]})
        quotes.set_status(c, q["id"], {"status": "quoted"})
        r = quotes.convert_to_order(c, q["id"], {})
        order = server.get_order(c, r["order_id"])
        self.assertEqual((order["partner_name"], order["lines"][0]["qty"], order["lines"][0]["unit_price"]),
                         ("新客戶精密", 100, 170))
        self.assertEqual(order["due_date"], (date.today() + timedelta(days=14)).isoformat())
        item = c.execute("SELECT * FROM items WHERE code = 'YF-2231'").fetchone()
        self.assertEqual((item["drawing_no"], item["material"], item["category"], item["customer_id"]),
                         ("YF-2231", "S45C", "成品", order["partner_id"]))
        self.assertEqual(len(quotes.list_files(c, "item", item["id"])), 2)  # 圖面掛到零件上
        with self.assertRaises(server.ApiError):
            quotes.convert_to_order(c, q["id"], {})
        self.assertEqual(quotes.get_quote(c, q["id"])["status"], "won")

    def test_list_search_and_past_prices(self):
        c = self.conn
        a = quotes.create_quote(c, dict(partner_id=self.cus, category="銑床件",
                                        lines=[dict(part_name="法蘭", drawing_no="FL-9", unit_price=1500, qty=5)]))
        quotes.set_status(c, a["id"], {"status": "quoted"})
        quotes.set_status(c, a["id"], {"status": "lost", "lost_reason": "價格太高"})
        b = quotes.create_quote(c, dict(partner_id=self.cus, lines=[dict(part_name="法蘭", drawing_no="FL-9", qty=10)]))
        res = quotes.list_quotes(c, {"search": "FL-9"})
        self.assertEqual(len(res["rows"]), 2)
        self.assertEqual(res["categories"], ["銑床件"])
        self.assertEqual(quotes.list_quotes(c, {"status": "lost"})["rows"][0]["lost_reason"], "價格太高")
        past = quotes.get_quote(c, b["id"])["lines"][0]["past"]
        self.assertEqual((past[0]["type"], past[0]["unit_price"]), ("報價(未成交)", 1500))
        stats = quotes.quote_stats(c)
        self.assertEqual((stats["lost"], stats["new"], stats["win_rate"]), (1, 1, 0))

    def test_delete_shared_file_keeps_disk_copy(self):
        c = self.conn
        q = quotes.create_quote(c, dict(partner_id=self.cus, lines=[dict(part_name="x", unit_price=1)]))
        f = quotes.save_file(c, "quote", q["id"], "a.png", b"png")
        quotes.link_files(c, "quote", q["id"], "item", self.bar)
        item_file = quotes.list_files(c, "item", self.bar)[0]
        quotes.delete_file(c, f["id"])
        self.assertTrue(os.path.isfile(quotes.file_path(c, item_file["id"])[1]))
        quotes.delete_file(c, item_file["id"])
        self.assertFalse(os.listdir(os.path.dirname(os.path.join(quotes.files_dir(), item_file["stored"]))))


class ProductionTest(Base):
    def make_wo(self, qty=100, due=None, ops=None, code="SH-1"):
        c = self.conn
        found = c.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()
        item = found[0] if found else server.create_item(c, dict(code=code, name="軸", category="成品"))["id"]
        return server.create_work_order(c, dict(item_id=item, qty=qty, due_date=due or "", ops=ops,
                                                materials=[dict(item_id=self.bar, qty_per_unit=0.1)]))["id"]

    def test_routes_copied_to_work_order(self):
        c = self.conn
        item = server.create_item(c, dict(code="SH-9", name="軸", category="成品"))["id"]
        production.save_routes(c, item, {"ops": [
            dict(process="CNC 車床", machine_id=self.lathe, setup_min=30, cycle_min=3),
            dict(process="熱處理", outsource=1, vendor_id=self.vendor, days=4),
            dict(process="CNC 銑床", machine_name="CNC-03 新機台", cycle_min=2)]})
        self.assertEqual(c.execute("SELECT cycle_min FROM items WHERE id = ?", (item,)).fetchone()[0], 5)
        wo = server.create_work_order(c, dict(item_id=item, qty=10))["id"]
        ops = production.wo_ops(c, wo)
        self.assertEqual([o["process"] for o in ops], ["CNC 車床", "熱處理", "CNC 銑床"])
        self.assertEqual((ops[1]["outsource"], ops[1]["vendor"], ops[2]["machine"]), (1, "新光熱處理", "CNC-03 新機台"))

    def test_schedule_order_dependencies_and_lateness(self):
        c = self.conn
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        # 工單 A：車床 16 小時 → 銑床 10 小時；交期明天 → 一定會延誤
        a = self.make_wo(qty=160, due=tomorrow, ops=[
            dict(process="CNC 車床", machine_id=self.lathe, cycle_min=6),
            dict(process="CNC 銑床", machine_id=self.mill, cycle_min=3.75)])
        # 工單 B：只有銑床 5 小時，沒有交期
        b = self.make_wo(qty=50, code="SH-2", ops=[dict(process="CNC 銑床", machine_id=self.mill, cycle_min=6)])
        s = production.schedule(c)
        lathe = next(m for m in s["machines"] if m["id"] == self.lathe)
        mill = next(m for m in s["machines"] if m["id"] == self.mill)
        self.assertEqual(lathe["load_hours"], 16)
        a_mill = next(o for o in mill["queue"] if o["wo_id"] == a)
        b_mill = next(o for o in mill["queue"] if o["wo_id"] == b)
        # A 的銑床要等車床做完（2 個工作天）才能開始；B 不用等
        self.assertAlmostEqual(a_mill["start_t"], 2.0)
        # B 排在 A 後面，但 A 還在等車床，銑床不空等 → B 先做（0 ~ 0.5 天）
        self.assertAlmostEqual(b_mill["start_t"], 0.0)
        self.assertAlmostEqual(b_mill["end_t"], 0.5)
        self.assertTrue(a_mill["late"])
        self.assertEqual(a_mill["waiting_prev"], "CNC 車床")
        self.assertFalse(b_mill["late"])
        self.assertEqual(s["work_orders"][0]["wo_id"], a)  # 會延誤的排最前面
        # 把 B 移到最前面
        production.move_op(c, b_mill["id"], {"direction": "top"})
        mill = next(m for m in production.schedule(c)["machines"] if m["id"] == self.mill)
        self.assertEqual(mill["queue"][0]["wo_id"], b)

    def test_report_and_outsource_flow(self):
        c = self.conn
        wo = self.make_wo(qty=10, ops=[
            dict(process="CNC 車床", machine_id=self.lathe, cycle_min=5),
            dict(process="陽極處理", outsource=1, days=3)])
        lathe_op, out_op = production.wo_ops(c, wo)
        production.start_op(c, lathe_op["id"])
        self.assertEqual(server.get_work_order(c, wo)["status"], "in_progress")
        r = production.report_op(c, lathe_op["id"], {"good_qty": 6})
        self.assertFalse(r["op_done"])
        r = production.report_op(c, lathe_op["id"], {"good_qty": 4, "scrap_qty": 1})
        self.assertTrue(r["op_done"])
        self.assertFalse(r["all_done"])
        with self.assertRaises(server.ApiError):
            production.send_out(c, out_op["id"], {})  # 沒有指定廠商
        production.send_out(c, out_op["id"], {"vendor_id": self.vendor, "sent_date": "2026-01-01", "expected_back": "2026-01-04"})
        s = production.schedule(c)
        self.assertTrue(s["outsource"][0]["overdue"])
        self.assertEqual(s["outsource"][0]["vendor"], "新光熱處理")
        r = production.report_op(c, out_op["id"], {"good_qty": 10})
        self.assertTrue(r["all_done"])
        server.complete_work_order(c, wo, {"good_qty": 10})
        self.assertEqual(production.schedule(c)["work_orders"], [])

    def test_old_work_orders_get_ops(self):
        c = self.conn
        item = server.create_item(c, dict(code="OLD", name="舊件", category="成品", cycle_min=3))["id"]
        c.execute("INSERT INTO work_orders (no, item_id, qty, machine, created) VALUES ('WO-OLD', ?, 20, '舊機台', '2026-01-01')", (item,))
        production.ensure_ops(c)
        ops = production.wo_ops(c, c.execute("SELECT id FROM work_orders WHERE no = 'WO-OLD'").fetchone()[0])
        self.assertEqual((len(ops), ops[0]["machine"], ops[0]["cycle_min"]), (1, "舊機台", 3))

    def test_calendar_skips_sundays(self):
        cal = production.Calendar({1, 2, 3, 4, 5, 6}, start=date(2026, 10, 10))  # 週六
        self.assertEqual(cal.date_of(0.5), "2026-10-10")
        self.assertEqual(cal.date_of(1.5), "2026-10-12")  # 跳過週日
        self.assertEqual(cal.to_t("2026-10-12"), 2.0)

    def test_machine_cannot_be_removed_with_open_work(self):
        self.make_wo(ops=[dict(process="CNC 車床", machine_id=self.lathe, cycle_min=1)])
        with self.assertRaises(server.ApiError):
            production.delete_machine(self.conn, self.lathe)
        production.delete_machine(self.conn, self.mill)


class HttpTest(Base):
    """實際啟動 HTTP 伺服器測：登入、上傳 / 下載附件、備份 zip。"""

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.conn.commit()
        self.cookie = ""

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if self.cookie:
            r.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(r) as res:
                if res.headers.get("Set-Cookie"):
                    self.cookie = res.headers["Set-Cookie"].split(";")[0]
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_login_flow(self):
        self.assertEqual(self.req("GET", "/api/dashboard")[0], 200)  # 還沒有帳號前，系統電腦本機可以直接用
        self.assertEqual(self.req("POST", "/api/auth/setup", {"username": "boss", "display_name": "老闆", "password": "abc123!"})[0], 200)
        self.cookie = ""
        self.assertEqual(self.req("GET", "/api/dashboard")[0], 401)
        st = json.loads(self.req("GET", "/api/auth/status")[1])
        self.assertEqual((st["required"], st["logged_in"], st["setup_needed"]), (True, False, False))
        self.assertEqual(self.req("POST", "/api/auth/login", {"username": "boss", "password": "wrong"})[0], 403)
        self.assertEqual(self.req("POST", "/api/auth/login", {"username": "boss", "password": "abc123!"})[0], 200)
        self.assertTrue(self.cookie.startswith("cnc_session="))
        self.assertEqual(self.req("GET", "/api/dashboard")[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/me")[1])["display_name"], "老闆")
        self.assertEqual(self.req("GET", "/")[0], 200)  # 頁面本身不含資料，可以直接開
        # 換密碼要輸入目前的密碼
        self.assertEqual(self.req("POST", "/api/me/password", {"old_password": "x", "new_password": "newpass9"})[0], 403)
        self.assertEqual(self.req("POST", "/api/auth/logout")[0], 200)
        self.cookie = ""
        self.assertEqual(self.req("GET", "/api/files/1/raw")[0], 401)

    def test_upload_download_and_backup(self):
        q = quotes.create_quote(self.conn, dict(partner_id=self.cus, lines=[dict(part_name="x")]))
        self.conn.commit()
        status, body, _ = self.req("POST", f"/api/files/upload?owner_type=quote&owner_id={q['id']}&name=%E5%9C%96.jpg",
                                   raw=b"\xff\xd8jpegdata")
        self.assertEqual(status, 200, body)
        fid = json.loads(body)["id"]
        status, data, headers = self.req("GET", f"/api/files/{fid}/raw")
        self.assertEqual((status, data, headers["Content-Type"]), (200, b"\xff\xd8jpegdata", "image/jpeg"))
        self.assertIn("filename*=UTF-8''%E5%9C%96.jpg", headers["Content-Disposition"])
        self.assertEqual(self.req("POST", "/api/files/upload?owner_type=hack&owner_id=1&name=a", raw=b"x")[0], 400)
        status, data, _ = self.req("GET", "/api/backup")
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("cnc.db", names)
        self.assertTrue(any(n.startswith("files/") and n.endswith("圖.jpg") for n in names))


if __name__ == "__main__":
    unittest.main()
