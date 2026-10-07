"""Настройки берутся из файла .env (см. .env.example)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# Логин и пароль для веб-панели (обязательны, если панель доступна не только с localhost)
PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")

# Адреса, по которым открывают панель, кроме localhost и 127.0.0.1 (через запятую).
# Запросы к панели по другим адресам отклоняются — это защита от DNS rebinding.
PANEL_HOSTS = [h.strip().lower() for h in os.getenv("PANEL_HOSTS", "").split(",") if h.strip()]

DB_PATH = os.getenv("DB_PATH", "data/bot.db")

# Как часто бот проверяет цену, секунд
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "15"))

# Реальная торговля выключена, пока явно не включите ALLOW_LIVE=1
ALLOW_LIVE = _flag("ALLOW_LIVE")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Ключи демо-счёта Binance (demo.binance.com → API Management)
BINANCE_DEMO_API_KEY = os.getenv("BINANCE_DEMO_API_KEY", "")
BINANCE_DEMO_API_SECRET = os.getenv("BINANCE_DEMO_API_SECRET", "")

# Комиссии для бумажной торговли и бэктеста (тейкер, доля)
PAPER_FEE_SPOT = float(os.getenv("PAPER_FEE_SPOT", "0.001"))
PAPER_FEE_FUTURES = float(os.getenv("PAPER_FEE_FUTURES", "0.0005"))
