"""示範資料：只有在資料庫完全沒有品項時才會寫入，不會覆蓋正式資料。"""
from datetime import date, timedelta

import server


def load_demo(path=None):
    conn = server.connect(path)
    try:
        if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            return False
        with conn:
            d = lambda n: (date.today() + timedelta(days=n)).isoformat()
            items = [
                dict(code="RM-AL6061-D30", name="鋁棒 6061", category="原料", spec="Ø30 × 3000mm", unit="支",
                     safety_stock=20, cost=850, opening_stock=35, location="A-01"),
                dict(code="RM-S45C-D50", name="中碳鋼棒 S45C", category="原料", spec="Ø50 × 3000mm", unit="支",
                     safety_stock=10, cost=2200, opening_stock=6, location="A-02"),
                dict(code="RM-SUS304-PL10", name="不鏽鋼板 SUS304", category="原料", spec="10t × 300 × 300",
                     unit="片", safety_stock=15, cost=1300, opening_stock=40, location="A-03"),
                dict(code="FG-SHAFT-001", name="傳動軸", category="成品", spec="Ø28 × 120 公差 ±0.01",
                     unit="個", price=380, opening_stock=50, location="C-01"),
                dict(code="FG-FLANGE-002", name="法蘭盤", category="成品", spec="SUS304 外徑 280",
                     unit="個", price=1650, location="C-02"),
                dict(code="TL-EM-D10", name="鎢鋼銑刀", category="刀具", spec="Ø10 4刃", unit="支",
                     safety_stock=8, cost=950, opening_stock=5, location="T-01"),
                dict(code="CS-COOLANT", name="切削液", category="耗材", spec="20L", unit="桶",
                     safety_stock=3, cost=2800, opening_stock=4, location="B-01"),
            ]
            ids = {it["code"]: server.create_item(conn, it)["id"] for it in items}
            sup = server.create_partner(conn, dict(type="supplier", name="大成金屬材料行", contact="陳先生",
                                                   phone="04-2345-6789"))["id"]
            server.create_partner(conn, dict(type="supplier", name="精工刀具有限公司", contact="林小姐",
                                             phone="04-2233-4455"))
            cus = server.create_partner(conn, dict(type="customer", name="永豐機械股份有限公司", contact="王經理",
                                                   phone="04-2567-8901", tax_id="12345678"))["id"]
            cus2 = server.create_partner(conn, dict(type="customer", name="台中自動化設備", contact="張先生",
                                                    phone="04-2789-0123"))["id"]

            server.create_order(conn, dict(kind="purchase", partner_id=sup, due_date=d(3), lines=[
                dict(item_id=ids["RM-S45C-D50"], qty=20, unit_price=2150)]))
            so1 = server.create_order(conn, dict(kind="sales", partner_id=cus, due_date=d(5), customer_po="YF-PO-8812",
                                                 lines=[dict(item_id=ids["FG-SHAFT-001"], qty=200, unit_price=380)]))
            so2 = server.create_order(conn, dict(kind="sales", partner_id=cus2, due_date=d(-2), lines=[
                dict(item_id=ids["FG-FLANGE-002"], qty=10, unit_price=1650)]))
            server.create_order(conn, dict(kind="sales", partner_id=cus2, lines=[
                dict(item_id=ids["FG-SHAFT-001"], qty=30, unit_price=400)]))
            # 已出貨 30 個傳動軸，收了部分款
            order1 = server.get_order(conn, so1["id"])
            server.deliver_order(conn, so1["id"], dict(lines=[dict(line_id=order1["lines"][0]["id"], qty=30)]))
            server.add_payment(conn, so1["id"], dict(amount=5000, note="訂金"))

            wo = server.create_work_order(conn, dict(item_id=ids["FG-SHAFT-001"], qty=200, sales_order_id=so1["id"],
                                                     machine="CNC-02 車床", due_date=d(4), materials=[
                                                         dict(item_id=ids["RM-AL6061-D30"], qty_per_unit=0.05)]))
            server.start_work_order(conn, wo["id"])
            server.create_work_order(conn, dict(item_id=ids["FG-FLANGE-002"], qty=10, sales_order_id=so2["id"],
                                                machine="CNC-05 綜合加工機", due_date=d(1), materials=[
                                                    dict(item_id=ids["RM-SUS304-PL10"], qty_per_unit=1)]))
            # 模擬從舊系統匯入的過去三年銷貨 / 進貨紀錄
            import importer
            hist = []
            for i, (days, no, cust, code, name, qty, price) in enumerate([
                (-980, "S1120215001", "永豐機械股份有限公司", "FG-SHAFT-001", "傳動軸", 300, 350),
                (-700, "S1121120004", "永豐機械股份有限公司", "FG-SHAFT-001", "傳動軸", 200, 360),
                (-700, "S1121120004", "永豐機械股份有限公司", "SL-010", "軸套（已停產）", 200, 45),
                (-420, "S1130826002", "永豐機械股份有限公司", "FG-SHAFT-001", "傳動軸", 500, 365),
                (-150, "S1140512001", "永豐機械股份有限公司", "FG-SHAFT-001", "傳動軸", 300, 375),
                (-600, "S1130301003", "台中自動化設備", "FG-FLANGE-002", "法蘭盤", 20, 1600),
                (-90, "S1140710002", "台中自動化設備", "FG-FLANGE-002", "法蘭盤", 15, 1650),
                (-800, "S1120620001", "宏達精密（已無往來）", "FG-PIN-007", "定位銷", 2000, 12),
            ]):
                hist.append(dict(doc_date=d(days), doc_no=no, partner=cust, item_code=code, item_name=name,
                                 qty=qty, unit_price=price, amount=qty * price))
            importer.apply_history(conn, hist, "sales")
            importer.apply_history(conn, [dict(doc_date=d(-200), doc_no="P1140320001", partner="大成金屬材料行",
                                               item_code="RM-AL6061-D30", item_name="鋁棒 6061", qty=50,
                                               unit_price=820, amount=41000)], "purchase")
        return True
    finally:
        conn.close()
