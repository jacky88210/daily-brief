# 設定「開機自動啟動」與桌面捷徑（做法 B：系統裝在辦公室電腦）
# 用法：雙擊 install-autostart.bat；取消：雙擊 uninstall-autostart.bat
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'CNC 進銷存.lnk'
$desktopUrl = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CNC 進銷存.url'

if ($Remove) {
    Remove-Item $startupLink -ErrorAction SilentlyContinue
    Write-Host '已取消開機自動啟動（桌面捷徑保留）。目前正在執行的系統，關掉它的視窗即可停止。'
    exit 0
}

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($startupLink)
$lnk.TargetPath = Join-Path $dir 'server-autostart.bat'
$lnk.WorkingDirectory = $dir
$lnk.WindowStyle = 7   # 縮小執行
$lnk.Description = 'CNC 進銷存系統（開機自動啟動）'
$lnk.Save()

Set-Content -Path $desktopUrl -Encoding ASCII -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8080/`r`n"

Start-Process -FilePath (Join-Path $dir 'server-autostart.bat') -WorkingDirectory $dir -WindowStyle Minimized

Write-Host ''
Write-Host '完成！'
Write-Host ' 1. 以後這台電腦開機登入後，系統會自動在背景（工作列縮小的視窗）執行。'
Write-Host ' 2. 桌面多了「CNC 進銷存」捷徑，點兩下就能打開系統。'
Write-Host ' 3. 系統已經開始執行了。請不要關掉工作列上那個「CNC 進銷存（背景執行中）」視窗。'
Write-Host ''
Write-Host '提醒：請到「Windows 設定 > 系統 > 電源」把「睡眠」設為「永不」，否則電腦睡著時手機連不到、也不會備份。'
