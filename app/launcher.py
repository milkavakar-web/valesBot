"""Запуск бота и панели одной командой — из start.sh, start.bat, `python -m app` или ValesBot.exe.

Что делает:
- переходит в папку программы, чтобы .env и data/ лежали рядом с ней;
- при первом запуске создаёт .env из шаблона;
- если порт занят самим ботом — просто открывает панель, если чем-то другим — берёт свободный;
- запускает панель и открывает её в браузере.

Флаги: --no-browser — не открывать браузер; --check — запуститься, проверить, что панель
отвечает, и выйти (для автоматических проверок сборок).
"""
import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # собранный ValesBot.exe


def base_dir() -> Path:
    if FROZEN:  # настройки и база — рядом с exe, а не во временной папке распаковки
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def template() -> Path:
    root = Path(getattr(sys, "_MEIPASS", base_dir()))
    return root / ".env.example"


def port_busy(port: int) -> bool:
    """На порту кто-то слушает? Проверяем подключением, а не bind: после остановки
    программы порт ещё с минуту «занят» (TIME_WAIT), хотя запустить на нём панель можно."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def panel_alive(port: int) -> bool:
    """На этом порту уже отвечает наша панель?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=1.5) as r:
            return "timeframes" in json.load(r)
    except Exception:
        return False


def say(text: str) -> None:
    print(text, flush=True)


def main(argv=None) -> int:
    args = set(argv if argv is not None else sys.argv[1:])
    check = "--check" in args
    os.chdir(base_dir())
    if not Path(".env").exists() and template().exists():
        shutil.copyfile(template(), ".env")
        say(f"Создан файл настроек {Path('.env').resolve()} — ключи Binance и Telegram впишите туда.")

    from app import config  # читает .env, поэтому только после того, как он появился

    local = config.HOST in ("127.0.0.1", "localhost")
    if not local and not config.PANEL_PASSWORD:
        say("Панель будет доступна из сети, но пароль не задан. "
            "Укажите PANEL_PASSWORD в .env или HOST=127.0.0.1")
        return 1
    browser = local and not check and "--no-browser" not in args and os.getenv("NO_BROWSER") != "1"

    port = config.PORT
    if local and port_busy(port):
        if panel_alive(port):
            url = f"http://127.0.0.1:{port}"
            say(f"Бот уже запущен — открываю панель: {url}")
            if browser:
                webbrowser.open(url)
            return 0
        busy = port
        port = next((p for p in range(busy + 1, busy + 50) if not port_busy(p)), None)
        if port is None:
            say(f"Порт {busy} и следующие за ним заняты. Укажите свободный PORT в .env")
            return 1
        say(f"Порт {busy} занят другой программой — панель будет на порту {port}.")
        config.PORT = port

    import uvicorn
    from app.main import app as asgi

    url = f"http://127.0.0.1:{port}" if local else f"http://{config.HOST}:{port}"
    server = uvicorn.Server(uvicorn.Config(asgi, host=config.HOST, port=port, log_level="info"))
    say("")
    say(f"  Панель бота: {url}")
    say("  Остановить бота: закройте это окно или нажмите Ctrl+C.")
    say("  Позиции и стопы на бирже при этом остаются, при следующем запуске боты продолжат работу.")
    say("")

    if browser:
        def open_when_ready():
            for _ in range(300):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(0.2)
        threading.Thread(target=open_when_ready, daemon=True).start()

    if check:
        return asyncio.run(_check(server, port))
    server.run()
    return 0


async def _check(server, port: int) -> int:
    """Запустить панель, убедиться, что она отвечает, и корректно остановить."""
    task = asyncio.create_task(server.serve())
    ok = False
    for _ in range(150):
        await asyncio.sleep(0.2)
        if server.started:
            ok = await asyncio.to_thread(panel_alive, port)
            break
        if task.done():
            break
    server.should_exit = True
    await task
    say("ПРОВЕРКА: панель отвечает" if ok else "ПРОВЕРКА: панель не запустилась")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
