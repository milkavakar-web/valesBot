@echo off
chcp 65001 >nul
rem Запуск бота и панели двойным щелчком (Windows).
rem Флаги: --no-browser — не открывать браузер, --check — проверить запуск и выйти.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  echo Первый запуск: ставлю uv — он сам скачает Python и библиотеки.
  echo Права администратора не нужны, всё ляжет в папку пользователя.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
uv run --quiet --no-project --python 3.12 --with-requirements requirements.txt python -m app %*
set CODE=%ERRORLEVEL%
if not "%~1"=="--check" if not "%CODE%"=="0" pause
exit /b %CODE%
