"""生產排程：機台、製程路線、工序排隊、現場報工、外包追蹤。

一張工單由多道工序組成（例：CNC 車床 → CNC 銑床 → 熱處理（外包）→ 研磨 → 檢驗）。
每道工序排在某台機台的佇列裡（外包工序則記錄廠商與預計回廠日）。

排程估算（schedule）：
- 每台機台每天可用時數（daily_hours），只算工作日（預設週一到週六）。
- 工序所需時間 = 架機時間（還沒開始才算）+ 單件工時 ×（數量 − 已完成）。
- 一道工序最早開始 = max(機台空出來的時間, 前一道工序完成的時間)。
- 估完成日 > 工單交期 → 標記「會延誤」。
時間用「工作天」為單位計算，最後再換成日曆日期。
"""
from datetime import date, timedelta

import server
from server import ApiError, rows, num, today

PROCESSES = ["下料", "CNC 車床", "CNC 銑床", "綜合加工", "車銑複合", "線割", "放電", "鑽孔攻牙", "研磨",
             "熱處理", "表面處理", "去毛邊", "檢驗", "包裝"]
OUTSOURCE_HINT = ("熱處理", "表面處理", "電鍍", "陽極", "外包")
DEFAULT_OUTSOURCE_DAYS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT DEFAULT '',
    daily_hours REAL DEFAULT 8,
    active INTEGER DEFAULT 1,
    sort INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS item_routes (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    process TEXT NOT NULL,
    machine_id INTEGER REFERENCES machines(id),
    setup_min REAL DEFAULT 0,
    cycle_min REAL DEFAULT 0,
    outsource INTEGER DEFAULT 0,
    vendor_id INTEGER REFERENCES partners(id),
    days REAL DEFAULT 0,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wo_ops (
    id INTEGER PRIMARY KEY,
    wo_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    process TEXT NOT NULL,
    machine_id INTEGER REFERENCES machines(id),
    setup_min REAL DEFAULT 0,
    cycle_min REAL DEFAULT 0,
    outsource INTEGER DEFAULT 0,
    vendor_id INTEGER REFERENCES partners(id),
    days REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'todo',
    priority REAL DEFAULT 0,
    good_qty REAL DEFAULT 0,
    scrap_qty REAL DEFAULT 0,
    started TEXT DEFAULT '',
    finished TEXT DEFAULT '',
    sent_date TEXT DEFAULT '',
    expected_back TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ops_wo ON wo_ops(wo_id);
CREATE INDEX IF NOT EXISTS idx_ops_machine ON wo_ops(machine_id, status);
"""

OP_FIELDS = ["process", "machine_id", "setup_min", "cycle_min", "outsource", "vendor_id", "days", "note"]


def now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def workdays(conn):
    s = server.get_setting(conn, "workdays", "123456")  # 週一 = 1 … 週日 = 7
    return {int(c) for c in s if c.isdigit() and 1 <= int(c) <= 7} or {1, 2, 3, 4, 5, 6}


# ---------------------------------------------------------------- 機台

def list_machines(conn, q=None):
    return rows(conn.execute("SELECT * FROM machines WHERE active = 1 ORDER BY sort, name"))


def machine_by_name(conn, name):
    name = (name or "").strip()
    if not name:
        return None
    r = conn.execute("SELECT id, active FROM machines WHERE name = ?", (name,)).fetchone()
    if r:
        if not r["active"]:
            conn.execute("UPDATE machines SET active = 1 WHERE id = ?", (r["id"],))
        return r["id"]
    return conn.execute("INSERT INTO machines (name) VALUES (?)", (name,)).lastrowid


def save_machine(conn, body, mid=None):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError("請輸入機台名稱")
    hours = num(body.get("daily_hours") or 8, "每日工時", 0.5)
    if hours > 24:
        raise ApiError("每日工時不能超過 24")
    data = (name, body.get("kind", ""), hours, int(body.get("sort") or 0), body.get("note", ""))
    try:
        if mid:
            conn.execute("UPDATE machines SET name=?, kind=?, daily_hours=?, sort=?, note=? WHERE id=?", data + (mid,))
        else:
            mid = conn.execute("INSERT INTO machines (name, kind, daily_hours, sort, note) VALUES (?,?,?,?,?)", data).lastrowid
    except server.sqlite3.IntegrityError:
        raise ApiError(f"機台「{name}」已存在")
    return {"id": mid}


def delete_machine(conn, mid):
    if conn.execute("SELECT 1 FROM wo_ops WHERE machine_id = ? AND status != 'done'", (mid,)).fetchone():
        raise ApiError("這台機台還有未完成的工序，請先移到其他機台")
    conn.execute("UPDATE machines SET active = 0 WHERE id = ?", (mid,))
    return {"ok": True}


# ---------------------------------------------------------------- 製程路線

def _clean_ops(conn, ops):
    out = []
    for i, op in enumerate(ops or []):
        process = (op.get("process") or "").strip()
        if not process:
            continue
        outsource = 1 if op.get("outsource") in (1, True, "1", "true", "on") else 0
        mid = op.get("machine_id")
        if not str(mid or "").strip() and op.get("machine_name"):
            mid = machine_by_name(conn, op["machine_name"])
        vid = op.get("vendor_id")
        out.append({
            "process": process,
            "machine_id": None if outsource or not str(mid or "").strip() else int(mid),
            "setup_min": num(op.get("setup_min") or 0, "架機時間", 0),
            "cycle_min": num(op.get("cycle_min") or 0, "單件工時", 0),
            "outsource": outsource,
            "vendor_id": int(vid) if outsource and str(vid or "").strip() else None,
            "days": num(op.get("days") or 0, "外包天數", 0),
            "note": op.get("note", ""),
        })
    return out


def get_routes(conn, item_id):
    return rows(conn.execute(
        """SELECT r.*, m.name AS machine, p.name AS vendor FROM item_routes r
           LEFT JOIN machines m ON m.id = r.machine_id LEFT JOIN partners p ON p.id = r.vendor_id
           WHERE r.item_id = ? ORDER BY r.seq""", (item_id,)))


def save_routes(conn, item_id, body):
    ops = _clean_ops(conn, body.get("ops"))
    conn.execute("DELETE FROM item_routes WHERE item_id = ?", (item_id,))
    for i, op in enumerate(ops, 1):
        conn.execute(f"INSERT INTO item_routes (item_id, seq, {', '.join(OP_FIELDS)}) VALUES (?, ?, {', '.join('?' * len(OP_FIELDS))})",
                     [item_id, i] + [op[f] for f in OP_FIELDS])
    # 第一道機台工序的單件工時也回寫到品項，總覽的機台負荷會用到
    total = sum(op["cycle_min"] for op in ops if not op["outsource"])
    if total:
        conn.execute("UPDATE items SET cycle_min = ? WHERE id = ?", (total, item_id))
    return {"ok": True, "count": len(ops)}


def _next_priority(conn, machine_id):
    r = conn.execute("SELECT MAX(priority) FROM wo_ops WHERE machine_id IS ? AND status != 'done'", (machine_id,)).fetchone()[0]
    return (r or 0) + 1


def create_ops(conn, wo_id, ops=None):
    """開工單時建立工序：有傳入就用；否則複製品項的製程路線；都沒有就用工單上的機台建一道「加工」。"""
    w = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
    if ops is None:
        ops = get_routes(conn, w["item_id"])
        if not ops:
            cyc = conn.execute("SELECT cycle_min FROM items WHERE id = ?", (w["item_id"],)).fetchone()[0] or 0
            ops = [{"process": "加工", "machine_id": machine_by_name(conn, w["machine"]), "cycle_min": cyc}]
    ops = _clean_ops(conn, ops)
    conn.execute("DELETE FROM wo_ops WHERE wo_id = ?", (wo_id,))
    for i, op in enumerate(ops, 1):
        conn.execute(f"INSERT INTO wo_ops (wo_id, seq, priority, {', '.join(OP_FIELDS)}) VALUES (?, ?, ?, {', '.join('?' * len(OP_FIELDS))})",
                     [wo_id, i, _next_priority(conn, op["machine_id"])] + [op[f] for f in OP_FIELDS])
    return len(ops)


def ensure_ops(conn):
    """舊工單（還沒有工序）補建一道工序，排程看板才看得到。"""
    for w in rows(conn.execute("""SELECT id FROM work_orders w WHERE status IN ('pending', 'in_progress')
                                  AND NOT EXISTS (SELECT 1 FROM wo_ops o WHERE o.wo_id = w.id)""")):
        create_ops(conn, w["id"])


# ---------------------------------------------------------------- 工序操作

OP_SQL = """
SELECT o.*, w.no AS wo_no, w.qty AS wo_qty, w.due_date, w.status AS wo_status, w.item_id, w.note AS wo_note,
       i.code, i.name AS item_name, i.drawing_no, i.revision, i.material, i.unit,
       m.name AS machine, v.name AS vendor, v.phone AS vendor_phone,
       COALESCE(NULLIF(c.short_name, ''), c.name) AS customer, so.no AS sales_no,
       (SELECT COUNT(*) FROM wo_ops x WHERE x.wo_id = o.wo_id) AS op_count
FROM wo_ops o JOIN work_orders w ON w.id = o.wo_id JOIN items i ON i.id = w.item_id
LEFT JOIN machines m ON m.id = o.machine_id LEFT JOIN partners v ON v.id = o.vendor_id
LEFT JOIN orders so ON so.id = w.sales_order_id LEFT JOIN partners c ON c.id = so.partner_id
"""


def get_op(conn, oid):
    r = conn.execute(OP_SQL + " WHERE o.id = ?", (oid,)).fetchone()
    if not r:
        raise ApiError("找不到工序", 404)
    return dict(r)


def wo_ops(conn, wo_id):
    return rows(conn.execute(OP_SQL + " WHERE o.wo_id = ? ORDER BY o.seq", (wo_id,)))


def _check_open(op):
    if op["wo_status"] in ("done", "cancelled"):
        raise ApiError("工單已結案")


def start_op(conn, oid):
    op = get_op(conn, oid)
    _check_open(op)
    if op["status"] == "done":
        raise ApiError("這道工序已完成")
    conn.execute("UPDATE wo_ops SET status = 'doing', started = CASE WHEN started = '' THEN ? ELSE started END WHERE id = ?",
                 (now_str(), oid))
    conn.execute("UPDATE work_orders SET status = 'in_progress' WHERE id = ? AND status = 'pending'", (op["wo_id"],))
    return {"ok": True}


def report_op(conn, oid, body):
    """報工：回報這次做好幾件。累計達到工單數量（或勾選完成）就把工序結束。"""
    op = get_op(conn, oid)
    _check_open(op)
    good = num(body.get("good_qty") or 0, "良品數", 0)
    scrap = num(body.get("scrap_qty") or 0, "不良數", 0)
    total_good = op["good_qty"] + good
    finish = body.get("finish") in (True, 1, "1", "true") or total_good >= op["wo_qty"] - 1e-9
    conn.execute("""UPDATE wo_ops SET good_qty = good_qty + ?, scrap_qty = scrap_qty + ?,
                    status = ?, started = CASE WHEN started = '' THEN ? ELSE started END,
                    finished = CASE WHEN ? THEN ? ELSE finished END WHERE id = ?""",
                 (good, scrap, "done" if finish else "doing", now_str(), 1 if finish else 0, now_str(), oid))
    conn.execute("UPDATE work_orders SET status = 'in_progress' WHERE id = ? AND status = 'pending'", (op["wo_id"],))
    left = conn.execute("SELECT COUNT(*) FROM wo_ops WHERE wo_id = ? AND status != 'done'", (op["wo_id"],)).fetchone()[0]
    return {"ok": True, "op_done": finish, "all_done": left == 0, "wo_id": op["wo_id"]}


def send_out(conn, oid, body):
    op = get_op(conn, oid)
    _check_open(op)
    vid = body.get("vendor_id") or op["vendor_id"]
    if not vid:
        raise ApiError("請選擇外包廠商")
    sent = body.get("sent_date") or today()
    back = body.get("expected_back") or server._plus_days(sent, int(op["days"] or DEFAULT_OUTSOURCE_DAYS))
    conn.execute("""UPDATE wo_ops SET outsource = 1, machine_id = NULL, vendor_id = ?, status = 'doing',
                    sent_date = ?, expected_back = ?, started = CASE WHEN started = '' THEN ? ELSE started END WHERE id = ?""",
                 (int(vid), sent, back, now_str(), oid))
    conn.execute("UPDATE work_orders SET status = 'in_progress' WHERE id = ? AND status = 'pending'", (op["wo_id"],))
    return {"ok": True}


def update_op(conn, oid, body):
    """改機台、時間、外包設定（排程時調整）。"""
    op = get_op(conn, oid)
    merged = {f: body.get(f, op[f]) for f in OP_FIELDS}
    if "machine_id" in body and str(body["machine_id"] or "") != str(op["machine_id"] or ""):
        merged["outsource"] = 0
    clean = _clean_ops(conn, [merged])[0]
    sets = {f: clean[f] for f in OP_FIELDS}
    if clean["machine_id"] != op["machine_id"]:
        sets["priority"] = _next_priority(conn, clean["machine_id"])
    for f in ("expected_back",):
        if f in body:
            sets[f] = body[f]
    conn.execute(f"UPDATE wo_ops SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", list(sets.values()) + [oid])
    return {"ok": True}


def move_op(conn, oid, body):
    """在機台佇列中調整順序：up / down / top。"""
    op = get_op(conn, oid)
    queue = [r["id"] for r in rows(conn.execute(
        "SELECT id FROM wo_ops WHERE machine_id IS ? AND status != 'done' ORDER BY (status = 'doing') DESC, priority, id",
        (op["machine_id"],)))]
    i = queue.index(oid)
    d = body.get("direction")
    if d == "up" and i > 0:
        queue[i - 1], queue[i] = queue[i], queue[i - 1]
    elif d == "down" and i < len(queue) - 1:
        queue[i + 1], queue[i] = queue[i], queue[i + 1]
    elif d == "top":
        queue.insert(0, queue.pop(i))
    conn.executemany("UPDATE wo_ops SET priority = ? WHERE id = ?", [(k + 1, x) for k, x in enumerate(queue)])
    return {"ok": True}


# ---------------------------------------------------------------- 排程估算

class Calendar:
    """把「從今天起第幾個工作天」換成日期。"""

    def __init__(self, days, start=None):
        self.days = days
        self.start = start or date.today()
        self._cache = []

    def _nth(self, k):
        d = self.start if not self._cache else self._cache[-1] + timedelta(days=1)
        while len(self._cache) <= k:
            if d.isoweekday() in self.days:
                self._cache.append(d)
            d += timedelta(days=1)
        return self._cache[k]

    def date_of(self, t):
        """t = 工作天（浮點），0 = 今天開始。剛好整數視為前一天收工。"""
        k = int(t - 1e-9) if t > 0 else 0
        return self._nth(max(k, 0)).isoformat()

    def to_t(self, iso):
        """日曆日期 → 工作天位置（該日收工時）。"""
        target = date.fromisoformat(iso)
        if target < self.start:
            return 0.0
        k = 0
        while self._nth(k) < target:
            k += 1
        return k + 1.0


def schedule(conn, q=None):
    ops = rows(conn.execute(OP_SQL + " WHERE o.status != 'done' AND w.status IN ('pending', 'in_progress')"))
    done_ops = {(r["wo_id"], r["seq"]): r for r in rows(conn.execute(
        "SELECT wo_id, seq, finished FROM wo_ops WHERE status = 'done'"))}
    machines = list_machines(conn)
    cap = {m["id"]: m["daily_hours"] or 8 for m in machines}
    cal = Calendar(workdays(conn))
    by_wo = {}
    for op in ops:
        by_wo.setdefault(op["wo_id"], []).append(op)
        remaining = max(op["wo_qty"] - op["good_qty"], 0)
        op["remaining_hours"] = round(((op["setup_min"] if op["status"] == "todo" else 0) + op["cycle_min"] * remaining) / 60, 2)
    for lst in by_wo.values():
        lst.sort(key=lambda o: o["seq"])

    queues = {}
    for op in ops:
        if not op["outsource"]:
            queues.setdefault(op["machine_id"], []).append(op)
    for lst in queues.values():
        lst.sort(key=lambda o: (o["status"] != "doing", o["priority"], o["due_date"] or "9999", o["id"]))

    finish = {}       # op id → 完成位置（工作天）
    machine_free = {mid: 0.0 for mid in queues}

    def prev_finish(op):
        """前一道（未完成的）工序完成時間；前面都完成了就是 0。"""
        prevs = [o for o in by_wo[op["wo_id"]] if o["seq"] < op["seq"]]
        if not prevs:
            return 0.0, True
        p = prevs[-1]
        if p["id"] in finish:
            return finish[p["id"]], True
        return None, False

    def place_outsource(op):
        pf, _ = prev_finish(op)
        pf = pf or 0.0
        if op["sent_date"]:
            start_t = 0.0
            end_t = cal.to_t(op["expected_back"]) if op["expected_back"] else pf + (op["days"] or DEFAULT_OUTSOURCE_DAYS) * 6 / 7
        else:
            start_t = pf
            end_t = pf + (op["days"] or DEFAULT_OUTSOURCE_DAYS) * 6 / 7
        op["start_t"], op["end_t"] = start_t, max(end_t, start_t)
        finish[op["id"]] = op["end_t"]

    pending = {mid: list(lst) for mid, lst in queues.items()}
    outs = [o for o in ops if o["outsource"]]

    def place(op, mid, start_t):
        c = cap.get(mid, 8) if mid else 8
        op["start_t"] = start_t
        op["end_t"] = start_t + op["remaining_hours"] / c
        finish[op["id"]] = op["end_t"]
        machine_free[mid] = op["end_t"]
        pending[mid].remove(op)

    guard = 0
    while (any(pending.values()) or outs) and guard < 100000:
        guard += 1
        for o in list(outs):
            if prev_finish(o)[1]:
                place_outsource(o)
                outs.remove(o)
        # 依佇列順序排：最前面那道如果還在等前工序，機台先做後面已經可以做的（不讓機台空等）
        best = None   # (開始時間, 機台, 工序)
        for mid, lst in pending.items():
            free = machine_free[mid]
            choice = None
            for op in lst:
                pf, ok = prev_finish(op)
                if not ok:
                    continue
                start = max(free, pf)
                if pf <= free + 1e-9:
                    choice = (start, op)
                    break
                if choice is None or start < choice[0] - 1e-9:
                    choice = (start, op)
            if choice and (best is None or choice[0] < best[0] - 1e-9):
                best = (choice[0], mid, choice[1])
        if best:
            place(best[2], best[1], best[0])
            continue
        heads = [(lst[0], mid) for mid, lst in pending.items() if lst]
        if heads:
            # 互相卡住（前工序還沒排到）：先把優先序最高的硬排上去並標示
            op, mid = min(heads, key=lambda x: x[0]["priority"])
            op["conflict"] = True
            place(op, mid, machine_free[mid])
        elif outs:
            place_outsource(outs.pop(0))

    for op in ops:
        op["est_start"] = cal.date_of(op.get("start_t", 0) + 1e-6)
        op["est_finish"] = cal.date_of(op.get("end_t", 0))
        prevs = [o for o in by_wo[op["wo_id"]] if o["seq"] < op["seq"]]
        op["waiting_prev"] = prevs[-1]["process"] if prevs else ""
    wo_finish = {}
    for wo_id, lst in by_wo.items():
        end = max(o.get("end_t", 0) for o in lst)
        wo_finish[wo_id] = cal.date_of(end)
        due = lst[0]["due_date"]
        for o in lst:
            o["wo_est_finish"] = wo_finish[wo_id]
            o["late"] = bool(due and wo_finish[wo_id] > due)

    board = []
    for m in machines:
        q_ops = queues.get(m["id"], [])
        load = sum(o["remaining_hours"] for o in q_ops)
        board.append({**m, "queue": q_ops, "load_hours": round(load, 1),
                      "free_date": cal.date_of(machine_free.get(m["id"], 0)) if q_ops else "",
                      "late": sum(1 for o in q_ops if o["late"])})
    unassigned = queues.get(None, [])
    outsource = sorted([o for o in ops if o["outsource"]], key=lambda o: (o["sent_date"] == "", o["expected_back"] or "9999"))
    for o in outsource:
        o["overdue"] = bool(o["sent_date"] and o["expected_back"] and o["expected_back"] < today())
    wos = []
    for wo_id, lst in by_wo.items():
        f = lst[0]
        wos.append({"wo_id": wo_id, "wo_no": f["wo_no"], "item": f"{f['code']} {f['item_name']}", "qty": f["wo_qty"],
                    "customer": f["customer"], "due_date": f["due_date"], "est_finish": wo_finish[wo_id],
                    "late": f["late"], "current": next((o["process"] for o in lst if o["status"] == "doing"), lst[0]["process"])})
    wos.sort(key=lambda w: (not w["late"], w["due_date"] or "9999"))
    return {"machines": board, "unassigned": unassigned, "outsource": outsource, "work_orders": wos,
            "processes": PROCESSES, "today": today()}


def floor_queue(conn, q):
    """現場用：某台機台（或外包）的工作清單，含圖面附件。"""
    s = schedule(conn)
    mid = q.get("machine_id")
    if mid == "outsource":
        lst = s["outsource"]
    else:
        lst = next((m["queue"] for m in s["machines"] if str(m["id"]) == str(mid)), [])
    for op in lst[:20]:
        op["files"] = rows(conn.execute(
            """SELECT id, filename, mime FROM files WHERE (owner_type = 'item' AND owner_id = ?)
               OR (owner_type = 'workorder' AND owner_id = ?) ORDER BY id""", (op["item_id"], op["wo_id"])))
        op["ops"] = [{"seq": o["seq"], "process": o["process"], "status": o["status"]} for o in wo_ops(conn, op["wo_id"])]
    return {"queue": lst[:20], "machines": [{"id": m["id"], "name": m["name"], "count": len(m["queue"])} for m in s["machines"]],
            "outsource_count": len(s["outsource"])}
