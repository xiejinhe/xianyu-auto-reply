@echo off
chcp 936 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 闲鱼自动回复系统 - 环境检测
cls

set FAILCOUNT=0
set WARNCOUNT=0
set VTBAD=
set DOCKERMISSING=

echo ============================================================
echo               闲鱼自动回复系统 - 环境检测
echo ============================================================
echo.
echo   本程序只做检查，不会修改你的电脑。
echo   看到 [ X ] 的项目，请按最后一节的说明处理，
echo   处理完再双击本文件重新检测一次。
echo.
echo ============================================================
echo   一、系统环境
echo ============================================================
echo.

rem ---------- 操作系统 ----------
set OSN=读取失败
for /f "usebackq tokens=*" %%i in (`powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).Caption"`) do set OSN=%%i
echo   [ OK ] 操作系统   : !OSN!

rem ---------- 系统位数 ----------
set ARCH=读取失败
for /f "usebackq tokens=*" %%i in (`powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).OSArchitecture"`) do set ARCH=%%i
echo !ARCH! | findstr /C:"64" >nul
if errorlevel 1 (
    echo   [ X  ] 系统位数   : !ARCH!
    echo          ^-^> 需要 64 位系统，32 位跑不了
    set /a FAILCOUNT+=1
) else (
    echo   [ OK ] 系统位数   : !ARCH!
)

rem ---------- 内存 ----------
set MEMGB=0
for /f "usebackq tokens=*" %%i in (`powershell -NoProfile -Command "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)"`) do set MEMGB=%%i
for /f "tokens=1 delims=." %%a in ("!MEMGB!") do set MEMINT=%%a
if !MEMINT! LSS 8 (
    echo   [ X  ] 内存       : !MEMGB! GB
    echo          ^-^> 需要至少 8 GB，建议 16 GB
    set /a FAILCOUNT+=1
) else (
    echo   [ OK ] 内存       : !MEMGB! GB
)

rem ---------- 磁盘 ----------
set DRV=%~d0
set DRV=!DRV::=!
set FREEGB=0
for /f "usebackq tokens=*" %%i in (`powershell -NoProfile -Command "[math]::Round((Get-PSDrive !DRV!).Free/1GB,0)"`) do set FREEGB=%%i
for /f "tokens=1 delims=." %%a in ("!FREEGB!") do set FREEINT=%%a
if !FREEINT! LSS 20 (
    echo   [ X  ] 磁盘剩余   : !DRV! 盘仅剩 !FREEGB! GB
    echo          ^-^> 需要至少 20 GB 空闲空间
    set /a FAILCOUNT+=1
) else (
    echo   [ OK ] 磁盘剩余   : !DRV! 盘还有 !FREEGB! GB
)

rem ---------- CPU 虚拟化 ----------
set VTFW=
set HYP=
for /f "usebackq tokens=*" %%i in (`powershell -NoProfile -Command "(Get-CimInstance Win32_Processor ^| Select-Object -First 1).VirtualizationFirmwareEnabled"`) do set VTFW=%%i
for /f "usebackq tokens=*" %%i in (`powershell -NoProfile -Command "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent"`) do set HYP=%%i
if /i "!VTFW!"=="True" goto :vtok
if /i "!HYP!"=="True" goto :vtok
echo   [ X  ] CPU 虚拟化 : 未开启
echo          ^-^> 必须开启，否则 Docker 装不上（见文末说明）
set /a FAILCOUNT+=1
set VTBAD=1
goto :vtdone
:vtok
echo   [ OK ] CPU 虚拟化 : 已开启
:vtdone

rem ---------- 安装路径 ----------
set PATHBAD=
powershell -NoProfile -Command "if ('%~dp0' -match '[^\x00-\x7F]') { exit 1 } else { exit 0 }"
if errorlevel 1 set PATHBAD=1
echo %~dp0 | findstr /C:" " >nul
if not errorlevel 1 set PATHBAD=1
if defined PATHBAD (
    echo   [ !  ] 安装路径   : 含中文或空格
    echo          ^-^> 建议改放到 D:\xianyu 这样的纯英文路径，更稳
    set /a WARNCOUNT+=1
) else (
    echo   [ OK ] 安装路径   : 纯英文，没问题
)

echo.
echo ============================================================
echo   二、Docker
echo ============================================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
    echo   [ X  ] Docker Desktop : 未安装
    echo          ^-^> 必须先安装，见文末说明
    set /a FAILCOUNT+=1
    set DOCKERMISSING=1
    goto :dockerdone
)
echo   [ OK ] Docker Desktop : 已安装
docker info >nul 2>&1
if errorlevel 1 (
    echo   [ !  ] Docker 正在运行 : 否
    echo          ^-^> 不用管，双击「2-一键启动」会自动帮你启动
) else (
    echo   [ OK ] Docker 正在运行 : 是
)
:dockerdone

echo.
echo ============================================================
echo   三、端口占用检查
echo ============================================================
echo.
for %%P in (9000 8089 8090 8091) do (
    set BUSY=
    for /f "usebackq tokens=*" %%L in (`netstat -ano ^| findstr /C:":%%P "`) do set BUSY=1
    if defined BUSY (
        echo   [ !  ] 端口 %%P : 已被占用
        set /a WARNCOUNT+=1
    ) else (
        echo   [ OK ] 端口 %%P : 空闲
    )
)

echo.
echo ============================================================
echo   检测结果
echo ============================================================
echo.
if !FAILCOUNT! EQU 0 (
    echo   必需条件全部满足！
    echo   现在可以双击「2-一键启动」了。
) else (
    echo   有 !FAILCOUNT! 项必须先处理，请看下面的说明。
)
echo.
if !WARNCOUNT! GTR 0 (
    echo   另有 !WARNCOUNT! 条提醒（标 [ ! ] 的），不影响使用。
    echo   - 如果端口被占用，且你之前启动过本系统，忽略即可。
)
echo.

if defined VTBAD goto :help_vt
goto :help_docker

:help_vt
echo ------------------------------------------------------------
echo   【CPU 虚拟化未开启】怎么办：
echo ------------------------------------------------------------
echo   1. 关机。开机时连续按 Del 键或 F2 键进入 BIOS。
echo      （华硕/微星/技嘉多是 Del，联想/戴尔/惠普多是 F2 或 F12）
echo   2. 在 BIOS 里找这些字样之一：
echo      Virtualization Technology、Intel VT-x、
echo      Intel Virtualization Technology、SVM Mode、AMD-V
echo      把它从 Disabled 改成 Enabled。
echo   3. 按 F10 保存并退出，电脑会重启。
echo   4. 重启后，再双击本文件检测一次。
echo.
echo   如果 BIOS 里找不到这一项，把这台电脑的型号告诉我。
echo.

:help_docker
if defined DOCKERMISSING goto :help_docker_do
goto :finish

:help_docker_do
echo ------------------------------------------------------------
echo   【Docker Desktop 未安装】怎么办：
echo ------------------------------------------------------------
echo   1. 打开浏览器访问：
echo      https://www.docker.com/products/docker-desktop/
echo   2. 点 "Download for Windows" 下载安装包。
echo   3. 双击安装包，安装时保持 "Use WSL 2 instead of Hyper-V"
echo      这一项是勾选状态，其它一路点 Next / Ok。
echo   4. 装完重启电脑，桌面会出现 Docker Desktop 图标。
echo   5. 双击桌面 Docker Desktop 图标，等左下角变成绿色
echo      "Engine running"，然后关掉它也行。
echo   6. 再双击本文件检测一次。
echo.
echo   更详细的图文步骤见「使用说明.txt」。
echo.

:finish
echo ============================================================
echo   按任意键关闭本窗口
echo ============================================================
pause >nul
