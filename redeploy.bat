@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ===============================================================================
echo        NAM AN MARKET - UNIFIED MERCHANT PORTAL (NAM-UMP)
echo              DOCKER DEPLOY AND REDEPLOY AUTOMATION SCRIPT
echo ===============================================================================
echo.

:: 1. Kiem tra Docker Engine
echo [1/6] Kiem tra Docker Daemon...
docker info >nul 2>&1
if errorlevel 1 (
    echo [LOI] Docker chua duoc khoi dong hoac chua duoc cai dat!
    echo Vui long mo ung dung Docker Desktop va thu lai.
    exit /b 1
)
echo       [OK] Docker Daemon dang hoat dong binh thuong.
echo.

:: 2. Kiem tra file cau hinh .env
echo [2/6] Kiem tra file cau hinh moi truong .env...
if not exist ".env" (
    if exist ".env.example" (
        echo       [CANH BAO] Chua co file .env, tu dong tao ban sao tu .env.example...
        copy ".env.example" ".env" >nul
        echo       [OK] Da tao file .env tu .env.example.
    ) else (
        echo       [LOI] Khong tim thay .env va .env.example!
        exit /b 1
    )
) else (
    echo       [OK] File .env da san sang.
)
echo.

:: 3. Dung va don dep container cu
echo [3/6] Dung va thu hoi containers cu naman_portal_app, naman_portal_nginx...
docker compose down --remove-orphans
echo       [OK] Da giai phong cac container cu.
echo.

:: 4. Build lai Docker Image
echo [4/6] Dong goi va build lai image voi ma nguon moi nhat...
if "%1"=="--clean" (
    echo       Che do clean build: Khong su dung cache...
    docker compose build --no-cache
) else if "%1"=="--no-cache" (
    echo       Che do no-cache: Build lai toan bo packages...
    docker compose build --no-cache
) else (
    docker compose build
)

if errorlevel 1 (
    echo.
    echo [LOI] Qua trinh build Docker Image that bai!
    exit /b 1
)
echo       [OK] Build Docker Image thanh cong.
echo.

:: 5. Khoi dong containers
echo [5/6] Khoi dong cac service container Detached mode...
docker compose up -d
if errorlevel 1 (
    echo.
    echo [LOI] Khong the khoi chay containers qua docker compose!
    exit /b 1
)
echo       [OK] Containers da duoc khoi dong thanh cong.
echo.

:: 6. Kiem tra trang thai va Health check probe
echo [6/6] Dang doi service backend san sang va thuc hien Health check...
set ATTEMPTS=0
set MAX_ATTEMPTS=15
set HEALTHY=0

:HEALTH_LOOP
set /a ATTEMPTS+=1
ping -n 3 127.0.0.1 >nul

curl.exe -f -s http://127.0.0.1:3001/api/v1/health >nul 2>&1
if not errorlevel 1 (
    set HEALTHY=1
    goto HEALTH_DONE
)

if !ATTEMPTS! LSS !MAX_ATTEMPTS! (
    echo       Dang cho container san sang: Lan thu !ATTEMPTS! tren !MAX_ATTEMPTS!...
    goto HEALTH_LOOP
)

:HEALTH_DONE
echo.
echo ===============================================================================
if !HEALTHY! EQU 1 (
    echo   [THANH CONG] NAM AN MERCHANT PORTAL DA SAN SANG PHUC VU!
) else (
    echo   [CANH BAO] Container da chay nhung Health Check chua phan hoi 200 OK.
    echo   Vui long kiem tra log chi tiet bang lenh: docker compose logs -f
)
echo ===============================================================================
echo.
echo TRANG THAI CONTAINERS HIEN TAI:
docker compose ps
echo.
echo DIA CHI TRUY CAP HE THONG:
echo   * Cong Quan Tri Portal:    http://localhost:3001
echo   * Quan Ly Ton Kho va Menu: http://localhost:3001/admin/inventory
echo   * Quan Ly Don Hang:        http://localhost:3001/admin/orders
echo   * Tai lieu API Swagger:    http://localhost:3001/docs
echo   * Health Check Probe:      http://localhost:3001/api/v1/health
echo.
echo CAC LENH TIEN ICH KHI CAN:
echo   * Xem log thoi gian thuc:  docker compose logs -f app
echo   * Xem log cua Nginx:       docker compose logs -f nginx
echo   * Dung toan bo he thong:   docker compose down
echo   * Rebuild sach hoan toan:  redeploy.bat --clean
echo ===============================================================================
echo.
endlocal
