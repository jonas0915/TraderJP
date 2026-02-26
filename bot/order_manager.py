"""
Order Manager — the brain between a TradingView alert and Tradovate execution.

Responsibilities:
  1. Validate the incoming signal against session + news + risk filters
  2. Determine the correct ES front-month symbol
  3. Check current position before acting (avoid doubling up unintentionally)
  4. Place entry order + optional stop-loss and take-profit bracket orders
  5. Log every decision with clear reasoning

Supported signal actions (case-insensitive):
  buy          → enter long (market order)
  sell         → enter short (market order)
  close_long   → close / flatten long position
  close_short  → close / flatten short position
  close        → close / flatten all positions
  flatten      → alias for close
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from bot.tradovate_client import TradovateClient, TradovateError
from bot.risk_manager import RiskManager
from bot.news_filter import NewsFilter
from bot.session_filter import SessionFilter
from bot.contract_utils import get_front_month_symbol


@dataclass
class TradeSignal:
    """Parsed + validated payload from a TradingView webhook alert."""
    action:      str            # buy | sell | close_long | close_short | close | flatten
    qty:         int   = 1
    order_type:  str   = "market"   # market | limit
    price:       Optional[float] = None   # for limit entries
    stop_loss:   Optional[float] = None   # absolute price level
    take_profit: Optional[float] = None   # absolute price level
    comment:     str   = ""
    symbol:      str   = ""     # override auto-detected symbol if set


class OrderManager:
    # Mapping from TradingView action strings to internal actions
    _NORMALISE = {
        "buy":         "buy",
        "long":        "buy",
        "sell":        "sell",
        "short":       "sell",
        "close":       "close",
        "flatten":     "close",
        "close_all":   "close",
        "closeall":    "close",
        "close_long":  "close_long",
        "closelong":   "close_long",
        "close_short": "close_short",
        "closeshort":  "close_short",
        "exit":        "close",
        "exit_long":   "close_long",
        "exit_short":  "close_short",
    }

    def __init__(
        self,
        client:         TradovateClient,
        risk_manager:   RiskManager,
        news_filter:    NewsFilter,
        session_filter: SessionFilter,
        base_symbol:    str = "ES",
        default_qty:    int = 1,
    ):
        self.client         = client
        self.risk_manager   = risk_manager
        self.news_filter    = news_filter
        self.session_filter = session_filter
        self.base_symbol    = base_symbol
        self.default_qty    = default_qty

    # ------------------------------------------------------------------
    # Main entry point (called by webhook server)
    # ------------------------------------------------------------------

    async def handle_signal(self, signal: TradeSignal) -> dict:
        """
        Process a TradingView signal end-to-end.
        Returns a result dict with keys: success, message, order (if placed).
        """
        action = self._NORMALISE.get(signal.action.lower().strip())
        if action is None:
            return self._err(f"Unknown action: '{signal.action}'")

        signal.action = action
        symbol = signal.symbol or get_front_month_symbol(self.base_symbol)
        qty    = signal.qty or self.default_qty

        logger.info(f"Signal received: {action.upper()} {qty} {symbol}  comment='{signal.comment}'")

        # ---- CLOSE / FLATTEN ----
        if action in ("close", "close_long", "close_short"):
            return await self._handle_close(action, symbol, qty)

        # ---- ENTRY (buy/sell) ----

        # 1. Session filter
        allowed, reason = self.session_filter.is_trading_allowed()
        if not allowed:
            logger.warning(f"Trade BLOCKED (session): {reason}")
            return self._err(f"Session blocked: {reason}")

        # 2. News filter
        blacked_out, news_reason = self.news_filter.is_news_blackout()
        if blacked_out:
            logger.warning(f"Trade BLOCKED (news): {news_reason}")
            return self._err(f"News blackout: {news_reason}")

        # 3. Risk check
        ok, risk_reason = self.risk_manager.check_order_allowed(qty)
        if not ok:
            logger.warning(f"Trade BLOCKED (risk): {risk_reason}")
            return self._err(f"Risk blocked: {risk_reason}")

        # 4. Position check — avoid unintended pyramid
        position_qty = await self._get_position_qty(symbol)
        if action == "buy" and position_qty > 0:
            logger.warning(
                f"Already long {position_qty} contracts {symbol}. "
                f"Skipping duplicate buy. (Send close_long first to flip.)"
            )
            return self._err("Already in long position — send close_long before buying again.")
        if action == "sell" and position_qty < 0:
            logger.warning(
                f"Already short {abs(position_qty)} contracts {symbol}. "
                f"Skipping duplicate sell. (Send close_short first to flip.)"
            )
            return self._err("Already in short position — send close_short before selling again.")

        # 5. Place entry order
        tradovate_action = "Buy" if action == "buy" else "Sell"
        comment = signal.comment or f"TraderJP-{action}"

        try:
            if signal.order_type == "limit" and signal.price:
                entry_result = await asyncio.to_thread(
                    self.client.place_limit_order,
                    tradovate_action, symbol, qty, signal.price, comment
                )
            else:
                entry_result = await asyncio.to_thread(
                    self.client.place_market_order,
                    tradovate_action, symbol, qty, comment
                )
        except TradovateError as e:
            logger.error(f"Entry order failed: {e}")
            return self._err(f"Order rejected by Tradovate: {e}")

        logger.success(
            f"Entry order placed: {tradovate_action} {qty} {symbol} → {entry_result}"
        )

        # 6. Place bracket orders (stop-loss / take-profit)
        brackets = []
        if signal.stop_loss:
            brackets.append(
                asyncio.create_task(
                    self._place_bracket(
                        "Sell" if action == "buy" else "Buy",
                        symbol, qty, "stop", signal.stop_loss,
                        f"{comment}-SL"
                    )
                )
            )
        if signal.take_profit:
            brackets.append(
                asyncio.create_task(
                    self._place_bracket(
                        "Sell" if action == "buy" else "Buy",
                        symbol, qty, "limit", signal.take_profit,
                        f"{comment}-TP"
                    )
                )
            )
        if brackets:
            await asyncio.gather(*brackets, return_exceptions=True)

        return {
            "success": True,
            "message": f"{tradovate_action} {qty} {symbol} order placed",
            "order":   entry_result,
        }

    # ------------------------------------------------------------------
    # Close helpers
    # ------------------------------------------------------------------

    async def _handle_close(self, action: str, symbol: str, qty: int) -> dict:
        position_qty = await self._get_position_qty(symbol)

        if position_qty == 0:
            logger.info(f"Close signal received but no position in {symbol}.")
            return self._err("No open position to close.")

        if action == "close_long" and position_qty <= 0:
            return self._err("No long position to close.")
        if action == "close_short" and position_qty >= 0:
            return self._err("No short position to close.")

        # Cancel any outstanding bracket orders first
        await asyncio.to_thread(self.client.cancel_all_orders)

        try:
            result = await asyncio.to_thread(
                self.client.liquidate_position, symbol
            )
            logger.success(f"Position closed: {symbol} → {result}")
            return {"success": True, "message": f"Position {symbol} closed", "order": result}
        except TradovateError as e:
            logger.error(f"Liquidate failed: {e}")
            return self._err(f"Liquidate failed: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_position_qty(self, symbol: str) -> int:
        """
        Return signed net quantity for the given symbol on this account.
        Positive = long, Negative = short, 0 = flat.
        """
        try:
            positions = await asyncio.to_thread(self.client.get_positions)
        except TradovateError as e:
            logger.error(f"Could not fetch positions: {e}")
            return 0

        for pos in positions:
            if pos.get("contractId") and symbol in str(pos.get("contractId", "")):
                return pos.get("netPos", 0)
            # Tradovate may also return the symbol directly
            if pos.get("symbol", "") == symbol:
                return pos.get("netPos", 0)
        return 0

    async def _place_bracket(
        self,
        action: str,
        symbol: str,
        qty: int,
        order_type: str,
        price: float,
        comment: str,
    ):
        try:
            if order_type == "stop":
                result = await asyncio.to_thread(
                    self.client.place_stop_order, action, symbol, qty, price, comment
                )
            else:
                result = await asyncio.to_thread(
                    self.client.place_limit_order, action, symbol, qty, price, comment
                )
            logger.info(f"Bracket {order_type} placed: {action} {qty} {symbol} @ {price}")
            return result
        except TradovateError as e:
            logger.error(f"Bracket order failed ({order_type} @ {price}): {e}")
            return None

    @staticmethod
    def _err(msg: str) -> dict:
        return {"success": False, "message": msg, "order": None}
