"""
Order-flow scalping strategy.

Signals are derived from live Tradovate DOM (depth of market) and trade prints:

  1. DOM bid/ask imbalance  — ratio of cumulative size on top N bid levels vs
                               top N ask levels.
  2. Cumulative delta       — net buy-vs-sell aggressor volume over the last
                               ``delta_lookback`` prints.
  3. Large-print detection  — single prints above ``large_print_threshold``
                               contracts are logged for visibility.

Entry rules
-----------
  LONG  : bid_imbalance_ratio >= imbalance_ratio  AND  rolling_delta >= delta_min
  SHORT : ask_imbalance_ratio >= imbalance_ratio  AND  rolling_delta <= -delta_min

No automatic exits — position management is handled manually or via the
/flatten endpoint.  The existing session, news, and risk filters all still
apply before any order is sent.

Cooldown between signals is enforced via ``cooldown_seconds`` to avoid rapid
re-entry on sustained imbalance.
"""

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from bot.tradovate_client import TradovateClient, TradovateError
from bot.risk_manager import RiskManager
from bot.news_filter import NewsFilter
from bot.session_filter import SessionFilter
from bot.contract_utils import get_front_month_symbol


class OrderFlowStrategy:

    def __init__(
        self,
        client:              TradovateClient,
        risk_manager:        RiskManager,
        news_filter:         NewsFilter,
        session_filter:      SessionFilter,
        base_symbol:         str   = "ES",
        qty:                 int   = 1,
        imbalance_ratio:     float = 3.0,
        delta_min:           float = 50.0,
        delta_lookback:      int   = 30,
        large_print_threshold: int = 100,
        dom_levels:          int   = 5,
        cooldown_seconds:    int   = 30,
    ):
        self.client               = client
        self.risk_manager         = risk_manager
        self.news_filter          = news_filter
        self.session_filter       = session_filter
        self.base_symbol          = base_symbol
        self.qty                  = qty
        self.imbalance_ratio      = imbalance_ratio
        self.delta_min            = delta_min
        self.dom_levels           = dom_levels
        self.large_print_threshold = large_print_threshold
        self.cooldown_seconds     = cooldown_seconds

        # Rolling delta window (each entry = signed trade size)
        self._delta_window: deque = deque(maxlen=delta_lookback)

        # Last known best bid/ask (updated from quote events)
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0

        # Timestamp of the last executed entry (for cooldown)
        self._last_entry_time: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Callbacks wired to MarketDataFeed
    # ------------------------------------------------------------------

    async def on_dom_update(self, dom):
        """Called on every DOM snapshot from the market data feed."""
        try:
            await self._evaluate_dom(dom)
        except Exception as exc:
            logger.error(f"OrderFlowStrategy.on_dom_update error: {exc}")

    async def on_quote_update(self, quote):
        """Called on every quote event (includes last trade price/size)."""
        try:
            self._process_trade_print(quote)
        except Exception as exc:
            logger.error(f"OrderFlowStrategy.on_quote_update error: {exc}")

    # ------------------------------------------------------------------
    # Quote processing — build cumulative delta
    # ------------------------------------------------------------------

    def _process_trade_print(self, quote):
        """Classify each trade print as buy or sell aggressor and update delta."""
        size = quote.trade_size
        if size == 0:
            return

        # Update local bid/ask reference
        if quote.bid_price:
            self._last_bid = quote.bid_price
        if quote.ask_price:
            self._last_ask = quote.ask_price

        bid = self._last_bid
        ask = self._last_ask

        if ask and quote.trade_price >= ask:
            delta = size           # lifted the ask — buy aggressor
        elif bid and quote.trade_price <= bid:
            delta = -size          # hit the bid — sell aggressor
        else:
            return                 # mid-price fill; skip

        self._delta_window.append(delta)

        if abs(delta) >= self.large_print_threshold:
            side = "BUY " if delta > 0 else "SELL"
            logger.info(
                f"LARGE PRINT: {side} {abs(delta):>4} contracts "
                f"@ {quote.trade_price:.2f}  "
                f"(rolling delta: {sum(self._delta_window):+.0f})"
            )

    # ------------------------------------------------------------------
    # DOM evaluation — generate signals
    # ------------------------------------------------------------------

    async def _evaluate_dom(self, dom):
        """Compute imbalance and fire an entry signal if conditions are met."""
        bids = dom.bids[: self.dom_levels]
        asks = dom.asks[: self.dom_levels]

        total_bid = sum(b.get("size", 0) for b in bids)
        total_ask = sum(a.get("size", 0) for a in asks)

        if total_bid == 0 or total_ask == 0:
            return

        ratio         = total_bid / total_ask
        rolling_delta = sum(self._delta_window)

        if ratio >= self.imbalance_ratio and rolling_delta >= self.delta_min:
            await self._try_entry(
                action="buy",
                display=f"bid/ask {ratio:.1f}:1 | delta {rolling_delta:+.0f}",
            )
        elif ratio <= (1.0 / self.imbalance_ratio) and rolling_delta <= -self.delta_min:
            inv = 1.0 / ratio
            await self._try_entry(
                action="sell",
                display=f"ask/bid {inv:.1f}:1 | delta {rolling_delta:+.0f}",
            )

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    async def _try_entry(self, action: str, display: str):
        # ---- cooldown -----------------------------------------------
        now = datetime.now(timezone.utc)
        if self._last_entry_time:
            elapsed = (now - self._last_entry_time).total_seconds()
            if elapsed < self.cooldown_seconds:
                return

        # ---- session filter -----------------------------------------
        allowed, reason = self.session_filter.is_trading_allowed()
        if not allowed:
            logger.debug(f"Order flow signal skipped (session): {reason}")
            return

        # ---- news filter --------------------------------------------
        blacked_out, _ = self.news_filter.is_news_blackout()
        if blacked_out:
            logger.debug("Order flow signal skipped (news blackout).")
            return

        # ---- risk check ---------------------------------------------
        ok, risk_reason = self.risk_manager.check_order_allowed(self.qty)
        if not ok:
            logger.warning(f"Order flow signal blocked (risk): {risk_reason}")
            return

        # ---- position check -----------------------------------------
        symbol = get_front_month_symbol(self.base_symbol)
        pos    = await self._get_position(symbol)

        if action == "buy"  and pos > 0:
            return   # already long
        if action == "sell" and pos < 0:
            return   # already short

        # Flip opposing position before entering
        if pos != 0:
            logger.info(
                f"OrderFlow: closing opposite position ({pos:+d}) "
                f"before {action.upper()} entry."
            )
            await asyncio.to_thread(self.client.cancel_all_orders)
            await asyncio.to_thread(self.client.liquidate_position, symbol)
            await asyncio.sleep(0.5)

        # ---- place order --------------------------------------------
        tradovate_action = "Buy" if action == "buy" else "Sell"
        logger.info(
            f"ORDER FLOW ENTRY: {tradovate_action.upper()} {self.qty} {symbol}  "
            f"[{display}]"
        )

        try:
            result = await asyncio.to_thread(
                self.client.place_market_order,
                tradovate_action,
                symbol,
                self.qty,
                f"OrderFlow-{action}",
            )
            logger.success(
                f"Order flow order confirmed: {tradovate_action} {self.qty} "
                f"{symbol} → {result}"
            )
            self._last_entry_time = now
        except TradovateError as exc:
            logger.error(f"Order flow order FAILED: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_position(self, symbol: str) -> int:
        """Return signed net position for ``symbol``. 0 if flat or error."""
        try:
            positions = await asyncio.to_thread(self.client.get_positions)
        except TradovateError as exc:
            logger.error(f"Could not fetch positions: {exc}")
            return 0

        for pos in positions:
            if pos.get("symbol", "") == symbol:
                return pos.get("netPos", 0)
            if symbol in str(pos.get("contractId", "")):
                return pos.get("netPos", 0)
        return 0
