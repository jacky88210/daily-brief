"""雙備份、備份驗證、遠端保護、密碼鎖定、存取紀錄：python -m unittest test_security"""
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

import server


def lan_ip():
    """這台機器的非 127.0.0.1 位址，用來模擬「其他電腦連進來」。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old_db = server.DB_PATH
        server.DB_PATH = os.path.join(self.dir, "cnc.db")
        server.init_db()
        server._fails.clear()
        self.conn = server.connect()
        server.create_item(self.conn, dict(code="A", name="零件"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        server.DB_PATH = self.old_db
        shutil.rmtree(self.dir)


class BackupTest(Base):
    def test_two_destinations_verified_and_pruned(self):
        nas, usb = os.path.join(self.dir, "nas"), os.path.join(self.dir, "usb")
        os.makedirs(nas)
        os.makedirs(usb)
        for d in (nas, usb):  # 先放一些舊備份，測保留份數
            for day in range(1, 6):
                open(os.path.join(d, f"cnc-backup-2026-01-0{day}.zip"), "wb").close()
            open(os.path.join(d, "其他檔案.txt"), "w").close()
        server.save_settings(self.conn, {"backup_dir": nas, "backup_dir2": usb, "backup_keep": "3"})
        self.conn.commit()
        status = server.backup_to_dir()
        self.assertTrue(all(st["ok"] for st in status.values()), status)
        for d in (nas, usb):
            names = sorted(os.listdir(d))
            self.assertEqual(len([n for n in names if n.endswith(".zip")]), 3)
            self.assertIn("其他檔案.txt", names)  # 不是備份檔的不會被刪
            newest = os.path.join(d, f"cnc-backup-{server.today()}.zip")
            with zipfile.ZipFile(newest) as z:
                self.assertIn("cnc.db", z.namelist())
        st = server.get_settings(self.conn)["backup_status"]
        self.assertEqual(set(st), {nas, usb})

    def test_one_destination_down_other_still_works(self):
        ok_dir = os.path.join(self.dir, "ok")
        os.makedirs(ok_dir)
        missing = os.path.join(self.dir, "NAS沒開")
        server.save_settings(self.conn, {"backup_dir": missing, "backup_dir2": ok_dir})
        self.conn.commit()
        status = server.backup_to_dir()
        self.assertFalse(status[missing]["ok"])
        self.assertTrue(status[ok_dir]["ok"])
        log = [r["action"] for r in server.list_audit(self.conn, {})]
        self.assertIn("自動備份失敗", log)

    def test_verify_rejects_broken_backup(self):
        good = server.backup_bytes()
        server.verify_backup(good)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("cnc.db", b"this is not a database")
        with self.assertRaises(Exception):
            server.verify_backup(buf.getvalue())

    def test_no_destination_configured(self):
        self.assertEqual(server.backup_to_dir(), {})
        with self.assertRaises(server.ApiError):
            server.run_backup_now(self.conn, {})


class AccessTest(Base):
    def start(self, host):
        srv = ThreadingHTTPServer((host, 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://{host}:{srv.server_address[1]}"

    def req(self, base, method, path, body=None, cookie=""):
        r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method)
        if cookie:
            r.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(r) as res:
                return res.status, json.loads(res.read() or b"{}"), res.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), ""

    def test_remote_blocked_until_password_set(self):
        ip = lan_ip()
        if not ip:
            self.skipTest("沒有區網位址可以模擬")
        remote = self.start(ip)
        status, body, _ = self.req(remote, "GET", "/api/items")
        self.assertEqual((status, body.get("setup")), (403, True))
        self.assertTrue(self.req(remote, "GET", "/api/auth/status")[1]["setup_needed"])
        # 第一次設定密碼可以從遠端做（例如裝在 NAS 上時）
        self.assertEqual(self.req(remote, "POST", "/api/settings/password", {"new_password": "s3cret"})[0], 200)
        self.assertEqual(self.req(remote, "GET", "/api/items")[0], 401)
        status, _, cookie = self.req(remote, "POST", "/api/auth/login", {"password": "s3cret"})
        self.assertEqual(self.req(remote, "GET", "/api/items", cookie=cookie.split(";")[0])[0], 200)
        # 已經有密碼後，遠端不能不知道舊密碼就改掉
        self.assertEqual(self.req(remote, "POST", "/api/settings/password", {"new_password": "hijack"},
                                  cookie=cookie.split(";")[0])[0], 403)

    def test_local_works_without_password(self):
        local = self.start("127.0.0.1")
        self.assertEqual(self.req(local, "GET", "/api/items")[0], 200)

    def test_lockout_after_five_failures_and_audit(self):
        local = self.start("127.0.0.1")
        self.req(local, "POST", "/api/settings/password", {"new_password": "right"})
        for _ in range(5):
            self.assertEqual(self.req(local, "POST", "/api/auth/login", {"password": "wrong"})[0], 403)
        status, body, _ = self.req(local, "POST", "/api/auth/login", {"password": "right"})
        self.assertEqual(status, 429)  # 鎖住了，連正確密碼也要等
        self.assertIn("分鐘", body["error"])
        server._fails.clear()
        status, _, cookie = self.req(local, "POST", "/api/auth/login", {"password": "right"})
        self.assertEqual(status, 200)
        c = cookie.split(";")[0]
        actions = [r["action"] for r in self.req(local, "GET", "/api/audit", cookie=c)[1]]
        self.assertEqual(actions.count("登入失敗"), 5)
        self.assertIn("登入", actions)
        self.assertIn("設定 / 變更密碼", actions)
        urllib.request.urlopen(urllib.request.Request(local + "/api/backup", headers={"Cookie": c})).read()
        self.assertEqual(self.req(local, "GET", "/api/audit", cookie=c)[1][0]["action"], "下載備份檔")


if __name__ == "__main__":
    unittest.main()
