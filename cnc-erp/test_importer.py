"""舊系統匯入測試：python -m unittest test_importer"""
import base64
import csv
import io
import os
import struct
import tempfile
import unittest
import zipfile

import importer
import server

# 模擬舊系統匯出的報表：上面有抬頭、最後有合計列
ITEM_ROWS = [
    ["XX精密工業有限公司"],
    ["產品庫存明細表", "", "列印日期：115/10/09"],
    [],
    ["產品編號", "品名", "規格", "單位", "庫存量", "安全存量", "平均成本", "標準售價", "類別"],
    ["A6061-30", "鋁棒6061", "φ30×3M", "支", "35", "20", "850", "", "原料"],
    ["SH-001", "傳動軸", "φ28×120", "個", "1,200", "", "", "380", "成品"],
    ["T-EM10", "銑刀", "φ10 4刃", "支", "abc", "8", "950", "", "工具"],
    ["", "", "", "", "", "", "", "", ""],
    ["", "合計", "", "", "1235"],
]


def b64(raw):
    return base64.b64encode(raw).decode()


def make_csv(rows, encoding="cp950"):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode(encoding)


def make_xlsx(rows):
    strings, sheet_rows = [], []
    for ri, r in enumerate(rows, 1):
        cells = []
        for ci, v in enumerate(r):
            ref = chr(65 + ci) + str(ri)
            if v.replace(".", "", 1).isdigit():
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            elif v:
                strings.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(
            f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>")
    return buf.getvalue()


def make_dbf(fields, records):
    """fields: [(name, length)]，全部用字元型欄位，Big5 編碼。"""
    rec_len = 1 + sum(l for _, l in fields)
    hdr_len = 32 + 32 * len(fields) + 1
    out = bytearray(struct.pack("<BBBBIHH20x", 3, 126, 10, 9, len(records), hdr_len, rec_len))
    for name, length in fields:
        out += name.encode("cp950").ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([length, 0]) + b"\0" * 14
    out += b"\r"
    for deleted, values in records:
        out += b"*" if deleted else b" "
        for (_, length), v in zip(fields, values):
            out += v.encode("cp950").ljust(length, b" ")[:length]
    return bytes(out + b"\x1a")


class ImportTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        server.init_db(self.path)
        self.conn = server.connect(self.path)

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def run_import(self, **body):
        return importer.run_import(self.conn, body)

    def item(self, code):
        r = self.conn.execute("SELECT * FROM items WHERE code = ?", (code,)).fetchone()
        return dict(r, stock=server.stock_of(self.conn, r["id"])) if r else None

    def test_big5_csv_preview_detects_header_and_columns(self):
        r = self.run_import(target="items", filename="庫存.csv", data=b64(make_csv(ITEM_ROWS)))
        self.assertEqual(r["header_row"], 3)
        self.assertEqual(r["mapping"]["code"], 0)
        self.assertEqual(r["mapping"]["stock"], 4)
        self.assertEqual(r["mapping"]["cost"], 6)
        self.assertEqual(r["count"], 3)  # 空白列與合計列都略過
        self.assertEqual(r["problem_count"], 1)  # abc 不是數字
        self.assertIsNone(self.item("A6061-30"))  # 預覽不寫入

    def test_commit_creates_items_with_stock(self):
        r = self.run_import(target="items", filename="x.csv", data=b64(make_csv(ITEM_ROWS)),
                            default_category="刀具", commit=True)
        self.assertEqual(r["summary"]["created"], 3)
        shaft = self.item("SH-001")
        self.assertEqual(shaft["stock"], 1200)
        self.assertEqual(shaft["category"], "成品")
        self.assertEqual(shaft["price"], 380)
        tool = self.item("T-EM10")
        self.assertEqual(tool["category"], "刀具")  # 「工具」對不上，用預設
        self.assertEqual(tool["stock"], 0)

    def test_reimport_updates_instead_of_duplicating(self):
        data = b64(make_csv(ITEM_ROWS))
        self.run_import(target="items", filename="x.csv", data=data, commit=True)
        rows = [r[:] for r in ITEM_ROWS]
        rows[4][4] = "30"
        r = self.run_import(target="items", filename="x.csv", data=b64(make_csv(rows)), commit=True)
        self.assertEqual(r["summary"], {"created": 0, "updated": 3, "stock_set": 1})
        self.assertEqual(self.item("A6061-30")["stock"], 30)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)

    def test_xlsx(self):
        r = self.run_import(target="items", filename="庫存.xlsx", data=b64(make_xlsx(ITEM_ROWS)), commit=True)
        self.assertEqual(r["summary"]["created"], 3)
        self.assertEqual(self.item("A6061-30")["stock"], 35)

    def test_manual_mapping_overrides_auto(self):
        rows = [["貨品代碼", "貨品說明", "現貨"], ["P1", "螺絲", "10"]]
        r = self.run_import(target="items", filename="a.csv", data=b64(make_csv(rows)))
        self.assertTrue(r["missing_required"])
        with self.assertRaises(server.ApiError):
            self.run_import(target="items", filename="a.csv", data=b64(make_csv(rows)), commit=True)
        r = self.run_import(target="items", filename="a.csv", data=b64(make_csv(rows)), header_row=0,
                            mapping={"code": 0, "name": 1, "stock": 2}, commit=True)
        self.assertEqual(self.item("P1")["stock"], 10)

    def test_customers_from_dbf(self):
        raw = make_dbf([("客戶編號", 6), ("客戶簡稱", 20), ("統一編號", 8), ("電話", 14)], [
            (False, ["C001", "永豐機械", "12345678", "04-25678901"]),
            (True, ["C002", "已刪除公司", "", ""]),
            (False, ["C003", "台中自動化", "", "04-27890123"]),
        ])
        r = self.run_import(target="customer", filename="CUST.DBF", data=b64(raw), commit=True)
        self.assertEqual(r["summary"]["created"], 2)
        p = self.conn.execute("SELECT * FROM partners WHERE name = '永豐機械'").fetchone()
        self.assertEqual(p["type"], "customer")
        self.assertEqual(p["tax_id"], "12345678")
        self.assertIn("C001", p["note"])

    def test_utf8_csv_supplier(self):
        rows = [["廠商名稱", "聯絡人", "電話"], ["大成金屬", "陳先生", "04-1234"]]
        r = self.run_import(target="supplier", filename="s.csv", data=b64(make_csv(rows, "utf-8-sig")), commit=True)
        self.assertEqual(r["summary"]["created"], 1)

    def test_xls_gives_clear_message(self):
        with self.assertRaises(server.ApiError) as ctx:
            self.run_import(target="items", filename="old.xls", data=b64(b"\xd0\xcf\x11\xe0"))
        self.assertIn("另存新檔", str(ctx.exception))

    def test_api_route(self):
        old = server.DB_PATH
        server.DB_PATH = self.path
        try:
            status, r = server.handle_api("POST", "/api/import", {}, dict(
                target="items", filename="x.csv", data=b64(make_csv(ITEM_ROWS)), commit=True))
            self.assertEqual(status, 200, r)
            status, r = server.handle_api("POST", "/api/import", {}, dict(target="items", filename="x.csv", data=""))
            self.assertEqual(status, 400)
        finally:
            server.DB_PATH = old


# 舊系統「銷貨明細表」：同一張單只有第一行有單號/日期/客戶，日期是民國年，有小計列
SALES_ROWS = [
    ["XX精密工業有限公司  銷貨明細表"],
    ["日期", "單號", "客戶簡稱", "產品編號", "品名", "數量", "單價", "金額"],
    ["113/03/05", "S1130305001", "永豐機械", "SH-001", "傳動軸", "200", "360", "72,000"],
    ["", "", "", "SH-002", "軸套", "200", "45", "9,000"],
    ["", "", "", "", "小計", "", "", "81,000"],
    ["1140820", "S1140820003", "永豐機械", "SH-001", "傳動軸", "100", "380", "38,000"],
    ["2025/12/01", "S1141201001", "新客戶精機", "FL-01", "法蘭盤", "10", "1650", "16,500"],
    ["114/13/40", "BAD", "永豐機械", "SH-001", "傳動軸", "1", "1", "1"],
]


class HistoryImportTest(ImportTest):
    def import_sales(self, commit=True, rows=SALES_ROWS):
        return self.run_import(target="sales_history", filename="sales.csv", data=b64(make_csv(rows)), commit=commit)

    def test_norm_date(self):
        cases = {"113/03/05": "2024-03-05", "1140820": "2025-08-20", "2025/12/1": "2025-12-01",
                 "20251201": "2025-12-01", "114.1.9": "2025-01-09", "46000": "2025-12-09",
                 "2025-12-01 00:00:00": "2025-12-01", "114年1月9日": "2025-01-09", "abc": None, "114/13/40": None}
        for raw, want in cases.items():
            self.assertEqual(importer.norm_date(raw), want, raw)

    def test_preview_fills_down_and_skips_subtotals(self):
        r = self.import_sales(commit=False)
        self.assertEqual(r["count"], 4)
        second = r["sample"][1]
        self.assertEqual((second["doc_date"], second["doc_no"], second["partner"]),
                         ("2024-03-05", "S1130305001", "永豐機械"))
        self.assertEqual(r["problem_count"], 1)  # 114/13/40
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0], 0)

    def test_commit_creates_missing_customers_and_is_idempotent(self):
        server.create_partner(self.conn, dict(type="customer", name="永豐機械"))
        r = self.import_sales()
        self.assertEqual(r["summary"], {"created": 4, "skipped": 0, "new_partners": 1})
        r = self.import_sales()
        self.assertEqual(r["summary"], {"created": 0, "skipped": 4, "new_partners": 0})
        p = self.conn.execute("SELECT * FROM partners WHERE name = '新客戶精機'").fetchone()
        self.assertEqual(p["type"], "customer")

    def test_duplicate_lines_in_same_document_are_kept(self):
        rows = [SALES_ROWS[1], SALES_ROWS[2], SALES_ROWS[2]]
        self.assertEqual(self.import_sales(rows=rows)["summary"]["created"], 2)

    def test_history_shows_up_in_customer_summary_and_search(self):
        self.import_sales()
        pid = self.conn.execute("SELECT id FROM partners WHERE name = '永豐機械'").fetchone()[0]
        s = server.partner_summary(self.conn, pid)
        self.assertEqual(s["stats"]["first_date"], "2024-03-05")
        self.assertEqual(s["stats"]["last_date"], "2025-08-20")
        self.assertEqual(s["stats"]["amount"], 72000 + 9000 + 38000)
        top = s["stats"]["top_items"][0]
        self.assertEqual((top["item_code"], top["times"], top["last_price"]), ("SH-001", 2, 380))
        self.assertEqual([y["year"] for y in s["stats"]["by_year"]], ["2025", "2024"])
        h = server.list_history(self.conn, {"search": "法蘭"})
        self.assertEqual(h["count"], 1)
        self.assertEqual(h["rows"][0]["partner_name"], "新客戶精機")

    def test_last_prices_prefers_same_customer_and_includes_new_orders(self):
        self.import_sales()
        item = server.create_item(self.conn, dict(code="SH-001", name="傳動軸"))["id"]
        yf = self.conn.execute("SELECT id FROM partners WHERE name = '永豐機械'").fetchone()[0]
        other = self.conn.execute("SELECT id FROM partners WHERE name = '新客戶精機'").fetchone()[0]
        prices = server.last_prices(self.conn, {"item_id": item, "partner_id": yf})
        self.assertEqual([p["unit_price"] for p in prices], [380, 360])
        self.assertTrue(prices[0]["same_partner"])
        server.create_order(self.conn, dict(kind="sales", partner_id=other, order_date="2026-01-05",
                                            lines=[dict(item_id=item, qty=5, unit_price=400)]))
        prices = server.last_prices(self.conn, {"item_id": item, "partner_id": other})
        self.assertEqual((prices[0]["source"], prices[0]["unit_price"], prices[0]["same_partner"]), ("本系統", 400, 1))
        # 歷史查詢也會看到本系統的新訂單
        h = server.list_history(self.conn, {"item_code": "SH-001"})
        self.assertEqual(h["count"], 3)


if __name__ == "__main__":
    unittest.main()
