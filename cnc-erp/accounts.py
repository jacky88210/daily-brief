"""使用者帳號與權限。

每個人一個帳號（不共用密碼）：存取紀錄查得到是誰做的，有人離職只要停用他的帳號。

角色：
    admin   管理員     全部功能，含使用者帳號、系統設定、備份、匯入、存取紀錄
    office  辦公室人員  報價、訂單、採購、庫存、客戶、排程、單據…（不能改系統設定、帳號、下載備份）
    floor   現場人員    只有「現場報工」「排程」與看圖面，看不到價格、客戶往來、金額
"""
import re
from datetime import datetime

import server
from server import ApiError, rows

ROLES = {"admin": "管理員", "office": "辦公室人員", "floor": "現場人員"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'office',
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    last_login TEXT DEFAULT ''
);
"""

# 只有管理員能用的功能
ADMIN_ONLY = [
    (None, r"/api/users(/.*)?"),
    ("PUT", r"/api/settings"),
    ("GET", r"/api/system"),
    (None, r"/api/backup(/.*)?"),
    (None, r"/api/lingyue/.*"),
    (None, r"/api/import"),
    (None, r"/api/audit"),
    ("POST", r"/api/machines"),
    ("PUT", r"/api/machines/\d+"),
    ("DELETE", r"/api/machines/\d+"),
]
# 現場人員只能用這些
FLOOR_ALLOWED = [
    ("GET", r"/api/floor"),
    ("GET", r"/api/schedule"),
    ("GET", r"/api/machines"),
    ("GET", r"/api/workorders/\d+"),
    ("POST", r"/api/workorders/\d+/(start|complete)"),
    ("POST", r"/api/ops/\d+/(start|report|send)"),
    ("GET", r"/api/files/\d+/raw"),
    ("GET", r"/api/partners"),          # 送外包時選廠商
    ("GET", r"/api/me"),
    ("POST", r"/api/me/password"),
]


def _match(rules, method, path):
    return any((m is None or m == method) and re.fullmatch(p, path) for m, p in rules)


def allowed(role, method, path):
    if role == "admin":
        return True
    if _match(ADMIN_ONLY, method, path):
        return False
    if role == "floor":
        return _match(FLOOR_ALLOWED, method, path)
    return role == "office"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def has_users(conn):
    return bool(conn.execute("SELECT 1 FROM users WHERE active = 1 LIMIT 1").fetchone())


def migrate_single_password(conn):
    """舊版只有一組共用密碼：自動轉成帳號 admin（密碼不變）。"""
    old = server.get_setting(conn, "password_hash")
    if old and not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.execute("INSERT INTO users (username, display_name, role, password_hash, created) VALUES (?,?,?,?,?)",
                     ("admin", "管理員", "admin", old, now()))
        server.set_setting(conn, "password_hash", "")
        conn.execute("DELETE FROM sessions")


def public(u):
    if not u:
        return None
    out = {k: u[k] for k in ("id", "username", "display_name", "role", "active", "created", "last_login")}
    out["role_label"] = ROLES.get(u["role"], u["role"])
    return out


def _check_new_password(pw):
    if not pw or len(pw) < 6:
        raise ApiError("密碼至少 6 個字")
    if pw.isdigit() and len(set(pw)) <= 2:
        raise ApiError("密碼太簡單（例如 111111、121212），請換一組")


def _clean(conn, body, uid=None):
    username = (body.get("username") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,30}", username):
        raise ApiError("帳號請用 2~30 個英文、數字（可用 . _ -），例：wang、chen.boss")
    name = (body.get("display_name") or "").strip() or username
    role = body.get("role") or "office"
    if role not in ROLES:
        raise ApiError("角色錯誤")
    dup = conn.execute("SELECT id FROM users WHERE username = ? AND id != ?", (username, uid or 0)).fetchone()
    if dup:
        raise ApiError(f"帳號「{username}」已經有人用了")
    return username, name, role


def list_users(conn, q=None):
    return [public(u) for u in rows(conn.execute("SELECT * FROM users ORDER BY active DESC, role, username"))]


def create_user(conn, body):
    username, name, role = _clean(conn, body)
    _check_new_password(body.get("password"))
    uid = conn.execute("INSERT INTO users (username, display_name, role, password_hash, created) VALUES (?,?,?,?,?)",
                       (username, name, role, server._hash_password(body["password"]), now())).lastrowid
    return {"id": uid}


def _admins_left(conn, excluding):
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                        (excluding,)).fetchone()[0]


def update_user(conn, uid, body):
    u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not u:
        raise ApiError("找不到帳號", 404)
    username, name, role = _clean(conn, {**dict(u), **body}, uid)
    active = 1 if body.get("active", u["active"]) in (1, True, "1", "true") else 0
    if u["role"] == "admin" and (role != "admin" or not active) and not _admins_left(conn, uid):
        raise ApiError("至少要留一個啟用中的管理員帳號")
    conn.execute("UPDATE users SET username = ?, display_name = ?, role = ?, active = ? WHERE id = ?",
                 (username, name, role, active, uid))
    if body.get("password"):
        _check_new_password(body["password"])
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (server._hash_password(body["password"]), uid))
    if not active or body.get("password") or role != u["role"]:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))  # 停用 / 重設密碼 / 改角色 → 立即登出
    return {"ok": True}


def setup_first_admin(conn, body):
    """第一次使用：建立第一個管理員帳號（只有還沒有任何帳號時可以）。"""
    if has_users(conn):
        raise ApiError("已經有帳號了，請直接登入", 403)
    _check_new_password(body.get("password"))
    r = create_user(conn, {**body, "role": "admin"})
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), r["id"]))
    return r


def authenticate(conn, username, password):
    import hmac
    u = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", ((username or "").strip(),)).fetchone()
    if not u:
        server._hash_password(password or "x")  # 帳號不存在也花一樣的時間，不讓人猜帳號
        return None
    try:
        _, salt, _ = u["password_hash"].split("$")
    except ValueError:
        return None
    if not hmac.compare_digest(server._hash_password(password or "", salt), u["password_hash"]):
        return None
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), u["id"]))
    return dict(u)


def session_user(conn, token):
    if not token:
        return None
    r = conn.execute(
        """SELECT u.*, s.last_seen FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND u.active = 1""", (token,)).fetchone()
    if not r:
        return None
    if r["last_seen"] < server._plus_days(server.today(), -server.SESSION_DAYS):
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    conn.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (now(), token))
    return dict(r)


def change_own_password(conn, user, body):
    if not user:
        raise ApiError("系統電腦本機模式沒有帳號，請先建立管理員帳號")
    if not authenticate(conn, user["username"], body.get("old_password", "")):
        raise ApiError("目前的密碼不正確", 403)
    _check_new_password(body.get("new_password"))
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (server._hash_password(body["new_password"]), user["id"]))
    return {"ok": True}


def reset_admin(conn):
    """忘記密碼（在系統電腦執行 python server.py --reset-admin）：第一個管理員改成一組臨時密碼。"""
    import secrets
    u = conn.execute("SELECT * FROM users WHERE role = 'admin' ORDER BY active DESC, id LIMIT 1").fetchone()
    temp = secrets.token_urlsafe(6)
    if u:
        conn.execute("UPDATE users SET password_hash = ?, active = 1 WHERE id = ?", (server._hash_password(temp), u["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
        return u["username"], temp
    conn.execute("INSERT INTO users (username, display_name, role, password_hash, created) VALUES (?,?,?,?,?)",
                 ("admin", "管理員", "admin", server._hash_password(temp), now()))
    return "admin", temp


# 存取紀錄裡，修改資料的動作用中文描述
WRITE_LABELS = [
    (r"POST /api/orders", "新增訂單"), (r"PUT /api/orders/\d+", "修改訂單"),
    (r"POST /api/orders/\d+/deliver", "出貨 / 進貨"), (r"POST /api/orders/\d+/payments", "登記收付款"),
    (r"POST /api/orders/\d+/cancel", "作廢訂單"),
    (r"POST /api/quotes", "新增報價"), (r"PUT /api/quotes/\d+", "修改報價"),
    (r"POST /api/quotes/\d+/status", "變更報價狀態"), (r"POST /api/quotes/\d+/convert", "報價轉訂單"),
    (r"POST /api/items", "新增品項"), (r"PUT /api/items/\d+", "修改品項"), (r"DELETE /api/items/\d+", "停用品項"),
    (r"POST /api/stock/adjust", "盤點調整"),
    (r"POST /api/partners", "新增客戶 / 廠商"), (r"PUT /api/partners/\d+", "修改客戶 / 廠商"),
    (r"DELETE /api/partners/\d+", "停用客戶 / 廠商"), (r"POST /api/partners/batch", "批次修改客戶 / 廠商"),
    (r"POST /api/workorders", "開工單"), (r"POST /api/workorders/\d+/complete", "完工入庫"),
    (r"POST /api/workorders/\d+/cancel", "取消工單"),
    (r"POST /api/ops/\d+/report", "報工"), (r"POST /api/ops/\d+/send", "送外包"),
    (r"DELETE /api/files/\d+", "刪除附件"), (r"POST /api/files/upload", "上傳附件"),
    (r"PUT /api/settings", "修改系統設定"), (r"POST /api/users", "新增帳號"), (r"PUT /api/users/\d+", "修改帳號"),
    (r"POST /api/lingyue/import", "從凌越轉入資料"), (r"POST /api/import", "匯入資料"),
]


def write_label(method, path):
    key = f"{method} {path}"
    return next((label for p, label in WRITE_LABELS if re.fullmatch(p, key)), None)
