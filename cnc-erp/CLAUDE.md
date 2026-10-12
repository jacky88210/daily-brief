# CNC 進銷存系統：維護說明（給 Claude Code）

台灣中小型 CNC 加工廠用的進銷存 + 報價 + 生產排程系統。使用者是老闆、主管、業務、現場師傅，
**不是工程師**：畫面文字一律**繁體中文、台灣用語**（料號、圖號、交期、出貨單、統編、月結對帳單…），錯誤訊息要講人話、告訴他下一步怎麼做。

正式資料（客戶、報價、圖面）是**公司機密**，維護時最重要的是「不弄壞、不外流」。

## 執行與測試

```bash
python server.py --demo --port 8080          # 用示範資料開起來（資料庫：同資料夾 cnc.db）
CNC_DB=/tmp/demo.db python server.py --demo  # 用另一個資料庫，不碰正式資料（建議開發時都這樣）
python -m unittest discover -p "test_*.py"   # 全部測試（Windows：test.bat）
```

- 改完一定要跑**全部測試**，全過才算完成。新功能要加測試（照現有 `test_*.py` 的寫法：暫存資料庫、`server.connect(path)`）。
- 有改畫面就實際開瀏覽器點一遍（有 Playwright 可以截圖檢查；手機版用 iPhone 尺寸看）。
- 確認相容 Python 3.7：`uvx vermin --no-tips -t=3.7- --violations *.py`（例如不能用 `dict | dict`、`str.removeprefix`、海象運算子 `:=`）。

## 架構

| 檔案 | 內容 |
|---|---|
| `server.py` | HTTP 伺服器（標準函式庫 `http.server`）、路由表 `ROUTES`、資料表 `SCHEMA` + `MIGRATIONS`、訂單 / 出貨 / 庫存 / 工單 / 歷史查詢 / 總覽 / 設定 / 備份 / 登入檢查 `_auth` |
| `accounts.py` | 使用者帳號、角色權限（`ADMIN_ONLY`、`FLOOR_ALLOWED`）、存取紀錄的動作名稱（`WRITE_LABELS`） |
| `quotes.py` | 圖面報價、估價公式、附件（`files/` 資料夾）、報價轉訂單 |
| `production.py` | 機台、製程路線、工序、排程估算 `schedule()`、現場報工、外包 |
| `documents.py` | 出貨單 / 對帳單資料（列印版面在前端） |
| `importer.py` | Excel / CSV / DBF 匯入、欄位自動對應、歷史交易 |
| `lingyue.py` | 讀凌越備份檔（多個 FoxPro .DBF/.FPT 串成一個檔）並轉入 |
| `demo_data.py` | 示範資料（只在資料庫是空的時候寫入） |
| `static/` | 前端：`index.html` + `app.js`（原生 JS，**沒有建置步驟、沒有框架**）+ `style.css` |
| `*.bat`、`*.ps1` | Windows 啟動 / 安裝 / 更新腳本 |
| `Dockerfile` | 裝在 Synology NAS（Container Manager）用 |

資料（**不在 git 裡**）：`cnc.db`（SQLite）、`files/`（圖面附件）、`pre-update/`（更新前快照）、備份 zip。

## 必須遵守的規則

1. **只用 Python 標準函式庫**，不加任何 pip 套件（使用者電腦只有裝 Python，沒人會處理套件問題）。前端也不加 npm / 框架 / CDN。
2. **資料庫只能「加」不能「改 / 刪」**：新表用 `CREATE TABLE IF NOT EXISTS`；新欄位加到 `server.MIGRATIONS`（啟動時自動 `ALTER TABLE ADD COLUMN`）。
   不要 DROP / RENAME 欄位或表、不要改既有欄位意義。舊資料庫一定要能直接升級（參考 `test_documents.test_existing_orders_stay_untaxed_after_upgrade`）。
3. **新增 API 時三件事**：加到 `ROUTES`；想清楚權限（管理員專用就加進 `accounts.ADMIN_ONLY`；現場人員可用才加進 `FLOOR_ALLOWED`，預設現場人員**不能**用）；
   會修改資料的動作加一筆 `accounts.WRITE_LABELS`，存取紀錄才看得到是誰做的。
4. **絕對不要把正式資料放進 git 或傳到網路**：`cnc.db`、`files/`、`*.zip`、凌越備份 `*.001` 都在 `.gitignore`。測試資料一律用假資料。
5. 金額、稅額用 `server.tax_breakdown()`（四捨五入到元，不用 Python 的銀行家捨入）；日期是 `YYYY-MM-DD` 字串、台灣當地時間。
6. 所有寫入在 `handle_api` 的交易裡，出錯整筆回滾；庫存只透過 `add_move()` 異動（庫存 = 異動加總，可追溯）。
7. Windows 腳本：`.bat` 用 **UTF-8 + CRLF** 並在開頭 `chcp 65001`；`.ps1` 用 **UTF-8 with BOM + CRLF**。不要在 `.bat` 裡用會被誤判的語法（括號區塊內的 `%變數%` 要注意展開時機）。
8. 畫面要在**手機**上也能用（寬度 390px）；現場報工頁按鈕要大。
9. 改版時更新 `VERSION`（例：1.0.0 → 1.1.0）與 `CHANGELOG.md`（用老闆看得懂的話寫「這版多了什麼」）。

## 部署到工廠電腦

- 系統跑在辦公室電腦（做法 B），開機由 `server-autostart.bat` 自動啟動，每天自動備份到兩個位置（NAS + 外接硬碟）。
- 更新：工廠電腦雙擊 `update.bat` → 自動快照備份 → `git pull` → 跑全部測試（沒過自動退回舊版）→ 重啟系統。
  所以**推上去的版本一定要全部測試通過**。
- 出問題要還原：`pre-update/` 或備份位置裡的 zip 解壓縮，把 `cnc.db` 與 `files/` 放回程式資料夾。

## 常見需求的做法

- 單據版面：`static/app.js` 的「單據列印」區塊（`docPage()` 共同版型），紙張大小在 `PAPERS`。
- 總覽新增卡片：後端 `server.dashboard()` / `dashboard_trends()` 加資料，前端 `views.dashboard`。
- 新的設定項目：加到 `server.PUBLIC_SETTINGS`，前端 `views.tools` 的表單。
- 凌越欄位對應：`lingyue.py` 的 `run()`；Excel 匯入欄位別名：`importer.FIELDS`。
