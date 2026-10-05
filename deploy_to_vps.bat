@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ===============================================================================
echo        NAM AN MARKET - AUTOMATED REMOTE DEPLOY TO PRODUCTION VPS
echo ===============================================================================
echo.

echo [1/3] Day ma nguon moi nhat len GitHub...
git push origin main
if errorlevel 1 (
    echo [LOI] Khong the push code len GitHub!
    exit /b 1
)
echo       [OK] Da dong bo ma nguon len GitHub.
echo.

echo [2/3] Ket noi toi VPS (103.175.248.250:22022) va ra lenh redeploy...
ssh -o ConnectTimeout=10 naman-vps "cd /d C:\python\naman_merchant_portal && git pull origin main && cmd.exe /c redeploy.bat"
if errorlevel 1 (
    echo.
    echo [THONG BAO] Thu lai qua cong 22 mac dinh...
    ssh -p 22 -o ConnectTimeout=10 Administrator@103.175.248.250 "cd /d C:\python\naman_merchant_portal && git pull origin main && cmd.exe /c redeploy.bat"
)

echo.
echo ===============================================================================
echo  [HOAN TAT] QUA TRINH REDEPLOY PRODUCTION DA HOAN TAT!
echo ===============================================================================
