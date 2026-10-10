"""凌越備份轉入測試：python -m unittest test_lingyue

測試資料照真實凌越備份的格式與欄位製作（Visual FoxPro 資料表 + .FPT 備註，
外面包一層「0x01 + 13 字元檔名 + 4 bytes 長度 + 1 byte」的容器）。"""
import os
import struct
import tempfile
import unittest

import lingyue
import server


class FptWriter:
    BLOCK = 64

    def __init__(self):
        self.blocks = bytearray()
        self.next = 512 // self.BLOCK

    def add(self, text):
        if not text:
            return 0
        data = text.encode("cp950")
        chunk = struct.pack(">II", 1, len(data)) + data
        chunk += b"\0" * (-len(chunk) % self.BLOCK)
        block = self.next
        self.blocks += chunk
        self.next += len(chunk) // self.BLOCK
        return block

    def bytes(self):
        head = struct.pack(">I", self.next) + b"\0\0" + struct.pack(">H", self.BLOCK) + b"\0" * 504
        return head + bytes(self.blocks)


def vfp_table(fields, rows, deleted=()):
    """fields: [(名稱, 型別, 長度, 小數)]；rows: list[dict]。回傳 (dbf, fpt 或 None)。"""
    fpt = FptWriter() if any(f[1] == "M" for f in fields) else None
    rec_len = 1 + sum(f[2] for f in fields)
    hdr_len = 32 + 32 * len(fields) + 1 + 263
    out = bytearray(struct.pack("<BBBBIHH20x", 0x30, 125, 10, 9, len(rows), hdr_len, rec_len))
    for name, t, ln, dec in fields:
        out += name.encode().ljust(11, b"\0") + t.encode() + b"\0" * 4 + bytes([ln, dec]) + b"\0" * 14
    out += b"\r" + b"\0" * 263
    for i, row in enumerate(rows):
        out += b"*" if i in deleted else b" "
        for name, t, ln, dec in fields:
            v = row.get(name)
            if t == "C":
                out += (v or "").encode("cp950").ljust(ln)[:ln]
            elif t == "N":
                out += (b" " * ln if v is None else ("%.*f" % (dec, v)).rjust(ln).encode())
            elif t == "D":
                out += (v or "").replace("-", "").encode().ljust(8)
            elif t == "L":
                out += b"T" if v else b"F"
            elif t == "I":
                out += struct.pack("<i", v or 0)
            elif t == "M":
                out += struct.pack("<i", fpt.add(v or ""))
    out += b"\x1a"
    return bytes(out), (fpt.bytes() if fpt else None)


def container(files, broken_size=False):
    out = bytearray()
    for name, data in files:
        size = len(data) + (7 if broken_size else 0)
        out += b"\x01" + name.encode().ljust(13) + struct.pack("<I", size) + b"\0" + data
    return bytes(out)


C, N, D, L, I, M = "C", "N", "D", "L", "I", "M"
CUST_FIELDS = [("CLASS", C, 1, 0), ("NO", C, 10, 0), ("NAME", C, 60, 0), ("S_NAME", C, 10, 0),
               ("ADDR1", C, 60, 0), ("UNIFORM", C, 10, 0), ("ADDR2", C, 60, 0), ("TEL", C, 35, 0),
               ("FAX", C, 20, 0), ("CONTACT", C, 20, 0), ("PRESIDT", C, 20, 0), ("REM", M, 4, 0),
               ("E_MAIL", C, 50, 0), ("CDATE", D, 8, 0)]
STOCK_FIELDS = [("NO", C, 20, 0), ("NAME", C, 60, 0), ("SPEC", C, 20, 0), ("UNIT", C, 8, 0),
                ("SIZE", C, 10, 0), ("LOCATE", C, 10, 0), ("PRICE1", N, 16, 2), ("STD_AVE", N, 16, 2),
                ("AVE_PRICE", N, 16, 2), ("QTY", N, 12, 2), ("SAFE_QTY", N, 12, 2), ("REM", M, 4, 0),
                ("FIGNAME", C, 30, 0)]
SLIP_FIELDS = [("CLASS", C, 1, 0), ("SLIP_FG", C, 1, 0), ("DATE", D, 8, 0), ("NO", C, 10, 0),
               ("CT_NO", C, 10, 0), ("CT_NAME", C, 30, 0), ("TOT", N, 16, 2), ("TAL_REC", I, 4, 0)]
SLIPDT_FIELDS = [("CLASS", C, 1, 0), ("SLIP_FG", C, 1, 0), ("NO", C, 10, 0), ("CT_NO", C, 10, 0),
                 ("DATE", D, 8, 0), ("SK_NO", C, 20, 0), ("NAME", C, 40, 0), ("QTY", N, 12, 2),
                 ("UNIT", C, 8, 0), ("PRICE", N, 16, 2), ("STOT", N, 16, 2), ("REM", M, 4, 0)]
ORDER_FIELDS = [("CLASS", C, 1, 0), ("NO", C, 10, 0), ("DATE1", D, 8, 0), ("DATE2", D, 8, 0),
                ("CT_NO", C, 10, 0), ("CT_NAME", C, 30, 0), ("TOT", N, 16, 2)]
ORDERDT_FIELDS = [("CLASS", C, 1, 0), ("NO", C, 10, 0), ("SK_NO", C, 20, 0), ("NAME", C, 40, 0),
                  ("QTY", N, 12, 2), ("PRICE", N, 16, 2), ("DL_QTY", N, 12, 2)]
SYS_FIELDS = [("VER_HI", C, 1, 0), ("COMP_NAME", C, 30, 0)]

CUSTOMERS = [
    dict(CLASS="1", NO="C001", NAME="永豐機械股份有限公司", S_NAME="永豐", UNIFORM="12345678",
         TEL="04-25678901", FAX="04-25678902", CONTACT="王經理", ADDR1="台中市西屯區工業一路1號",
         REM="月結60天，交貨前先電話通知", E_MAIL="wang@example.com", CDATE="2010-05-01"),
    dict(CLASS="1", NO="C002", NAME="台中自動化設備", S_NAME="台中自動", TEL="04-27890123"),
    dict(CLASS="2", NO="V001", NAME="大成金屬材料行", S_NAME="大成", CONTACT="陳先生", TEL="04-23456789"),
    dict(CLASS="1", NO="C999", NAME="已刪除客戶", S_NAME="刪除"),
]
STOCK = [
    dict(NO="SH-001", NAME="傳動軸", SPEC="φ28×120", UNIT="個", LOCATE="C-01", PRICE1=380, AVE_PRICE=200,
         QTY=150, SAFE_QTY=50, REM="客戶圖號 YF-2231 Rev.C", FIGNAME="SH001.DWG"),
    dict(NO="AL6061-30", NAME="鋁棒6061", SPEC="φ30", SIZE="3M", UNIT="支", AVE_PRICE=850, QTY=35, SAFE_QTY=20),
    dict(NO="OLD-001", NAME="停產零件", UNIT="個", QTY=0),
    dict(NO="NEG-01", NAME="帳上負庫存", UNIT="個", QTY=-3),
]
LINES = [
    # A|1 = 銷貨（客戶，售價遠高於成本）
    dict(CLASS="A", SLIP_FG="1", NO="S0001", CT_NO="C001", DATE="2019-03-05", SK_NO="SH-001", NAME="傳動軸",
         QTY=200, UNIT="個", PRICE=360, STOT=72000, REM="急件"),
    dict(CLASS="A", SLIP_FG="1", NO="S0001", CT_NO="C001", DATE="2019-03-05", SK_NO="SH-001", NAME="傳動軸",
         QTY=200, UNIT="個", PRICE=360, STOT=72000),  # 同一張單同品項同數量兩行：都要保留
    dict(CLASS="A", SLIP_FG="1", NO="S0002", CT_NO="C002", DATE="2024-08-20", SK_NO="SH-001", NAME="傳動軸",
         QTY=100, UNIT="個", PRICE=380, STOT=38000),
    dict(CLASS="A", SLIP_FG="1", NO="S0003", CT_NO="C999", DATE="2015-01-10", SK_NO="SH-001", NAME="傳動軸",
         QTY=10, UNIT="個", PRICE=350, STOT=3500),
    # A|2 = 銷貨退回（只有一筆）
    dict(CLASS="A", SLIP_FG="2", NO="R0001", CT_NO="C001", DATE="2019-03-20", SK_NO="SH-001", NAME="傳動軸",
         QTY=5, UNIT="個", PRICE=360, STOT=1800),
    # B|1 = 進貨（廠商，價格約等於成本）
    dict(CLASS="B", SLIP_FG="1", NO="P0001", CT_NO="V001", DATE="2019-02-01", SK_NO="AL6061-30", NAME="鋁棒6061",
         QTY=50, UNIT="支", PRICE=820, STOT=41000),
    dict(CLASS="B", SLIP_FG="1", NO="P0002", CT_NO="V001", DATE="2024-06-01", SK_NO="AL6061-30", NAME="鋁棒6061",
         QTY=30, UNIT="支", PRICE=880, STOT=26400),
]
SLIPS = [dict(CLASS=l["CLASS"], SLIP_FG=l["SLIP_FG"], DATE=l["DATE"], NO=l["NO"], CT_NO=l["CT_NO"])
         for l in LINES]


def make_backup(broken_size=False):
    files = []
    for name, fields, rows, deleted in [
        ("0CUST", CUST_FIELDS, CUSTOMERS, {3}),
        ("0STOCK", STOCK_FIELDS, STOCK, ()),
        ("0SLIP", SLIP_FIELDS, SLIPS, ()),
        ("0SLIPDT", SLIPDT_FIELDS, LINES, ()),
        ("0ORDER", ORDER_FIELDS, [dict(CLASS="1", NO="O0001", DATE1="2026-10-01", DATE2="2026-10-20",
                                       CT_NO="C001", CT_NAME="永豐", TOT=7600)], ()),
        ("0ORDERDT", ORDERDT_FIELDS, [dict(CLASS="1", NO="O0001", SK_NO="SH-001", NAME="傳動軸",
                                           QTY=20, PRICE=380, DL_QTY=5)], ()),
        ("0SYS", SYS_FIELDS, [dict(VER_HI="9", COMP_NAME="XX精密工業")], ()),
    ]:
        dbf, fpt = vfp_table(fields, rows, deleted)
        files.append((name + ".DBF", dbf))
        if fpt:
            files.append((name + ".FPT", fpt))
        files.append((name + ".CDX", b"\0" * 1024))  # 索引檔，用不到
    return container(files, broken_size)


TYPE_MAP = {"A|1": "sales", "A|2": "sales_return", "B|1": "purchase"}
PARTNER_MAP = {"1": "customer", "2": "supplier"}


class LingyueTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.init_db(self.path)
        self.conn = server.connect(self.path)
        self.raw = make_backup()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_container_and_dbf_with_memo(self):
        files = lingyue.read_container(self.raw)
        self.assertIn("0CUST.DBF", files)
        self.assertIn("0CUST.FPT", files)
        bk = lingyue.Backup(self.raw)
        cust = bk.table("CUST")
        self.assertEqual(len(cust), 3)  # 已刪除的不算
        self.assertEqual(cust[0]["REM"], "月結60天，交貨前先電話通知")
        self.assertEqual(cust[0]["CDATE"], "2010-05-01")
        self.assertEqual(bk.table("STOCK")[0]["QTY"], 150)
        self.assertEqual(bk.table("SLIP")[0]["TAL_REC"], 0)

    def test_container_fallback_when_size_field_disagrees(self):
        bk = lingyue.Backup(make_backup(broken_size=True))
        self.assertEqual(len(bk.table("SLIPDT")), len(LINES))
        self.assertEqual(bk.table("CUST")[0]["REM"], "月結60天，交貨前先電話通知")

    def test_not_a_lingyue_backup(self):
        with self.assertRaises(server.ApiError):
            lingyue.Backup(b"hello world" * 10)

    def test_preview_guesses_types_and_classes(self):
        p = lingyue.preview(self.raw)
        self.assertEqual(p["company"], "XX精密工業")
        guesses = {t["key"]: t["guess"] for t in p["types"]}
        self.assertEqual(guesses, {"A|1": "sales", "B|1": "purchase", "A|2": "skip"})
        sales = next(t for t in p["types"] if t["key"] == "A|1")
        self.assertEqual((sales["docs"], sales["lines"], sales["date_from"], sales["date_to"]),
                         (3, 4, "2015-01-10", "2024-08-20"))
        self.assertEqual(sales["top_partners"][0], "永豐")
        self.assertEqual({c["class"]: c["guess"] for c in p["partner_classes"]},
                         {"1": "customer", "2": "supplier"})
        self.assertEqual(p["items"]["count"], 4)
        self.assertEqual(p["open_orders"][0]["lines"][0]["delivered"], 5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_full_import(self):
        s = lingyue.run(self.conn, self.raw, dict(type_map=TYPE_MAP, partner_map=PARTNER_MAP))
        self.assertEqual(s["partners"]["customer"]["created"], 2)
        self.assertEqual(s["partners"]["supplier"]["created"], 1)
        yf = self.conn.execute("SELECT * FROM partners WHERE name = '永豐機械股份有限公司'").fetchone()
        self.assertEqual((yf["type"], yf["tax_id"], yf["contact"]), ("customer", "12345678", "王經理"))
        for part in ("C001", "簡稱 永豐", "傳真 04-25678902", "月結60天"):
            self.assertIn(part, yf["note"])

        def item(code):
            r = self.conn.execute("SELECT * FROM items WHERE code = ?", (code,)).fetchone()
            return dict(r, stock=server.stock_of(self.conn, r["id"]))
        sh = item("SH-001")
        self.assertEqual((sh["category"], sh["stock"], sh["cost"], sh["price"], sh["safety_stock"]),
                         ("成品", 150, 200, 380, 50))
        self.assertIn("YF-2231", sh["note"])
        al = item("AL6061-30")
        self.assertEqual((al["category"], al["spec"]), ("原料", "φ30　3M"))
        self.assertEqual(item("OLD-001")["category"], "其他")
        self.assertEqual(item("NEG-01")["stock"], -3)

        self.assertEqual(s["history"]["sales"]["created"], 5)
        self.assertEqual(s["history"]["sales"]["new_partners"], 1)  # 已刪除的客戶 C999 也保留紀錄
        self.assertEqual(s["history"]["purchase"]["created"], 2)
        ret = self.conn.execute("SELECT * FROM history WHERE doc_no = 'R0001'").fetchone()
        self.assertEqual((ret["qty"], ret["amount"]), (-5, -1800))
        self.assertIn("銷貨退回", ret["note"])

        summary = server.partner_summary(self.conn, yf["id"])
        self.assertEqual(summary["stats"]["first_date"], "2019-03-05")
        self.assertEqual(summary["stats"]["amount"], 72000 * 2 - 1800)
        prices = server.last_prices(self.conn, {"item_id": sh["id"], "partner_id": yf["id"]})
        self.assertEqual(prices[0]["unit_price"], 360)

    def test_reimport_is_idempotent(self):
        opts = dict(type_map=TYPE_MAP, partner_map=PARTNER_MAP)
        lingyue.run(self.conn, self.raw, opts)
        s = lingyue.run(self.conn, self.raw, opts)
        self.assertEqual(s["history"]["sales"]["created"], 0)
        self.assertEqual(s["items"]["created"], 0)
        self.assertEqual(s["partners"]["customer"]["created"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0], 7)
        sh = self.conn.execute("SELECT id FROM items WHERE code = 'SH-001'").fetchone()[0]
        self.assertEqual(server.stock_of(self.conn, sh), 150)

    def test_skipping_parts_and_types(self):
        s = lingyue.run(self.conn, self.raw, dict(type_map={"A|1": "sales"}, partner_map=PARTNER_MAP,
                                                  parts={"history": True}))
        self.assertNotIn("items", s)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0], 4)

    def test_api_with_path(self):
        fd, backup = tempfile.mkstemp(suffix=".001")
        os.close(fd)
        with open(backup, "wb") as f:
            f.write(self.raw)
        old = server.DB_PATH
        server.DB_PATH = self.path
        try:
            status, p = server.handle_api("POST", "/api/lingyue/preview", {}, {"path": backup})
            self.assertEqual(status, 200, p)
            status, r = server.handle_api("POST", "/api/lingyue/import", {}, dict(
                path=backup, type_map=TYPE_MAP, partner_map=PARTNER_MAP))
            self.assertEqual(status, 200, r)
            status, r = server.handle_api("POST", "/api/lingyue/preview", {}, {"path": backup + ".nope"})
            self.assertEqual(status, 400)
        finally:
            server.DB_PATH = old
            os.remove(backup)


if __name__ == "__main__":
    unittest.main()
