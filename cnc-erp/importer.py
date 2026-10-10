"""從舊系統（例如凌越）匯入基本資料。

支援 Excel (.xlsx)、CSV（UTF-8 或 Big5）、dBASE/FoxPro 資料檔 (.dbf)。
欄位名稱自動辨識；辨識不到的可以在畫面上手動對應。
流程：先預覽（不寫入）→ 確認後才匯入，匯入在單一交易中完成，出錯整批取消。
"""
import base64
import csv
import hashlib
import io
import re
import struct
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import server

# 每種資料的欄位：(欄位, 顯示名稱, 可能的舊系統欄位名稱)
FIELDS = {
    "items": [
        ("code", "料號 *", ["料號", "品號", "產品編號", "產品代號", "商品編號", "貨號", "料品編號", "物料編號", "編號", "代號"]),
        ("name", "品名 *", ["品名", "產品名稱", "商品名稱", "品名規格", "物料名稱", "名稱"]),
        ("spec", "規格", ["規格", "規格說明", "型號", "尺寸", "材質"]),
        ("unit", "單位", ["單位", "庫存單位", "計量單位"]),
        ("category", "分類", ["分類", "類別", "產品類別", "大類", "類別名稱"]),
        ("stock", "目前庫存", ["庫存", "庫存量", "庫存數量", "現有庫存", "現存量", "結存", "結存數量", "期末數量", "數量"]),
        ("safety_stock", "安全庫存", ["安全存量", "安全庫存", "最低存量", "最低庫存"]),
        ("cost", "成本", ["成本", "單位成本", "平均成本", "標準成本", "最近進價", "進價", "標準進價"]),
        ("price", "售價", ["售價", "標準售價", "定價", "單價", "建議售價"]),
        ("location", "儲位", ["儲位", "倉位", "庫位", "存放位置", "倉庫"]),
        ("note", "備註", ["備註", "說明"]),
    ],
    "partners": [
        ("name", "名稱 *", ["客戶名稱", "廠商名稱", "公司名稱", "名稱", "全名", "客戶全名", "廠商全名",
                          "簡稱", "客戶簡稱", "廠商簡稱"]),
        ("old_code", "舊系統編號", ["客戶編號", "廠商編號", "客戶代號", "廠商代號", "編號", "代號"]),
        ("contact", "聯絡人", ["聯絡人", "連絡人", "負責人", "聯絡人員"]),
        ("phone", "電話", ["電話", "電話一", "電話1", "聯絡電話", "公司電話", "手機", "行動電話"]),
        ("tax_id", "統一編號", ["統一編號", "統編"]),
        ("address", "地址", ["地址", "公司地址", "營業地址", "送貨地址", "發票地址", "聯絡地址"]),
        ("note", "備註", ["備註", "付款條件", "說明"]),
    ],
}
FIELDS["history"] = [
    ("doc_date", "日期 *", ["日期", "單據日期", "銷貨日期", "出貨日期", "進貨日期", "交易日期", "訂單日期", "開單日期"]),
    ("doc_no", "單號", ["單號", "單據號碼", "單據編號", "銷貨單號", "出貨單號", "進貨單號", "訂單單號", "訂單編號",
                       "憑證號碼", "發票號碼"]),
    ("partner", "客戶 / 廠商 *", ["客戶名稱", "客戶簡稱", "廠商名稱", "廠商簡稱", "客戶", "廠商", "供應商", "交易對象", "名稱"]),
    ("item_code", "料號", ["產品編號", "料號", "品號", "產品代號", "商品編號", "貨號", "料品編號"]),
    ("item_name", "品名", ["品名", "產品名稱", "商品名稱", "品名規格"]),
    ("spec", "規格", ["規格", "規格說明", "型號"]),
    ("unit", "單位", ["單位"]),
    ("qty", "數量", ["數量", "銷貨數量", "出貨數量", "進貨數量", "交易數量"]),
    ("unit_price", "單價", ["單價", "銷貨單價", "進貨單價", "售價", "進價"]),
    ("amount", "金額", ["金額", "小計", "未稅金額", "銷貨金額", "進貨金額", "合計金額"]),
    ("note", "備註", ["備註", "說明", "摘要"]),
]
REQUIRED = {"items": ["code", "name"], "partners": ["name"], "history": ["doc_date", "partner"]}
NUMERIC = {"stock", "safety_stock", "cost", "price", "qty", "unit_price", "amount"}
TOTAL_ROW = re.compile(r"(本頁|本單|單據|客戶|廠商|總)?(合計|總計|小計)(金額)?")
# 報表常見「同一張單只在第一行印單號、日期、客戶」，下面幾行留白 → 往下沿用
FILL_DOWN = {"history": ["doc_date", "doc_no", "partner"]}
CATEGORIES = ["原料", "成品", "半成品", "刀具", "耗材", "其他"]


def _norm(s):
    s = str(s or "")
    # 全形轉半形、去空白與常見符號，讓「品　名」「品名:」也對得上
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    return re.sub(r"[\s:：()（）\[\]*]", "", s).lower()


# ---------------------------------------------------------------- 讀檔

def _decode(raw):
    for enc in ("utf-8-sig", "cp950", "big5hkscs"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp950", errors="replace")


def read_csv(raw):
    text = _decode(raw)
    dialect = csv.excel_tab if text.count("\t") > text.count(",") else csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect)]


def read_xlsx(raw):
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise server.ApiError("無法開啟 Excel 檔。如果是舊版 .xls，請用 Excel「另存新檔」成 .xlsx 或 CSV")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        raise server.ApiError("Excel 檔裡沒有工作表")
    sheet = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in sheets else sheets[0]
    out = []
    for row in ET.fromstring(z.read(sheet)).iter("{%s}row" % ns["m"]):
        cells = {}
        for c in row.findall("m:c", ns):
            col = 0
            for ch in re.match(r"[A-Z]+", c.get("r", "A")).group():
                col = col * 26 + ord(ch) - 64
            t = c.get("t")
            v = c.find("m:v", ns)
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
            else:
                val = v.text if v is not None else ""
                if val and re.fullmatch(r"-?\d+\.0+", val):
                    val = val.split(".")[0]
            cells[col - 1] = val
        # 依列號補上空白列，讓「第幾列」跟 Excel 看到的一致
        row_no = int(row.get("r") or len(out) + 1)
        while len(out) < row_no - 1:
            out.append([])
        r = [""] * (max(cells) + 1 if cells else 0)
        for i, v in cells.items():
            r[i] = v
        out.append(r)
    return out


def read_dbf(raw):
    """dBASE III / FoxPro .dbf，文字以 Big5 解碼。"""
    if len(raw) < 32:
        raise server.ApiError("DBF 檔案太小或已損壞")
    n_rec, hdr_len, rec_len = struct.unpack("<IHH", raw[4:12])
    fields, pos = [], 32
    while pos < hdr_len - 1 and raw[pos] != 0x0D:
        name = raw[pos:pos + 11].split(b"\0")[0]
        fields.append((_decode(name).strip(), chr(raw[pos + 11]), raw[pos + 16]))
        pos += 32
    rows = [[f[0] for f in fields]]
    for i in range(n_rec):
        rec = raw[hdr_len + i * rec_len: hdr_len + (i + 1) * rec_len]
        if not rec or rec[:1] == b"*":  # 已刪除的紀錄
            continue
        off, row = 1, []
        for _, ftype, flen in fields:
            val = rec[off:off + flen]
            off += flen
            row.append("" if ftype in "MGPB" else _decode(val).strip())
        rows.append(row)
    return rows


def read_table(filename, raw):
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "xlsx":
        return read_xlsx(raw)
    if ext == "dbf":
        return read_dbf(raw)
    if ext == "xls":
        raise server.ApiError("舊版 .xls 請先用 Excel 開啟，「另存新檔」成 .xlsx 或 CSV 再匯入")
    return read_csv(raw)


# ---------------------------------------------------------------- 欄位對應

def find_header(rows, kind):
    """報表前面常有公司名稱、列印日期等抬頭，找出真正的表頭在哪一列。"""
    aliases = {_norm(a) for _, _, al in FIELDS[kind] for a in al}
    best, best_score = 0, -1
    for i, r in enumerate(rows[:20]):
        score = sum(1 for c in r if _norm(c) in aliases)
        if score > best_score:
            best, best_score = i, score
    return best


def auto_mapping(headers, kind):
    norm = [_norm(h) for h in headers]
    mapping, used = {}, set()
    for field, _, aliases in FIELDS[kind]:
        for a in aliases:
            a = _norm(a)
            idx = next((i for i, h in enumerate(norm) if h == a and i not in used), None)
            if idx is not None:
                mapping[field] = idx
                used.add(idx)
                break
    return mapping


def _num(v):
    s = str(v or "").replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def norm_date(v):
    """把各種日期寫法轉成 2026-10-09。支援民國年（115/10/09、1151009、115.10.09）、
    西元年（2026/10/9、20261009）與 Excel 的日期序號。無法辨識回傳 None。"""
    s = str(v or "").strip()
    if not s:
        return None
    s = s.split(" ")[0].split("T")[0]
    try:
        if re.fullmatch(r"\d{5}(\.0+)?", s):  # Excel 日期序號
            n = int(float(s))
            if 20000 <= n <= 80000:
                return (date(1899, 12, 30) + timedelta(days=n)).isoformat()
        if re.fullmatch(r"\d{8}", s):
            return date(int(s[:4]), int(s[4:6]), int(s[6:])).isoformat()
        if re.fullmatch(r"\d{6,7}", s):  # 民國 yyyMMdd / yyMMdd
            return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:])).isoformat()
        parts = re.split(r"[/\-.年月日]", s)
        parts = [x for x in parts if x]
        if len(parts) == 3 and all(x.isdigit() for x in parts):
            y, m, d = (int(x) for x in parts)
            if y < 1000:
                y += 1911
            return date(y, m, d).isoformat()
    except ValueError:
        return None
    return None


def map_rows(rows, header_idx, mapping, kind):
    records, problems = [], []
    carry = {}
    for line_no, r in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        rec = {}
        for field, col in mapping.items():
            if col is None or col == "":
                continue
            col = int(col)
            rec[field] = str(r[col]).strip() if col < len(r) else ""
        if not any(rec.values()):
            continue
        if any(TOTAL_ROW.fullmatch(_norm(c)) for c in r) and not (rec.get("item_code") or rec.get("code")):
            continue  # 報表的合計 / 小計列
        for f in FILL_DOWN.get(kind, []):
            if f in mapping and mapping[f] is not None:
                if rec.get(f):
                    carry[f] = rec[f]
                else:
                    rec[f] = carry.get(f, "")
        if kind == "history":
            if not (rec.get("item_code") or rec.get("item_name")):
                continue  # 只有表頭沒有品項的列（例如單據抬頭）
            d = norm_date(rec.get("doc_date"))
            if rec.get("doc_date") and not d:
                problems.append(f"第 {line_no} 列日期「{rec['doc_date']}」看不懂，已略過")
                continue
            rec["doc_date"] = d or ""
        missing = [f for f in REQUIRED[kind] if not rec.get(f)]
        if missing:
            problems.append(f"第 {line_no} 列缺少必填欄位，已略過")
            continue
        for f in NUMERIC & rec.keys():
            n = _num(rec[f])
            if rec[f] and n is None:
                problems.append(f"第 {line_no} 列「{rec[f]}」不是數字，當作空白")
            rec[f] = n
        records.append(rec)
    return records, problems


# ---------------------------------------------------------------- 寫入

def apply_items(conn, records, default_category):
    created = updated = moved = 0
    for rec in records:
        cat = rec.get("category")
        cat = cat if cat in CATEGORIES else default_category
        existing = conn.execute("SELECT * FROM items WHERE code = ?", (rec["code"],)).fetchone()
        if existing:
            data = dict(existing)
            for f in ("name", "spec", "unit", "location", "note", "safety_stock", "cost", "price"):
                if rec.get(f) not in (None, ""):
                    data[f] = rec[f]
            data["category"] = cat if rec.get("category") else data["category"]
            data["active"] = 1
            conn.execute("""UPDATE items SET name=?, spec=?, unit=?, category=?, location=?, note=?,
                            safety_stock=?, cost=?, price=?, active=1 WHERE id=?""",
                         (data["name"], data["spec"], data["unit"], data["category"], data["location"],
                          data["note"], data["safety_stock"] or 0, data["cost"] or 0, data["price"] or 0,
                          existing["id"]))
            item_id = existing["id"]
            updated += 1
        else:
            item_id = server.create_item(conn, {
                "code": rec["code"], "name": rec["name"], "category": cat,
                "spec": rec.get("spec", ""), "unit": rec.get("unit") or "個",
                "safety_stock": rec.get("safety_stock") or 0, "cost": rec.get("cost") or 0,
                "price": rec.get("price") or 0, "location": rec.get("location", ""), "note": rec.get("note", ""),
            })["id"]
            created += 1
        if rec.get("stock") is not None:
            diff = rec["stock"] - server.stock_of(conn, item_id)
            if abs(diff) > 1e-9:
                server.add_move(conn, item_id, diff, "舊系統轉入", note="匯入時的庫存數", allow_negative=True)
                moved += 1
    return {"created": created, "updated": updated, "stock_set": moved}


def apply_partners(conn, records, ptype):
    created = updated = 0
    for rec in records:
        note = rec.get("note", "")
        if rec.get("old_code"):
            note = (f"舊編號 {rec['old_code']}　" + note).strip()
        data = {"type": ptype, "name": rec["name"], "contact": rec.get("contact", ""),
                "phone": rec.get("phone", ""), "tax_id": rec.get("tax_id", ""),
                "address": rec.get("address", ""), "note": note}
        for extra in ("short_name", "fax", "email", "owner", "grp"):
            if rec.get(extra):
                data[extra] = rec[extra]
        existing = conn.execute("SELECT * FROM partners WHERE type = ? AND name = ?",
                                (ptype, rec["name"])).fetchone()
        if existing:
            merged = {k: (v if v else existing[k]) for k, v in data.items()}
            server.update_partner(conn, existing["id"], merged)
            conn.execute("UPDATE partners SET active = 1 WHERE id = ?", (existing["id"],))
            updated += 1
        else:
            server.create_partner(conn, data)
            created += 1
    return {"created": created, "updated": updated}


def apply_history(conn, records, kind):
    """匯入歷史交易。同一個檔案重複匯入不會重複（用內容做指紋）。
    客戶 / 廠商名稱在本系統找不到時自動建立，避免舊客戶遺失。"""
    ptype = "customer" if kind == "sales" else "supplier"
    partners = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM partners WHERE type = ?", (ptype,))}
    created = skipped = new_partners = 0
    seen = {}
    for rec in records:
        name = rec["partner"]
        if name not in partners:
            partners[name] = server.create_partner(conn, {
                "type": ptype, "name": name, "note": "由舊系統交易紀錄自動建立"})["id"]
            new_partners += 1
        key = "|".join(str(rec.get(f) if rec.get(f) is not None else "") for f in
                       ("doc_date", "doc_no", "partner", "item_code", "item_name", "spec", "qty", "unit_price", "amount"))
        seen[key] = seen.get(key, 0) + 1  # 同一張單同品項同數量出現兩次也要保留
        fp = hashlib.sha1(f"{kind}|{key}|{seen[key]}".encode()).hexdigest()
        cur = conn.execute(
            """INSERT OR IGNORE INTO history (kind, doc_date, doc_no, partner_id, partner_name, item_code, item_name,
                   spec, unit, qty, unit_price, amount, note, fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, rec["doc_date"], rec.get("doc_no", ""), partners[name], name, rec.get("item_code", ""),
             rec.get("item_name", ""), rec.get("spec", ""), rec.get("unit", ""), rec.get("qty"),
             rec.get("unit_price"), rec.get("amount"), rec.get("note", ""), fp))
        if cur.rowcount:
            created += 1
        else:
            skipped += 1
    return {"created": created, "skipped": skipped, "new_partners": new_partners}


def run_import(conn, body):
    """body: target (items/customer/supplier/sales_history/purchase_history), filename, data (base64), mapping?, header_row?,
    default_category?, commit (bool)。"""
    target = body.get("target")
    kinds = {"items": "items", "customer": "partners", "supplier": "partners",
             "sales_history": "history", "purchase_history": "history"}
    if target not in kinds:
        raise server.ApiError("請選擇要匯入的資料類型")
    kind = kinds[target]
    try:
        raw = base64.b64decode(body.get("data") or "")
    except ValueError:
        raise server.ApiError("檔案內容錯誤")
    if not raw:
        raise server.ApiError("請選擇檔案")
    rows = read_table(body.get("filename") or "", raw)
    if not rows:
        raise server.ApiError("檔案裡沒有資料")
    header_idx = body.get("header_row")
    header_idx = find_header(rows, kind) if header_idx in (None, "") else int(header_idx)
    if header_idx >= len(rows):
        raise server.ApiError("表頭列超出範圍")
    headers = [str(h).strip() for h in rows[header_idx]]
    mapping = body.get("mapping") or auto_mapping(headers, kind)
    mapping = {k: (int(v) if v not in (None, "") else None) for k, v in mapping.items()}
    records, problems = map_rows(rows, header_idx, mapping, kind)

    result = {
        "headers": headers,
        "header_row": header_idx,
        "fields": [{"key": f, "label": label} for f, label, _ in FIELDS[kind]],
        "mapping": mapping,
        "count": len(records),
        "sample": records[:8],
        "problems": problems[:30],
        "problem_count": len(problems),
        "missing_required": [f for f in REQUIRED[kind] if mapping.get(f) is None],
    }
    if body.get("commit"):
        if result["missing_required"]:
            raise server.ApiError("必填欄位還沒對應：" + "、".join(
                label for f, label, _ in FIELDS[kind] if f in result["missing_required"]))
        if not records:
            raise server.ApiError("沒有可以匯入的資料")
        if kind == "items":
            result["summary"] = apply_items(conn, records, body.get("default_category") or "原料")
        elif kind == "history":
            result["summary"] = apply_history(conn, records, target.split("_")[0])
        else:
            result["summary"] = apply_partners(conn, records, target)
    return result
