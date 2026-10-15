@echo off
chcp 936 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 闲鱼自动回复系统 - 一键启动
cls

set STEPTOTAL=6

echo ============================================================
echo               闲鱼自动回复系统 - 一键启动
echo ============================================================
echo.
echo   这个程序会自动帮你完成所有事情，期间不用做任何操作。
echo   首次启动需要下载并编译，大约 10 到 30 分钟，
echo   中途请不要关闭这个黑色窗口。
echo.
echo ============================================================
echo.

rem ============ 步骤 1 / 6：检查文件是否完整 ============
echo [1/6] 检查文件是否完整...
if not exist "docker-compose.yml" (
    echo.
    echo   错误：找不到 docker-compose.yml
    echo   说明：这个程序必须和项目文件放在一起运行。
    echo         请确认你是双击的压缩包里解压出来的「2-一键启动」。
    goto :fail
)
if not exist ".env" (
    echo.
    echo   错误：找不到 .env 配置文件
    echo   说明：压缩包里应该带这个文件。如果解压后没有，
    echo         可能是杀毒软件误删了，请重新解压一次压缩包。
    goto :fail
)
echo       文件完整。
echo.

rem ============ 步骤 2 / 6：检查 Docker 是否安装 ============
echo [2/6] 检查 Docker Desktop...
where docker >nul 2>&1
if errorlevel 1 (
    echo.
    echo   错误：没有检测到 Docker Desktop。
    echo.
    echo   请先安装 Docker Desktop，步骤：
    echo     1. 打开 https://www.docker.com/products/docker-desktop/
    echo     2. 下载 Windows 版并安装，安装时勾选 WSL 2 那一项
    echo     3. 装完重启电脑，再双击本文件
    echo.
    echo   也可以先双击「1-环境检测」，里面有详细说明。
    goto :fail
)
echo       Docker Desktop 已安装。
echo.

rem ============ 步骤 3 / 6：配置国内镜像加速器 ============
echo [3/6] 配置国内镜像加速器...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\docker-mirror.ps1"
if errorlevel 1 (
    echo       已写入加速器配置，随后启动的 Docker 会直接生效。
) else (
    echo       加速器已就绪，无需改动。
)
echo.

rem ============ 步骤 4 / 6：启动 Docker 引擎 ============
echo [4/6] 启动 Docker 引擎...
docker info >nul 2>&1
if not errorlevel 1 (
    echo       Docker 引擎已在运行。
    goto :engine_ready
)

echo       Docker 没在运行，正在自动启动，请稍等...
set "DOCKER_EXE=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
if not exist "!DOCKER_EXE!" set "DOCKER_EXE=%LocalAppData%\Docker\Docker Desktop.exe"
if not exist "!DOCKER_EXE!" set "DOCKER_EXE=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
if not exist "!DOCKER_EXE!" (
    echo.
    echo   错误：找到了 docker 命令，但找不到 Docker Desktop 主程序。
    echo         请手动打开 Docker Desktop，等它左下角变成绿色，
    echo         再双击本文件一次。
    goto :fail
)
start "" "!DOCKER_EXE!"

set /a WT=0
:engine_wait
docker info >nul 2>&1
if not errorlevel 1 goto :engine_ready
set /a WT+=1
if !WT! GTR 60 (
    echo.
    echo   错误：等了 3 分钟 Docker 还没起来。
    echo         请手动双击桌面上的 Docker Desktop 图标，
    echo         等它左下角显示绿色 "Engine running"，再双击本文件。
    goto :fail
)
if !WT! EQU 1 echo       正在等待 Docker 引擎就绪...
if !WT! EQU 20 echo       还在启动中，第一次启动会慢一些...
if !WT! EQU 40 echo       快好了，请再等一下...
ping -n 4 127.0.0.1 >nul 2>&1
goto :engine_wait

:engine_ready
echo       Docker 引擎已就绪。
echo.

rem ============ 步骤 5 / 6：构建并启动全部服务 ============
echo [5/6] 构建并启动所有服务...
echo.
echo ------------------------------------------------------------
echo   首次启动要下载依赖并编译，会比较久，请耐心等待。
echo   下面滚动的都是正常过程，看到很多行是正常的。
echo ------------------------------------------------------------
echo.
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo   错误：启动失败。常见原因：
    echo     1. 网络问题，导致下载超时 ---- 重新双击本文件再试一次即可
    echo     2. 9000 等端口被别的软件占用 ---- 双击「1-环境检测」查一下
    echo     3. 磁盘空间不足 ---- 至少留 20 GB 空闲
    echo.
    echo   如果反复失败，请把上面红色或 error 的几行截图给我。
    goto :fail
)
echo.
echo       所有服务已启动。
echo.

rem ============ 步骤 6 / 6：等待网页可访问 ============
echo [6/6] 等待网页就绪...
set /a WR=0
:web_wait
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:9000/health' -UseBasicParsing -TimeoutSec 5; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :web_ready
set /a WR+=1
if !WR! GTR 40 (
    echo.
    echo   提醒：网页暂时还没响应，服务可能还在初始化。
    echo         你可以在浏览器里手动打开 http://localhost:9000 试试，
    echo         如果打不开，等 2 分钟再刷新。
    goto :web_open
)
if !WR! EQU 1 echo       正在等数据库和服务初始化...
if !WR! EQU 15 echo       还在初始化，第一次会比较慢...
ping -n 4 127.0.0.1 >nul 2>&1
goto :web_wait

:web_ready
echo       网页已就绪。

:web_open
echo.
echo ============================================================
echo                     启动成功！
echo ============================================================
echo.
echo   请在浏览器里打开：   http://localhost:9000
echo.
echo   初始账号：admin
echo   初始密码：admin123
echo   （登录后请尽快在「个人设置」里改掉密码）
echo.
echo   建议把 http://localhost:9000 添加到浏览器收藏夹。
echo.
echo   关闭服务：双击「3-一键停止」
echo   查看运行状态：双击「4-查看状态」
echo.
echo   现在自动为你打开浏览器...
ping -n 3 127.0.0.1 >nul 2>&1
start "" http://localhost:9000/
echo.
echo ============================================================
echo   这个窗口可以关闭了。关闭窗口不会停止服务。
echo ============================================================
echo.
pause >nul
exit /b 0

:fail
echo.
echo ============================================================
echo   按任意键关闭本窗口
echo ============================================================
pause >nul
exit /b 1
