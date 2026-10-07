"""Исполнение ордеров: бумажная торговля и Binance (демо или реальный счёт) через ccxt."""
import asyncio
import logging
from typing import Dict, Optional, Tuple

import ccxt.async_support as ccxt

from . import config
from .trader import Fill, OrderStatusUnknown

log = logging.getLogger("bot.broker")

# После этих ошибок ясно, что биржа ордер не приняла. Любая другая ошибка
# (таймаут, обрыв, сбой на стороне биржи) значит «неизвестно, прошёл ли ордер».
REJECTED = (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest, ccxt.AuthenticationError)


def exchange_symbol(market: str, symbol: str) -> str:
    """BTC/USDT → BTC/USDT для спота и BTC/USDT:USDT для USDⓈ-M фьючерсов."""
    if market == "futures":
        quote = symbol.split("/")[1]
        return f"{symbol}:{quote}"
    return symbol


class ExchangeHub:
    """Держит по одному подключению ccxt на пару (рынок, режим)."""

    def __init__(self):
        self._ex: Dict[Tuple[str, str], ccxt.binance] = {}
        self._lock = asyncio.Lock()

    def keys_for(self, mode: str) -> Tuple[str, str]:
        if mode == "live":
            return config.BINANCE_API_KEY, config.BINANCE_API_SECRET
        if mode == "demo":
            return config.BINANCE_DEMO_API_KEY, config.BINANCE_DEMO_API_SECRET
        return "", ""

    async def get(self, market: str, mode: str) -> ccxt.binance:
        """mode: 'public' (только рыночные данные), 'demo' или 'live'."""
        async with self._lock:
            return await self._get(market, mode)

    async def _get(self, market: str, mode: str) -> ccxt.binance:
        key = (market, mode)
        if key in self._ex:
            return self._ex[key]
        opts = {
            "defaultType": "spot" if market == "spot" else "future",
            "fetchCurrencies": False,
            "fetchMarkets": {"types": ["spot"] if market == "spot" else ["linear"]},
        }
        cfg = {"enableRateLimit": True, "options": opts}
        if mode in ("demo", "live"):
            api_key, secret = self.keys_for(mode)
            if not api_key or not secret:
                name = "BINANCE_API_KEY / BINANCE_API_SECRET" if mode == "live" else \
                    "BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET"
                raise RuntimeError(f"Нет ключей API: заполните {name} в .env и перезапустите бота")
            cfg.update({"apiKey": api_key, "secret": secret})
        ex = ccxt.binance(cfg)
        if mode == "demo":
            ex.enable_demo_trading(True)
        try:
            await ex.load_markets()
        except Exception:
            await ex.close()
            raise
        self._ex[key] = ex
        return ex

    async def close_all(self):
        for ex in self._ex.values():
            try:
                await ex.close()
            except Exception:
                pass
        self._ex.clear()


hub = ExchangeHub()


class PaperBroker:
    """Симуляция: сделки «исполняются» по переданной цене, с комиссией тейкера."""

    def __init__(self, market: str):
        self.fee_rate = config.PAPER_FEE_SPOT if market == "spot" else config.PAPER_FEE_FUTURES

    async def open(self, side: str, notional: float, price: float, cid: str = "") -> Fill:
        amount = notional / price
        return Fill(amount=amount, price=price, fee=notional * self.fee_rate)

    async def close(self, side: str, amount: float, price: float, cid: str = "") -> Fill:
        return Fill(amount=amount, price=price, fee=amount * price * self.fee_rate)

    async def find(self, cid: str) -> Optional[Fill]:
        return None  # бумажная сделка не может потеряться на бирже


class ExchangeBroker:
    """Рыночные ордера на Binance. Позиции на фьючерсах — изолированная маржа, one-way режим."""

    def __init__(self, ex: ccxt.binance, market: str, symbol: str, leverage: int):
        self.ex = ex
        self.market = market
        self.symbol = exchange_symbol(market, symbol)
        self.leverage = leverage
        m = ex.market(self.symbol)
        self.base, self.quote = m["base"], m["quote"]
        self.limits = m.get("limits") or {}

    async def prepare(self):
        if self.market != "futures":
            return
        try:
            await self.ex.set_margin_mode("isolated", self.symbol)
        except Exception as e:  # уже изолированная или есть открытая позиция
            log.info("set_margin_mode: %s", e)
        await self.ex.set_leverage(self.leverage, self.symbol)

    def _amount(self, raw: float, price: float, reduce_only: bool = False) -> float:
        try:
            amount = float(self.ex.amount_to_precision(self.symbol, raw))
        except Exception:
            amount = 0.0
        min_amount = (self.limits.get("amount") or {}).get("min") or 0
        # Минимальная сумма ордера на фьючерсах не касается закрытия позиции (reduceOnly):
        # иначе позицию дешевле минимума нельзя было бы закрыть даже по стопу.
        min_cost = 0 if reduce_only else (self.limits.get("cost") or {}).get("min") or 0
        if amount <= 0 or amount < min_amount or amount * price < min_cost:
            raise ValueError(
                f"Ордер слишком мал для биржи: {raw * price:.2f} {self.quote}. "
                f"Минимум примерно {max(min_cost, min_amount * price):.2f} {self.quote} — "
                f"увеличьте сумму ордера")
        return amount

    async def _fill(self, order: dict, fallback_amount: float, fallback_price: float,
                    is_spot_buy: bool) -> Fill:
        if order.get("average") is None or not order.get("filled"):
            try:
                order = await self.ex.fetch_order(order["id"], self.symbol)
            except Exception as e:
                log.warning("fetch_order: %s", e)
        filled = float(order.get("filled") or fallback_amount)
        price = float(order.get("average") or order.get("price") or fallback_price)
        fee_quote = 0.0
        fee_base = 0.0
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for f in fees:
            if not f or f.get("cost") is None:
                continue
            cost = float(f["cost"])
            if f.get("currency") == self.quote:
                fee_quote += cost
            elif f.get("currency") == self.base:
                fee_base += cost
                fee_quote += cost * price
            # комиссию в BNB не учитываем в расчёте прибыли
        amount = filled - fee_base if is_spot_buy else filled
        return Fill(amount=amount, price=price, fee=fee_quote)

    async def _market(self, order_side: str, amount: float, price: float, cid: str,
                      params: Optional[dict] = None) -> Fill:
        """Рыночный ордер с нашим ID. Если ответ не дошёл, бросает OrderStatusUnknown:
        тогда бот найдёт ордер по этому ID на следующей проверке (find)."""
        try:
            order = await self.ex.create_order(self.symbol, "market", order_side, amount, None,
                                               {**(params or {}), "clientOrderId": cid})
        except REJECTED:
            raise
        except Exception as e:
            raise OrderStatusUnknown(f"Биржа не подтвердила ордер ({e}). "
                                     f"Бот проверит его на следующем шаге") from e
        try:
            return await self._fill(order, amount, price,
                                    is_spot_buy=self.market == "spot" and order_side == "buy")
        except Exception as e:  # ордер принят, но разобрать ответ не вышло
            raise OrderStatusUnknown(f"Не удалось прочитать исполнение ордера: {e}") from e

    async def find(self, cid: str) -> Optional[Fill]:
        """Исполнение ордера по нашему ID; None — такого ордера на бирже нет или он
        ничего не купил и не продал."""
        try:
            order = await self.ex.fetch_order(None, self.symbol, {"clientOrderId": cid})
        except ccxt.OrderNotFound:
            return None
        if order.get("status") == "open":
            raise OrderStatusUnknown("Ордер ещё исполняется на бирже")
        if not order.get("filled"):
            return None
        return await self._fill(order, float(order["filled"]),
                                float(order.get("average") or order.get("price") or 0),
                                is_spot_buy=self.market == "spot" and order.get("side") == "buy")

    async def open(self, side: str, notional: float, price: float, cid: str) -> Fill:
        amount = self._amount(notional / price, price)
        return await self._market("buy" if side == "long" else "sell", amount, price, cid)

    async def close(self, side: str, amount: float, price: float, cid: str) -> Fill:
        if self.market == "futures":
            amount = self._amount(amount, price, reduce_only=True)
            return await self._market("sell" if side == "long" else "buy", amount, price, cid,
                                      {"reduceOnly": True})
        bal = await self.ex.fetch_balance()
        have = min(amount, float((bal.get(self.base) or {}).get("free") or 0))
        fill = await self._market("sell", self._amount(have, price), price, cid)
        # Объём продажи округляется вниз до шага лота, и часть монет остаётся на счёте.
        fill.unsold = max(have - fill.amount, 0.0)
        return fill
