@echo off
chcp 936 >nul 2>&1
cd /d "%~dp0"
title 闲鱼自动回复系统 - 一键停止
cls

echo ============================================================
echo               闲鱼自动回复系统 - 一键停止
echo ============================================================
echo.
echo   要停止的服务：
echo     MySQL 数据库、Redis 缓存、后端、WebSocket、定时任务、网页
echo.
echo   说明：停止不会删除你的数据。
echo         闲鱼账号、聊天记录、回复规则都保存在数据卷里，
echo         下次双击「2-一键启动」会自动接着用。
echo.
echo   正在停止，请稍等...
echo.

where docker >nul 2>&1
if errorlevel 1 (
    echo   没有检测到 Docker Desktop，服务本来就没在运行。
    goto :end
)

docker info >nul 2>&1
if errorlevel 1 (
    echo   Docker 引擎没在运行，服务本来就已经停止了。
    goto :end
)

docker compose down
if errorlevel 1 (
    echo.
    echo   停止时有报错。如果反复出现，把本窗口截图给我。
) else (
    echo.
    echo   ----------------------------------------------------------
    echo   已全部停止。想再启动，双击「2-一键启动」即可。
    echo   ----------------------------------------------------------
)

:end
echo.
pause >nul
