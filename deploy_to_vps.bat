@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ===============================================================================
echo        NAM AN MARKET - AUTOMATED REMOTE DEPLOY TO PRODUCTION VPS
echo ===============================================================================
echo.

:: 1. Day code len GitHub
echo [1/3] Day ma nguon moi nhat len GitHub...
git push origin main
if errorlevel 1 (
    echo [LOI] Khong the push code len GitHub!
    exit /b 1
)
echo       [OK] Da dong bo ma nguon len GitHub.
echo.

:: 2. Goi SSH vao VPS va thuc thi redeploy
echo [2/3] Ket noi toi Windows VPS (103.175.248.250) va tu dong redeploy...
ssh -i "%USERPROFILE%\.ssh\id_ed25519_naman_ump" -p 22 -o StrictHostKeyChecking=no Administrator@103.175.248.250 "cd /d C:\uploadIMG\docker\ump && cmd.exe /c redeploy.bat"
if errorlevel 1 (
    echo.
    echo [CANH BAO] Thu lai qua ssh config host (naman-vps)...
    ssh naman-vps "cd /d C:\uploadIMG\docker\ump && cmd.exe /c redeploy.bat"
)
echo.

:: 3. Kiem tra tinh trang sau khi deploy
echo [3/3] Kiem tra Health Check Production...
curl.exe -m 10 -s https://ump.namanmarket.com/api/v1/health
echo.

echo ===============================================================================
echo  [HOAN TAT] QUA TRINH REDEPLOY PRODUCTION DA XONG!
echo  Truy cap trang quan tri: https://ump.namanmarket.com/admin/orders
echo ===============================================================================
echo.
