@echo off
chcp 936 >nul 2>&1
cd /d "%~dp0"
title 闲鱼自动回复系统 - 重启服务
cls

echo ============================================================
echo               闲鱼自动回复系统 - 重启服务
echo ============================================================
echo.
echo   这个操作会先把所有服务停掉，再重新启动一遍。
echo   你的数据不会丢，闲鱼账号也不用重新登录。
echo.
echo   正在重启，请稍等...
echo.

where docker >nul 2>&1
if errorlevel 1 (
    echo   没有检测到 Docker Desktop，无法重启。
    echo   请先双击「2-一键启动」。
    goto :end
)

docker info >nul 2>&1
if errorlevel 1 (
    echo   Docker 引擎没在运行，请直接双击「2-一键启动」。
    goto :end
)

echo   [1/2] 停止服务...
docker compose down
if errorlevel 1 (
    echo.
    echo   停止失败，请把本窗口截图给我。
    goto :end
)
echo.

echo   [2/2] 重新启动...
docker compose up -d
if errorlevel 1 (
    echo.
    echo   启动失败。建议双击「1-环境检测」查一下环境，
    echo   或者把本窗口截图给我。
    goto :end
)
echo.

echo   ----------------------------------------------------------
echo   重启完成。等 30 秒左右，再打开 http://localhost:9000
echo   ----------------------------------------------------------

:end
echo.
pause >nul
