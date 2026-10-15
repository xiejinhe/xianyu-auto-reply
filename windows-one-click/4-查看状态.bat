@echo off
chcp 936 >nul 2>&1
cd /d "%~dp0"
title 闲鱼自动回复系统 - 查看状态
cls

echo ============================================================
echo               闲鱼自动回复系统 - 运行状态
echo ============================================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
    echo   没有检测到 Docker Desktop，服务不可能是运行状态。
    echo   请先安装 Docker Desktop，再双击「2-一键启动」。
    goto :end
)

docker info >nul 2>&1
if errorlevel 1 (
    echo   Docker 引擎没在运行，所以服务都没在跑。
    echo   想启动请双击「2-一键启动」。
    goto :end
)

echo   一、容器列表
echo   ------------------------------------------------------------
docker compose ps
echo   ------------------------------------------------------------
echo.
echo   怎么看：
echo     State 显示 running / Up 就是在运行，exited 就是停了。
echo     正常情况 6 个容器都应该是运行状态。
echo.

echo   二、网页访问测试
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:9000/health' -UseBasicParsing -TimeoutSec 5; if ($r.StatusCode -eq 200) { Write-Output '   [ 正常 ] 网页可以打开  http://localhost:9000' } else { Write-Output ('   [ 异常 ] 网页返回状态码 ' + $r.StatusCode) } } catch { Write-Output '   [ 异常 ] 网页打不开，服务可能还在启动，或者出错了' }"
echo.

echo   三、最近日志（40 行）
echo   ------------------------------------------------------------
docker compose logs --tail=40
echo   ------------------------------------------------------------
echo.
echo   上面这些日志一般不用看懂。
echo   如果服务不正常，把这个窗口整个截图发我，我来判断。
echo.
echo   如果只是某个服务没起来，可以先双击「5-重启服务」试试。
echo.

:end
echo ============================================================
echo   按任意键关闭本窗口
echo ============================================================
pause >nul
