#!/bin/sh
# Запуск бота и панели: ./start.sh  (Linux и macOS)
# Флаги: --no-browser — не открывать браузер, --check — проверить запуск и выйти.
set -e
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1 || [ -x "$HOME/.local/bin/uv" ]; then
    PATH="$HOME/.local/bin:$PATH"
elif python3 -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    # есть подходящий Python — свои библиотеки бот держит в папке .venv
    [ -x .venv/bin/python ] || python3 -m venv .venv
    if ! cmp -s requirements.txt .venv/requirements.installed; then
        echo "Ставлю библиотеки бота (только при первом запуске и после обновления)…"
        .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
        cp requirements.txt .venv/requirements.installed
    fi
    exec .venv/bin/python -m app "$@"
else
    echo "Подходящего Python нет — ставлю uv: он сам скачает Python и библиотеки."
    echo "Права администратора не нужны, всё ляжет в домашнюю папку."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        wget -qO- https://astral.sh/uv/install.sh | sh
    fi
    PATH="$HOME/.local/bin:$PATH"
fi
exec uv run --quiet --no-project --python 3.12 --with-requirements requirements.txt python -m app "$@"
