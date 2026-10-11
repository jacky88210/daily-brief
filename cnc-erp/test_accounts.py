"""多人帳號與權限：python -m unittest test_accounts"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import accounts
import server


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old_db = server.DB_PATH
        server.DB_PATH = os.path.join(self.dir, "cnc.db")
        server.init_db()
        server._fails.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.admin = self.setup_admin()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        server.DB_PATH = self.old_db
        shutil.rmtree(self.dir)

    def req(self, method, path, body=None, cookie=""):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method)
        if cookie:
            r.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(r) as res:
                return res.status, json.loads(res.read() or b"null"), (res.headers.get("Set-Cookie") or "").split(";")[0]
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), ""

    def setup_admin(self):
        status, _, cookie = self.req("POST", "/api/auth/setup", {"username": "boss", "display_name": "老闆", "password": "boss-pass1"})
        self.assertEqual(status, 200)
        return cookie

    def login(self, username, password):
        status, body, cookie = self.req("POST", "/api/auth/login", {"username": username, "password": password})
        return status, cookie

    def add_user(self, username, role, password="pass-1234", name=None):
        status, body, _ = self.req("POST", "/api/users", {"username": username, "display_name": name or username,
                                                          "role": role, "password": password}, self.admin)
        self.assertEqual(status, 200, body)
        return body["id"]

    def test_each_manager_has_own_account_and_actions_are_attributed(self):
        self.add_user("wang", "office", name="王經理")
        self.add_user("lin", "office", name="林副總")
        _, wang = self.login("wang", "pass-1234")
        _, lin = self.login("lin", "pass-1234")
        status, cus, _ = self.req("POST", "/api/partners", {"type": "customer", "name": "永豐"}, wang)
        self.assertEqual(status, 200)
        item = self.req("POST", "/api/items", {"code": "A1", "name": "軸", "opening_stock": 5}, lin)[1]["id"]
        self.req("POST", "/api/orders", {"kind": "sales", "partner_id": cus["id"],
                                          "lines": [{"item_id": item, "qty": 1, "unit_price": 100}]}, wang)
        log = self.req("GET", "/api/audit", cookie=self.admin)[1]
        who = {(r["action"], r["username"]) for r in log}
        self.assertIn(("新增客戶 / 廠商", "王經理"), who)
        self.assertIn(("新增品項", "林副總"), who)
        self.assertIn(("新增訂單", "王經理"), who)
        self.assertIn(("登入", "林副總"), who)

    def test_office_cannot_touch_admin_functions(self):
        self.add_user("wang", "office")
        _, wang = self.login("wang", "pass-1234")
        self.assertEqual(self.req("GET", "/api/dashboard", cookie=wang)[0], 200)
        self.assertEqual(self.req("GET", "/api/quotes", cookie=wang)[0], 200)
        for method, path, body in [("GET", "/api/users", None), ("POST", "/api/users", {"username": "x", "password": "123456ab"}),
                                   ("PUT", "/api/settings", {"company_name": "x"}), ("GET", "/api/backup", None),
                                   ("GET", "/api/audit", None), ("POST", "/api/lingyue/import", {})]:
            self.assertEqual(self.req(method, path, body, wang)[0], 403, path)

    def test_floor_staff_only_see_shop_floor(self):
        self.add_user("op1", "floor", name="阿明")
        _, op = self.login("op1", "pass-1234")
        self.assertEqual(self.req("GET", "/api/floor", cookie=op)[0], 200)
        self.assertEqual(self.req("GET", "/api/schedule", cookie=op)[0], 200)
        for path in ["/api/dashboard", "/api/quotes", "/api/orders?kind=sales", "/api/history", "/api/items/page",
                     "/api/statement?partner_id=1", "/api/partners/1/summary"]:
            self.assertEqual(self.req("GET", path, cookie=op)[0], 403, path)
        self.assertEqual(self.req("POST", "/api/me/password", {"old_password": "pass-1234", "new_password": "mine-5678"}, op)[0], 200)
        self.assertEqual(self.login("op1", "mine-5678")[0], 200)

    def test_disable_user_logs_them_out(self):
        uid = self.add_user("temp", "office")
        _, t = self.login("temp", "pass-1234")
        self.assertEqual(self.req("GET", "/api/dashboard", cookie=t)[0], 200)
        self.assertEqual(self.req("PUT", f"/api/users/{uid}", {"active": 0}, self.admin)[0], 200)
        self.assertEqual(self.req("GET", "/api/dashboard", cookie=t)[0], 401)  # 立即失效
        self.assertEqual(self.login("temp", "pass-1234")[0], 403)

    def test_rules(self):
        self.assertEqual(self.req("POST", "/api/users", {"username": "王", "password": "pass-1234"}, self.admin)[0], 400)
        self.assertEqual(self.req("POST", "/api/users", {"username": "a1", "password": "123"}, self.admin)[0], 400)
        self.assertEqual(self.req("POST", "/api/users", {"username": "a1", "password": "111111"}, self.admin)[0], 400)
        self.assertEqual(self.req("POST", "/api/users", {"username": "BOSS", "password": "pass-1234"}, self.admin)[0], 400)  # 不分大小寫重複
        users = self.req("GET", "/api/users", cookie=self.admin)[1]
        boss = users[0]["id"]
        # 不能把最後一個管理員停用或降級
        self.assertEqual(self.req("PUT", f"/api/users/{boss}", {"active": 0}, self.admin)[0], 400)
        self.assertEqual(self.req("PUT", f"/api/users/{boss}", {"role": "office"}, self.admin)[0], 400)
        self.assertEqual(self.login("Boss", "boss-pass1")[0], 200)  # 帳號不分大小寫

    def test_reset_admin_and_legacy_password_migration(self):
        conn = server.connect()
        with conn:
            username, temp = accounts.reset_admin(conn)
        conn.close()
        self.assertEqual(username, "boss")
        self.assertEqual(self.login("boss", temp)[0], 200)
        # 舊版的單一密碼自動轉成 admin 帳號
        fd, old = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            server.init_db(old)
            c = server.connect(old)
            with c:
                c.execute("DELETE FROM users")
                server.set_setting(c, "password_hash", server._hash_password("oldpass"))
            c.close()
            server.init_db(old)
            c = server.connect(old)
            u = accounts.authenticate(c, "admin", "oldpass")
            self.assertEqual(u["role"], "admin")
            self.assertEqual(server.get_setting(c, "password_hash"), "")
            c.close()
        finally:
            os.remove(old)


if __name__ == "__main__":
    unittest.main()
