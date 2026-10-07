"""Уведомления в Telegram.

Сообщения уходят из отдельной фоновой задачи: если Telegram недоступен или отвечает
медленно, торговля этого не замечает. Без TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env
уведомления выключены.
"""
import asyncio
import html
import logging
from typing import Any, Dict, Optional

import aiohttp

from . import config
from .rules import _num

log = logging.getLogger("bot.notify")
API = "https://api.telegram.org/bot{token}/{method}"
MODE_NAME = {"paper": "бумага", "demo": "демо", "live": "реальные деньги"}


class TelegramError(Exception):
    def __init__(self, msg: str, retry_after: Optional[int] = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Notifier:
    def __init__(self):
        self.queue: "asyncio.Queue[tuple]" = asyncio.Queue(maxsize=500)
        self.task: Optional[asyncio.Task] = None
        self.session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool:
        return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)

    def send(self, text: str, silent: bool = False) -> None:
        """Поставить сообщение в очередь. silent — без звука на телефоне."""
        if not self.enabled:
            return
        try:
            self.queue.put_nowait((text, silent))
        except asyncio.QueueFull:
            log.warning("Очередь уведомлений переполнена, сообщение пропущено")

    async def _call(self, method: str, **payload) -> Any:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
        async with self.session.post(url, json=payload) as r:
            data = await r.json(content_type=None)
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {r.status}",
                                (data.get("parameters") or {}).get("retry_after"))
        return data["result"]

    async def _message(self, text: str, silent: bool = False) -> None:
        await self._call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text, parse_mode="HTML",
                         disable_notification=silent, link_preview_options={"is_disabled": True})

    async def _worker(self) -> None:
        while True:
            text, silent = await self.queue.get()
            try:
                for attempt in range(3):
                    try:
                        await self._message(text, silent)
                        break
                    except TelegramError as e:
                        if not e.retry_after:
                            log.warning("Telegram: %s", e)
                            break
                        await asyncio.sleep(e.retry_after)
                    except Exception as e:
                        log.warning("Telegram недоступен: %s", e)
                        await asyncio.sleep(5 * (attempt + 1))
                await asyncio.sleep(1)  # Telegram пускает в один чат не больше сообщения в секунду
            finally:
                self.queue.task_done()

    async def start(self) -> None:
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Даём уйти тому, что уже в очереди (например, «программа остановлена»), до 10 секунд."""
        if self.task:
            try:
                await asyncio.wait_for(self.queue.join(), 10)
            except asyncio.TimeoutError:
                pass
            self.task.cancel()
            self.task = None
        if self.session:
            await self.session.close()
            self.session = None

    async def test(self) -> Dict[str, Any]:
        """Проверка из панели: отправляет пробное сообщение, а без chat_id подсказывает его."""
        if not config.TELEGRAM_BOT_TOKEN:
            return {"ok": False, "detail": "Не задан TELEGRAM_BOT_TOKEN в .env"}
        try:
            me = await self._call("getMe")
            if not config.TELEGRAM_CHAT_ID:
                chats = {}
                for u in await self._call("getUpdates", limit=50):
                    msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
                    chat = msg.get("chat") or {}
                    if chat.get("id"):
                        chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                return {"ok": False, "bot": me.get("username"),
                        "chats": [{"id": k, "name": v} for k, v in chats.items()],
                        "detail": "Не задан TELEGRAM_CHAT_ID. Напишите своему боту в Telegram любое "
                                  "сообщение и нажмите «Проверить» ещё раз — здесь появится нужный chat_id."}
            await self._message("✅ Связь с торговым ботом работает. Сюда будут приходить сделки и тревоги.")
            return {"ok": True, "bot": me.get("username")}
        except Exception as e:
            return {"ok": False, "detail": f"Telegram ответил: {e}"}


notifier = Notifier()


# ---------- тексты ----------

def _e(s: Any) -> str:
    return html.escape(str(s), quote=False)


def wanted(bot: Dict[str, Any]) -> bool:
    return bot["mode"] != "paper" or config.TELEGRAM_PAPER


def bot_line(bot: Dict[str, Any]) -> str:
    market = "спот" if bot["market"] == "spot" else "фьючерсы"
    return f"<b>{_e(bot['symbol'])}</b> · {market} · {MODE_NAME[bot['mode']]} — {_e(bot['name'])}"


def _usdt(v: float) -> str:
    return ("+" if v > 0 else "") + _num(v) + " USDT"


def trade_message(bot: Dict[str, Any], t: Dict[str, Any]):
    """Текст о сделке и признак «без звука»."""
    side = "лонг" if t["side"] == "long" else "шорт"
    base = bot["symbol"].split("/")[0]
    what = f"{_num(t['amount'])} {_e(base)} по {_num(t['price'])} ≈ {_num(t['amount'] * t['price'])} USDT"
    if t["action"] == "open":
        return f"▲ <b>Вход в {side}</b>\n{bot_line(bot)}\n{what}\nПочему: {_e(t['reason'])}", False
    if t["action"] == "safety":
        return f"● <b>{_e(t['reason'].capitalize())}</b>\n{bot_line(bot)}\n{what}", True
    pnl = t.get("pnl") or 0.0
    pct = t.get("pnl_pct")
    head = f"{'✅' if pnl > 0 else '🔻'} <b>Выход: {_usdt(pnl)}</b>" + (f" ({'+' if pct > 0 else ''}{_num(pct)}%)" if pct is not None else "")
    return f"{head}\n{bot_line(bot)}\n{what}\nПричина: {_e(t['reason'])}", False


def alert_message(bot: Dict[str, Any], text: str) -> str:
    return f"⚠️ <b>Внимание</b>\n{bot_line(bot)}\n{_e(text)}"


def info_message(bot: Dict[str, Any], text: str) -> str:
    return f"{bot_line(bot)}\n{_e(text)}"
