@echo off
setlocal ENABLEDELAYEDEXPANSION

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

set HOST=%~1
if "%HOST%"=="" set HOST=127.0.0.1
set PORT=%~2
if "%PORT%"=="" set PORT=8888
set RELOAD_FLAG=--reload
if /I "%~3"=="NORELOAD" set RELOAD_FLAG=

REM External market-data providers are sensitive to stale local proxy settings.
REM Default: do not inherit local proxy variables for this server process.
REM If a proxy is required, start with: set USE_PROXY=1 && start.bat
if not "%USE_PROXY%"=="1" (
  set HTTP_PROXY=
  set HTTPS_PROXY=
  set ALL_PROXY=
  set http_proxy=
  set https_proxy=
  set all_proxy=
  echo [INFO] Proxy disabled for server process. Set USE_PROXY=1 to keep local proxy.
) else (
  echo [INFO] USE_PROXY=1, keeping local proxy variables.
)

echo [INFO] Checking uv...
where uv >nul 2>&1
if errorlevel 1 (
  echo [ERR] uv not found. Install uv from https://github.com/astral-sh/uv and ensure it is in PATH.
  exit /b 1
)

echo [INFO] Check/Create uv venv at .venv...
if not exist ".venv" (
  uv venv --python 3.11
  if errorlevel 1 (
    echo [ERR] uv venv failed
    exit /b 1
  )
)

echo [INFO] Installing dependencies into uv environment (if needed)...
if exist requirements.txt (
  uv pip install -r requirements.txt
) else (
  uv pip install fastapi uvicorn pydantic langchain-core langchain-openai langchain-anthropic langchain-google-genai mcp akshare baostock pandas
)
if errorlevel 1 (
  echo [ERR] Dependency installation failed
  exit /b 1
)

echo [INFO] Starting server with uv: http://%HOST%:%PORT%/
uv run python -m uvicorn app.main:app --host %HOST% --port %PORT% %RELOAD_FLAG%

endlocal
