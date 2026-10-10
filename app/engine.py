"""Запуск ботов: каждый бот — отдельная задача asyncio, которая раз в POLL_SECONDS
забирает свечи, проверяет стоп/тейк/усреднения и на новой свече считает сигнал."""
import asyncio
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from . import config, db
from .brokers import ExchangeBroker, PaperBroker, exchange_symbol, hub
from .notify import alert_message, info_message, notifier, trade_message, wanted
from .strategy import prepare, warmup_candles
from .trader import OrderStatusUnknown, Trader

logger = logging.getLogger("bot.engine")


def now_ms() -> int:
    return int(time.time() * 1000)


class Manager:
    def __init__(self):
        self.tasks: Dict[int, asyncio.Task] = {}
        self.traders: Dict[int, Tuple[Trader, Any]] = {}
        self.logs: Dict[int, Deque[str]] = {}
        self.locks: Dict[int, asyncio.Lock] = {}
        self.err_count: Dict[int, int] = {}      # ошибок подряд у бота
        self.err_notified: Dict[int, str] = {}   # о какой ошибке уже написали в Telegram

    # ---------- служебное ----------

    def log(self, bot_id: int, msg: str) -> None:
        line = time.strftime("%d.%m %H:%M:%S") + "  " + msg
        self.logs.setdefault(bot_id, deque(maxlen=300)).append(line)
        logger.info("[бот %s] %s", bot_id, msg)

    def notify(self, bot: Dict[str, Any], text: str, silent: bool = False) -> None:
        if wanted(bot):
            notifier.send(text, silent)

    def alert(self, bot: Dict[str, Any], msg: str) -> None:
        self.log(bot["id"], "⚠ " + msg)
        self.notify(bot, alert_message(bot, msg))

    def lock(self, bot_id: int) -> asyncio.Lock:
        return self.locks.setdefault(bot_id, asyncio.Lock())

    def is_running(self, bot_id: int) -> bool:
        t = self.tasks.get(bot_id)
        return t is not None and not t.done()

    async def build(self, bot: Dict[str, Any]) -> Tuple[Trader, Any]:
        mode, market = bot["mode"], bot["market"]
        if mode == "live" and not config.ALLOW_LIVE:
            raise RuntimeError("Реальная торговля выключена. Чтобы включить, "
                               "поставьте ALLOW_LIVE=1 в .env и перезапустите программу")
        ex = await hub.get(market, "public" if mode == "paper" else mode)
        if mode == "paper":
            broker = PaperBroker(market)
        else:
            broker = ExchangeBroker(ex, market, bot["symbol"], bot["params"]["leverage"])
            await broker.prepare()
        sym = exchange_symbol(market, bot["symbol"])
        if sym not in ex.markets:
            raise ValueError(f"Пары {bot['symbol']} нет на Binance "
                             f"({'спот' if market == 'spot' else 'фьючерсы USDⓈ-M'})")
        state = bot["state"]
        bot_id = bot["id"]

        async def record(t: Dict[str, Any]) -> None:
            db.add_trade(bot_id, t)
            db.save_state(bot_id, state)
            self.notify(bot, *trade_message(bot, t))

        trader = Trader(bot, broker, state, record, lambda m: self.log(bot_id, m),
                        persist=lambda: db.save_state(bot_id, state),
                        alert=lambda m: self.alert(bot, m))
        return trader, ex

    # ---------- запуск и остановка ----------

    async def startup(self) -> None:
        for bot in db.list_bots():
            if bot["enabled"]:
                self.tasks[bot["id"]] = asyncio.create_task(self._run(bot["id"]))

    async def _cancel(self, bot_id: int, t: asyncio.Task) -> None:
        """Останавливает задачу бота только между проверками. Если прервать её посреди
        отправки ордера, сделка пройдёт на бирже, а бот о ней не узнает."""
        async with self.lock(bot_id):
            t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    async def shutdown(self) -> None:
        # enabled в базе не трогаем: после перезапуска программы боты продолжат работу
        tasks = list(self.tasks.items())
        await asyncio.gather(*(self._cancel(bot_id, t) for bot_id, t in tasks if not t.done()))
        self.tasks.clear()
        self.traders.clear()

    async def start(self, bot_id: int) -> None:
        if self.is_running(bot_id):
            return
        bot = db.get_bot(bot_id)
        if not bot:
            raise KeyError(bot_id)
        built = await self.build(bot)  # ошибки (нет ключей и т.п.) сразу уходят в интерфейс
        db.set_enabled(bot_id, True)
        self.tasks[bot_id] = asyncio.create_task(self._run(bot_id, built))
        self.notify(bot, info_message(bot, "▶ Бот запущен"), silent=True)

    async def stop(self, bot_id: int) -> None:
        db.set_enabled(bot_id, False)
        t = self.tasks.get(bot_id)
        if t and not t.done():
            await self._cancel(bot_id, t)
            self.log(bot_id, "Бот остановлен")
            bot = db.get_bot(bot_id)
            if bot:
                pos = bot["state"].get("position")
                tail = (" Позиция остаётся, стоп на бирже стоит." if pos and pos.get("stop")
                        else " Позиция остаётся без присмотра." if pos else "")
                self.notify(bot, info_message(bot, "⏸ Бот остановлен." + tail), silent=not pos)
        self.tasks.pop(bot_id, None)
        self.traders.pop(bot_id, None)

    async def _run(self, bot_id: int, built: Optional[Tuple[Trader, Any]] = None) -> None:
        while built is None:  # после перезапуска программы — пробуем, пока не получится
            try:
                built = await self.build(db.get_bot(bot_id))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._error(bot_id, None, e)
                await asyncio.sleep(60)
        trader, ex = built
        self.traders[bot_id] = built
        self.log(bot_id, "Бот запущен")
        while True:
            try:
                async with self.lock(bot_id):
                    await self._tick(trader, ex)
                delay = config.POLL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._error(bot_id, trader.state, e)
                delay = max(config.POLL_SECONDS, 30)
            await asyncio.sleep(delay)

    def _error(self, bot_id: int, state: Optional[dict], e: Exception) -> None:
        msg = str(e) or type(e).__name__
        if len(msg) > 300:
            msg = msg[:300] + "…"
        self.log(bot_id, "Ошибка: " + msg)
        if state is None:
            bot = db.get_bot(bot_id)
            state = bot["state"] if bot else {}
        state["error"] = msg
        db.save_state(bot_id, state)
        # В Telegram — только ошибки, которые держатся: случайный сбой сети исправится сам на
        # следующем шаге. Неподтверждённый ордер — сразу.
        n = self.err_count[bot_id] = self.err_count.get(bot_id, 0) + 1
        if isinstance(e, OrderStatusUnknown) or (n >= 2 and self.err_notified.get(bot_id) != msg):
            bot = db.get_bot(bot_id)
            if bot:
                self.notify(bot, alert_message(bot, "Ошибка: " + msg))
                self.err_notified[bot_id] = msg

    async def _tick(self, trader: Trader, ex) -> None:
        bot, state = trader.bot, trader.state
        sym = exchange_symbol(bot["market"], bot["symbol"])
        tf_ms = ex.parse_timeframe(bot["timeframe"]) * 1000
        # свечей берём с запасом на «разогрев» индикатора — так сигналы совпадают с бэктестом
        limit = min(1000, max(500, warmup_candles(trader.p) + 2))
        ohlcv = await ex.fetch_ohlcv(sym, bot["timeframe"], limit=limit)
        if len(ohlcv) < 3:
            raise RuntimeError("Биржа вернула слишком мало свечей")
        now = now_ms()
        # последняя свеча обычно ещё формируется — сигнал считаем только по закрытым
        closed = ohlcv if ohlcv[-1][0] + tf_ms <= now else ohlcv[:-1]
        price = float(ohlcv[-1][4])
        state["last_price"] = price
        state["last_tick"] = now

        # сначала разбираемся с ордером, ответ на который не дошёл, и со стопом на бирже
        await trader.resolve_pending()
        await trader.check_stop(price, now)
        await trader.on_price(price, now)

        last_ts = closed[-1][0]
        if state.get("last_candle_ts") != last_ts:
            state["last_candle_ts"] = last_ts  # сначала отмечаем: одна свеча — максимум одна попытка
            # в отдельном потоке: медленный сигнал-скрипт не должен задерживать других ботов
            ind = await asyncio.to_thread(prepare, closed, trader.p)
            await trader.on_candle(ind, len(closed) - 1, price, now)

        # после входа или усреднения ставим или переставляем стоп на бирже
        await trader.sync_stop(price, now)

        if self.err_notified.pop(bot["id"], None):
            self.notify(bot, info_message(bot, "✅ Снова работает после ошибки"), silent=True)
        self.err_count.pop(bot["id"], None)
        state["error"] = None
        db.save_state(bot["id"], state)

    # ---------- ручные действия ----------

    async def _trader_for(self, bot_id: int) -> Tuple[Trader, Any]:
        if self.is_running(bot_id) and bot_id in self.traders:
            return self.traders[bot_id]
        bot = db.get_bot(bot_id)
        if not bot:
            raise KeyError(bot_id)
        return await self.build(bot)

    async def close_position(self, bot_id: int) -> None:
        async with self.lock(bot_id):
            trader, ex = await self._trader_for(bot_id)
            bot = trader.bot
            ticker = await ex.fetch_ticker(exchange_symbol(bot["market"], bot["symbol"]))
            price = float(ticker["last"])
            await trader.resolve_pending()
            await trader.check_stop(price, now_ms())  # стоп мог сработать, пока бот стоял
            if not trader.pos:
                raise ValueError("Открытой позиции нет")
            await trader.close(price, "вручную", now_ms())
            db.save_state(bot_id, trader.state)

    async def forget_position(self, bot_id: int) -> None:
        """Убрать позицию и неподтверждённый ордер из памяти бота без сделки
        (если разобрались с ними сами на бирже)."""
        async with self.lock(bot_id):
            if self.is_running(bot_id) and bot_id in self.traders:
                trader = self.traders[bot_id][0]
            else:
                bot = db.get_bot(bot_id)
                st = (bot["state"].get("position") or {}).get("stop")
                trader = (await self.build(bot))[0] if st else None
                state = bot["state"]
            if trader:
                state = trader.state
                st = (state.get("position") or {}).get("stop")
                if st:  # стоп на бирже без позиции однажды закрыл бы чужую позицию — снимаем
                    try:
                        await trader.broker.cancel_stop(st)
                        self.log(bot_id, "Стоп на бирже снят")
                    except Exception as e:
                        self.alert(trader.bot, f"Не удалось снять стоп на бирже ({e}). Снимите его вручную на Binance.")
            state["position"] = None
            state["pending"] = None
            state["dust"] = None
            db.save_state(bot_id, state)
            self.log(bot_id, "Позиция убрана из памяти бота без сделки")


manager = Manager()
