@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ======================================================================
REM  monitor_stock 本地容器启停脚本（Windows + Docker Desktop）
REM
REM    scripts\docker.bat start     启动（镜像不存在时自动构建）
REM    scripts\docker.bat stop      停止并移除容器（config/logs 数据保留）
REM    scripts\docker.bat restart   重建容器并重启（改了代码或 .env 后用这个）
REM    scripts\docker.bat status    查看容器状态与健康检查
REM    scripts\docker.bat logs      跟踪日志（Ctrl+C 退出）
REM    scripts\docker.bat rebuild   依赖变化后重建镜像再启动
REM    scripts\docker.bat shell     进入容器
REM ======================================================================

set "ROOT=%~dp0.."
pushd "%ROOT%" >nul 2>&1 || (
  echo [ERR] 无法进入项目根目录：%ROOT%
  exit /b 1
)

set "COMPOSE=docker compose -f docker/docker-compose.local.yml"
REM compose 在无 TTY 的控制台会刷一屏重复进度行，能静默就静默
set "QUIET="
docker compose --progress quiet version >nul 2>&1 && set "QUIET=--progress quiet"
set "IMAGE=monitor-stock:local"
set "CTR=monitor-stock-local"
set "PORT=8888"
set "URL=http://localhost:%PORT%/"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

REM ---- 依赖：Docker 必须在运行 ----------------------------------------
docker info >nul 2>&1
if errorlevel 1 (
  echo [ERR] Docker 没有运行。请先启动 Docker Desktop，等托盘图标变成 Running 再试。
  goto :fail
)

if /I "%ACTION%"=="start"   goto :do_start
if /I "%ACTION%"=="stop"    goto :do_stop
if /I "%ACTION%"=="restart" goto :do_restart
if /I "%ACTION%"=="status"  goto :do_status
if /I "%ACTION%"=="logs"    goto :do_logs
if /I "%ACTION%"=="rebuild" goto :do_rebuild
if /I "%ACTION%"=="shell"   goto :do_shell

echo [ERR] 不认识的动作：%ACTION%
echo       可用：start ^| stop ^| restart ^| status ^| logs ^| rebuild ^| shell
goto :fail


:do_start
docker image inspect %IMAGE% >nul 2>&1
if errorlevel 1 (
  echo [INFO] 首次运行，正在构建镜像（首次约 10 分钟，之后有缓存很快）...
  call %COMPOSE% %QUIET% up -d --build
) else (
  call %COMPOSE% %QUIET% up -d
)
if errorlevel 1 goto :fail
call :wait_healthy
goto :done


:do_restart
echo [INFO] 重建容器（让新代码和新 .env 生效）...
call %COMPOSE% %QUIET% up -d --force-recreate
if errorlevel 1 goto :fail
call :wait_healthy
goto :done


:do_rebuild
echo [INFO] 重建镜像（改了 requirements.txt 才需要）...
call %COMPOSE% %QUIET% up -d --build --force-recreate
if errorlevel 1 goto :fail
call :wait_healthy
goto :done


:do_stop
echo [INFO] 停止并移除容器（config/ 与 logs/ 里的数据不受影响）...
call %COMPOSE% %QUIET% down
if errorlevel 1 goto :fail
echo [OK] 已停止。下次启动： scripts\docker.bat start
goto :done


:do_status
echo [INFO] 容器状态：
for /f "delims=" %%L in ('docker ps -a --filter "name=%CTR%" --format "  {{.Names}}  |  {{.Status}}  |  {{.Ports}}"^') do echo %%L
echo.
docker inspect %CTR% --format "  mem_limit_bytes={{.HostConfig.Memory}}" 2>nul
echo [INFO] 健康检查 %URL%api/health ：
curl -s -o nul -w "  HTTP %%{http_code}  (%%{time_total}s)\n" -m 5 "%URL%api/health" 2>nul
if errorlevel 1 echo   无法连接（容器可能没启动）
goto :done


:do_logs
echo [INFO] 跟踪日志，Ctrl+C 退出...
call %COMPOSE% %QUIET% logs -f --tail 100
goto :done


:do_shell
echo [INFO] 进入容器（exit 退出）...
docker exec -it %CTR% bash
goto :done


REM ---- 健康检查：最多等 60 秒 ----------------------------------------
:wait_healthy
echo [INFO] 等待服务就绪...
set "OK="
for /l %%i in (1,1,30) do (
  for /f %%c in ('curl -s -o nul -w "%%{http_code}" -m 3 "%URL%api/health" 2^>nul') do set "CODE=%%c"
  if "!CODE!"=="200" (
    set "OK=1"
    goto :healthy_ok
  )
  timeout /t 2 /nobreak >nul
)
:healthy_ok
if defined OK (
  echo [OK] 服务已就绪：%URL%
  echo       口令见仓库根目录 .env 的 APP_PASSWORD
) else (
  echo [WARN] 60 秒内健康检查未通过，看日志： scripts\docker.bat logs
)
exit /b 0


:done
popd >nul 2>&1
endlocal
exit /b 0

:fail
popd >nul 2>&1
endlocal
exit /b 1
