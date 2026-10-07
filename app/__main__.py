"""Запуск: python -m app"""
import sys

import uvicorn

from . import config

if __name__ == "__main__":
    if config.HOST not in ("127.0.0.1", "localhost") and not config.PANEL_PASSWORD:
        sys.exit("Панель будет доступна из сети, но пароль не задан. "
                 "Укажите PANEL_PASSWORD в .env или HOST=127.0.0.1")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info")
