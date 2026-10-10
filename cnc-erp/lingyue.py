"""從凌越進銷存的備份檔（例如 wstkBKUP.001）直接轉入。

凌越的備份檔是把多個 FoxPro 資料表（.DBF，備註在 .FPT）串成一個檔案：
每個檔案前面是 0x01 + 13 個字元的檔名 + 4 bytes 檔案長度，接著就是檔案內容。

用到的資料表：
    CUST    客戶 / 廠商（CLASS 區分）
    STOCK   產品 / 材料與目前庫存
    SLIP    單據表頭（CLASS + SLIP_FG 區分單據種類）
    SLIPDT  單據明細
    ORDER / ORDERDT  未結訂單（只列出來給人工重開）

單據種類與客戶 / 廠商的代碼意義每家設定可能不同，所以先「預覽 + 猜測」，
由使用者在畫面上確認對應後才寫入。
"""
import collections
import re
import statistics
import struct
from datetime import date

import importer
import server

ENTRY_NAME = re.compile(r"[0-9A-Za-z_~$\-]{1,9}\.[A-Za-z0-9]{1,3}")
ENTRY_SCAN = re.compile(rb"\x01([0-9A-Za-z_~$\-]{1,9}\.[A-Za-z0-9]{1,3}) {0,12}")
DBF_VERSIONS = {0x02, 0x03, 0x30, 0x31, 0x32, 0x43, 0x63, 0x83, 0x8B, 0x8E, 0xCB, 0xF5, 0xFB}

KIND_LABELS = {"sales": "銷貨", "sales_return": "銷貨退回", "purchase": "進貨",
               "purchase_return": "進貨退出", "skip": "不匯入"}


# ---------------------------------------------------------------- 讀檔

def read_container(raw):
    """拆開備份檔，回傳 {檔名(大寫): bytes}。"""
    files = {}
    pos = 0
    # 先照「檔名 + 長度」一個一個讀
    while pos + 19 <= len(raw) and raw[pos] == 1:
        name = raw[pos + 1:pos + 14].rstrip(b" \0").decode("latin-1")
        if not ENTRY_NAME.fullmatch(name):
            break
        size = struct.unpack("<I", raw[pos + 14:pos + 18])[0]
        start, end = pos + 19, pos + 19 + size
        if end > len(raw):
            break
        files[name.upper()] = raw[start:end]
        pos = end
    if pos >= len(raw) - 16 and files:
        return files
    # 長度欄位對不上時的備案：用檔名位置切割
    files = {}
    hits = [m for m in ENTRY_SCAN.finditer(raw)
            if raw[m.start() + 1:m.start() + 14].rstrip(b" \0") == m.group(1)]
    for i, m in enumerate(hits):
        start = m.start() + 19
        end = hits[i + 1].start() if i + 1 < len(hits) else len(raw)
        data = raw[start:end]
        if m.group(1).upper().endswith(b".DBF") and len(data) >= 32:
            n_rec, hdr_len, rec_len = struct.unpack("<IHH", data[4:12])
            data = data[:hdr_len + n_rec * rec_len + 1]
        files[m.group(1).decode("latin-1").upper()] = data
    return files


def _text(b):
    b = b.replace(b"\0", b" ")
    try:
        return b.decode("cp950").strip()
    except UnicodeDecodeError:
        return b.decode("cp950", errors="replace").strip()


def memo_reader(fpt):
    """FoxPro .FPT 備註檔。回傳 函式(區塊編號) -> 文字。"""
    if not fpt or len(fpt) < 512:
        return lambda block: ""
    block_size = struct.unpack(">H", fpt[6:8])[0] or 64

    def read(block):
        off = block * block_size
        if block <= 0 or off + 8 > len(fpt):
            return ""
        typ, length = struct.unpack(">II", fpt[off:off + 8])
        if typ != 1 or length > len(fpt):
            return ""
        return _text(fpt[off + 8:off + 8 + length])
    return read


def read_dbf(raw, memo=None):
    """讀 dBASE / FoxPro / Visual FoxPro 資料表，回傳 list[dict]（欄位名稱大寫）。"""
    if len(raw) < 32 or raw[0] not in DBF_VERSIONS:
        return []
    n_rec, hdr_len, rec_len = struct.unpack("<IHH", raw[4:12])
    fields, pos = [], 32
    while pos + 32 <= min(hdr_len, len(raw)) and raw[pos] != 0x0D:
        name = raw[pos:pos + 11].split(b"\0")[0].decode("latin-1").strip().upper()
        fields.append((name, chr(raw[pos + 11]), raw[pos + 16], raw[pos + 17]))
        pos += 32
    memo = memo or (lambda block: "")
    out = []
    for i in range(n_rec):
        off = hdr_len + i * rec_len
        rec = raw[off:off + rec_len]
        if len(rec) < rec_len:
            break
        if rec[:1] == b"*":  # 已刪除
            continue
        p, row = 1, {}
        for name, t, ln, dec in fields:
            v = rec[p:p + ln]
            p += ln
            if t == "C":
                row[name] = _text(v)
            elif t in "NF":
                s = v.strip().decode("latin-1")
                try:
                    row[name] = float(s) if s else None
                except ValueError:
                    row[name] = None
            elif t == "D":
                s = v.strip().decode("latin-1")
                try:
                    row[name] = date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat() if len(s) == 8 else ""
                except ValueError:
                    row[name] = ""
            elif t == "L":
                row[name] = v[:1] in (b"T", b"t", b"Y", b"y")
            elif t == "I":
                row[name] = struct.unpack("<i", v)[0] if ln == 4 else None
            elif t == "B":
                row[name] = struct.unpack("<d", v)[0] if ln == 8 else None
            elif t == "Y":
                row[name] = struct.unpack("<q", v)[0] / 10000 if ln == 8 else None
            elif t == "T":
                if ln == 8:
                    day = struct.unpack("<i", v[:4])[0]
                    row[name] = date.fromordinal(day - 1721425).isoformat() if day > 1721425 else ""
                else:
                    row[name] = ""
            elif t in "MGW":
                if ln == 4:
                    block = struct.unpack("<i", v)[0]
                else:
                    s = v.strip().decode("latin-1")
                    block = int(s) if s.isdigit() else 0
                row[name] = memo(block) if t == "M" else ""
            # 其他型別（例如 VFP 的 _NullFlags）略過
        out.append(row)
    return out


class Backup:
    """拆好的凌越備份。prefix 是檔名前面的資料集代號（通常是 0）。"""

    def __init__(self, raw, prefix=None):
        self.files = read_container(raw)
        self.prefixes = sorted(n[:-len("STOCK.DBF")] for n in self.files if n.endswith("STOCK.DBF"))
        if not self.prefixes:
            raise server.ApiError("這個檔案裡找不到凌越的產品資料（STOCK.DBF），可能不是凌越的備份檔")
        self.prefix = prefix if prefix in self.prefixes else self.prefixes[0]
        self._cache = {}

    def table(self, name):
        if name not in self._cache:
            base = self.prefix + name
            raw = self.files.get(base + ".DBF")
            if raw is None:
                self._cache[name] = []
            else:
                fpt = self.files.get(base + ".FPT")
                self._cache[name] = read_dbf(raw, memo_reader(fpt))
        return self._cache[name]

    def counts(self):
        out = []
        for n in sorted(self.files):
            if n.startswith(self.prefix) and n.endswith(".DBF"):
                raw = self.files[n]
                out.append({"name": n[len(self.prefix):-4],
                            "records": struct.unpack("<I", raw[4:8])[0] if len(raw) >= 8 else 0})
        return out


# ---------------------------------------------------------------- 分析與猜測

def _type_key(r):
    return f"{r.get('CLASS', '')}|{r.get('SLIP_FG', '')}"


def _cost(item):
    for f in ("AVE_PRICE", "STD_AVE"):
        if item.get(f):
            return item[f]
    return None


def analyze(bk):
    cust = bk.table("CUST")
    stock = bk.table("STOCK")
    slips = bk.table("SLIP")
    lines = bk.table("SLIPDT")
    cust_class = {c["NO"]: c.get("CLASS", "") for c in cust}
    cust_name = {c["NO"]: c.get("S_NAME") or c.get("NAME") for c in cust}
    cost = {s["NO"]: _cost(s) for s in stock}

    types = collections.OrderedDict()
    for ln in lines:
        t = types.setdefault(_type_key(ln), {
            "key": _type_key(ln), "class": ln.get("CLASS", ""), "slip_fg": ln.get("SLIP_FG", ""),
            "docs": set(), "lines": 0, "amount": 0.0, "partners": collections.Counter(),
            "date_from": "", "date_to": "", "ratios": []})
        t["docs"].add(ln.get("NO"))
        t["lines"] += 1
        t["amount"] += ln.get("STOT") or 0
        t["partners"][ln.get("CT_NO", "")] += 1
        d = ln.get("DATE") or ""
        if d:
            t["date_from"] = min(t["date_from"] or d, d)
            t["date_to"] = max(t["date_to"], d)
        c = cost.get(ln.get("SK_NO"))
        if c and ln.get("PRICE"):
            t["ratios"].append(ln["PRICE"] / c)
    # 只有表頭沒有明細的單據種類也列出來
    for s in slips:
        types.setdefault(_type_key(s), {
            "key": _type_key(s), "class": s.get("CLASS", ""), "slip_fg": s.get("SLIP_FG", ""),
            "docs": set(), "lines": 0, "amount": 0.0, "partners": collections.Counter(),
            "date_from": "", "date_to": "", "ratios": []})

    # 每種單據主要是跟哪一類（CUST.CLASS）往來
    for t in types.values():
        cls = collections.Counter()
        for no, n in t["partners"].items():
            cls[cust_class.get(no, "?")] += n
        t["partner_class"] = cls.most_common(1)[0][0] if cls else ""
        t["ratio"] = statistics.median(t["ratios"]) if t["ratios"] else None

    guess_types, guess_classes = _guess(types, cust_class)
    return {
        "types": [{
            "key": t["key"], "class": t["class"], "slip_fg": t["slip_fg"], "docs": len(t["docs"]),
            "lines": t["lines"], "amount": t["amount"], "date_from": t["date_from"], "date_to": t["date_to"],
            "top_partners": [cust_name.get(no) or no for no, _ in t["partners"].most_common(5)],
            "price_cost_ratio": t["ratio"], "guess": guess_types.get(t["key"], "skip"),
        } for t in sorted(types.values(), key=lambda t: -t["lines"])],
        "partner_classes": [{
            "class": cls, "count": n,
            "samples": [c.get("S_NAME") or c.get("NAME") for c in cust if c.get("CLASS", "") == cls][:6],
            "guess": guess_classes.get(cls, "customer"),
        } for cls, n in sorted(collections.Counter(c.get("CLASS", "") for c in cust).items())],
    }


def _guess(types, cust_class):
    """猜哪種單據是銷貨、哪種是進貨，以及哪類是客戶、哪類是廠商。

    依據：同一類往來對象（CUST.CLASS）的單據歸成一組；
    售價 / 成本比較高、或總金額較大的那一組是銷貨，另一組是進貨；
    每組裡筆數最多的那種單據當主要單據，其他種類先不匯入，請使用者決定。"""
    groups = collections.defaultdict(list)
    for t in types.values():
        if t["lines"]:
            groups[t["partner_class"]].append(t)
    if not groups:
        return {}, {}

    def group_ratio(ts):
        r = [x for t in ts for x in t["ratios"]]
        return statistics.median(r) if r else None

    ranked = sorted(groups.items(), key=lambda kv: -sum(t["lines"] for t in kv[1]))[:2]
    if len(ranked) == 2:
        (ca, ta), (cb, tb) = ranked
        ra, rb = group_ratio(ta), group_ratio(tb)
        if ra and rb and abs(ra - rb) / max(ra, rb) > 0.1:
            sales_first = ra > rb
        else:
            sales_first = sum(t["amount"] for t in ta) >= sum(t["amount"] for t in tb)
        sales_cls, sales_ts, purch_cls, purch_ts = (ca, ta, cb, tb) if sales_first else (cb, tb, ca, ta)
        guess_types = {max(sales_ts, key=lambda t: t["lines"])["key"]: "sales",
                       max(purch_ts, key=lambda t: t["lines"])["key"]: "purchase"}
        guess_classes = {sales_cls: "customer", purch_cls: "supplier"}
    else:
        # 所有單據都跟同一類往來：筆數最多的當銷貨，其他請使用者決定
        cls, ts = ranked[0]
        ts = sorted(ts, key=lambda t: -t["lines"])
        guess_types = {ts[0]["key"]: "sales"}
        guess_classes = {cls: "customer"}
    return guess_types, guess_classes


def preview(raw, prefix=None):
    bk = Backup(raw, prefix)
    stock = bk.table("STOCK")
    info = analyze(bk)
    orders = bk.table("ORDER")
    order_lines = bk.table("ORDERDT")
    company = (bk.table("SYS") or [{}])[0].get("COMP_NAME", "")
    info.update({
        "prefix": bk.prefix,
        "prefixes": bk.prefixes,
        "company": company,
        "tables": bk.counts(),
        "items": {"count": len(stock),
                  "with_stock": sum(1 for s in stock if (s.get("QTY") or 0) != 0),
                  "sample": [{"code": s["NO"], "name": s.get("NAME"), "spec": s.get("SPEC"), "unit": s.get("UNIT"),
                              "qty": s.get("QTY"), "cost": _cost(s), "price": s.get("PRICE1")} for s in stock[:5]]},
        "partners": len(bk.table("CUST")),
        "open_orders": [{
            "no": o.get("NO"), "class": o.get("CLASS"), "date": o.get("DATE1"), "due": o.get("DATE2"),
            "partner": o.get("CT_NAME"), "total": o.get("TOT"),
            "lines": [{"code": l.get("SK_NO"), "name": l.get("NAME"), "qty": l.get("QTY"),
                       "delivered": l.get("DL_QTY"), "price": l.get("PRICE")}
                      for l in order_lines if l.get("NO") == o.get("NO") and l.get("CLASS") == o.get("CLASS")],
        } for o in orders],
    })
    return info


# ---------------------------------------------------------------- 寫入

def _join(*parts):
    return "　".join(p for p in parts if p)


def run(conn, raw, options):
    """options: prefix, partner_map {CLASS: customer|supplier|skip}, type_map {key: sales|...|skip},
    default_category, parts {items, partners, history}"""
    bk = Backup(raw, options.get("prefix"))
    parts = options.get("parts") or {"items": True, "partners": True, "history": True}
    partner_map = options.get("partner_map") or {}
    type_map = options.get("type_map") or {}
    summary = {}

    # 客戶 / 廠商
    cust = bk.table("CUST")
    no_to_name = {}
    partner_records = {"customer": [], "supplier": []}
    for c in cust:
        ptype = partner_map.get(c.get("CLASS", ""), "customer")
        name = c.get("NAME") or c.get("S_NAME") or c.get("NO")
        no_to_name[c["NO"]] = name
        if ptype not in partner_records or not name:
            continue
        partner_records[ptype].append({
            "name": name, "old_code": c.get("NO", ""), "contact": c.get("CONTACT", ""),
            "phone": c.get("TEL", ""), "tax_id": c.get("UNIFORM", ""),
            "address": c.get("ADDR1", "") or c.get("ADDR2", ""),
            "note": _join(f"簡稱 {c['S_NAME']}" if c.get("S_NAME") and c.get("S_NAME") != name else "",
                          f"傳真 {c['FAX']}" if c.get("FAX") else "",
                          f"負責人 {c['PRESIDT']}" if c.get("PRESIDT") else "",
                          f"Email {c['E_MAIL']}" if c.get("E_MAIL") else "",
                          f"送貨地址 {c['ADDR2']}" if c.get("ADDR2") and c.get("ADDR1") else "",
                          c.get("REM", "")),
        })
    if parts.get("partners"):
        summary["partners"] = {t: importer.apply_partners(conn, recs, t) for t, recs in partner_records.items()}

    # 交易紀錄（先算出每個品項是賣出還是買進，用來判斷分類）
    sold, bought = set(), set()
    history = {"sales": [], "purchase": []}
    for ln in bk.table("SLIPDT"):
        kind = type_map.get(_type_key(ln), "skip")
        if kind == "skip":
            continue
        base = "sales" if kind.startswith("sales") else "purchase"
        sign = -1 if kind.endswith("return") else 1
        (sold if base == "sales" else bought).add(ln.get("SK_NO"))
        qty, amount = ln.get("QTY"), ln.get("STOT")
        history[base].append({
            "doc_date": ln.get("DATE") or "", "doc_no": ln.get("NO", ""),
            "partner": no_to_name.get(ln.get("CT_NO")) or ln.get("CT_NO") or "（未知）",
            "item_code": ln.get("SK_NO", ""), "item_name": ln.get("NAME", ""), "unit": ln.get("UNIT", ""),
            "qty": None if qty is None else sign * qty, "unit_price": ln.get("PRICE"),
            "amount": None if amount is None else sign * amount,
            "note": _join(KIND_LABELS[kind] if sign < 0 else "", ln.get("REM", "")),
        })
    if parts.get("history"):
        summary["history"] = {k: importer.apply_history(conn, recs, k) for k, recs in history.items() if recs}

    # 產品 / 材料與庫存
    if parts.get("items"):
        default_cat = options.get("default_category") or "其他"
        records = []
        for s in bk.table("STOCK"):
            if not s.get("NO"):
                continue
            cat = "成品" if s["NO"] in sold else "原料" if s["NO"] in bought else default_cat
            spec = _join(s.get("SPEC", ""), s.get("SIZE", ""), s.get("COLOR", ""))
            records.append({
                "code": s["NO"], "name": s.get("NAME") or s["NO"], "category": cat, "spec": spec,
                "unit": s.get("UNIT") or "個", "stock": s.get("QTY") or 0,
                "safety_stock": max(s.get("SAFE_QTY") or 0, 0), "cost": max(_cost(s) or 0, 0),
                "price": max(s.get("PRICE1") or 0, 0), "location": s.get("LOCATE", ""),
                "note": _join(f"圖檔 {s['FIGNAME']}" if s.get("FIGNAME") else "", s.get("REM", "")),
            })
        summary["items"] = importer.apply_items(conn, records, default_cat)
    return summary
