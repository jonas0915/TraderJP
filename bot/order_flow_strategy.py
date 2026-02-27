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

Brackets placed immediately after every entry
---------------------------------------------
  Stop loss   : $300 (6 ES points) against the trade
  Take profit : $600 (12 ES points) in favour of the trade  →  2:1 R:R

Session guards
--------------
  Daily profit cap : once realized + open P&L >= $100 no new entries are opened.
  All existing session / news / risk-manager filters still apply.
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

# ---------------------------------------------------------------------------
# ES contract specs
# ---------------------------------------------------------------------------

ES_POINT_VALUE = 50.0   # USD per full index point
ES_TICK_SIZE   = 0.25   # points per minimum price increment ($12.50 / tick)


def _to_points(dollars: float) -> float:
    """Convert a dollar P&L amount to ES index points."""
    return dollars / ES_POINT_VALUE


def _tick(price: float) -> float:
    """Round an ES price to the nearest valid 0.25-point tick."""
    return round(round(price / ES_TICK_SIZE) * ES_TICK_SIZE, 2)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class OrderFlowStrategy:

    def __init__(
        self,
        client:                TradovateClient,
        risk_manager:          RiskManager,
        news_filter:           NewsFilter,
        session_filter:        SessionFilter,
        base_symbol:           str   = "ES",
        qty:                   int   = 1,
        imbalance_ratio:       float = 3.0,
        delta_min:             float = 50.0,
        delta_lookback:        int   = 30,
        large_print_threshold: int   = 100,
        dom_levels:            int   = 5,
        cooldown_seconds:      int   = 30,
        stop_loss_dollars:     float = 300.0,
        take_profit_dollars:   float = 600.0,
        daily_profit_cap:      float = 100.0,
    ):
        self.client                = client
        self.risk_manager          = risk_manager
        self.news_filter           = news_filter
        self.session_filter        = session_filter
        self.base_symbol           = base_symbol
        self.qty                   = qty
        self.imbalance_ratio       = imbalance_ratio
        self.delta_min             = delta_min
        self.dom_levels            = dom_levels
        self.large_print_threshold = large_print_threshold
        self.cooldown_seconds      = cooldown_seconds
        self.stop_loss_dollars     = stop_loss_dollars
        self.take_profit_dollars   = take_profit_dollars
        self.daily_profit_cap      = daily_profit_cap

        # Rolling delta window (each entry = signed trade size)
        self._delta_window: deque = deque(maxlen=delta_lookback)

        # Last known best bid/ask (updated from quote events)
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0

        # Timestamp of the last executed entry (for cooldown)
        self._last_entry_time: Optional[datetime] = None

        # Consecutive DOM snapshot confirmations required before entry.
        # Prevents single noisy snapshots from triggering trades.
        self._signal_streak: dict = {"buy": 0, "sell": 0}
        self._required_streak: int = 3

        # Minimum number of prints required in the delta window before acting.
        # Avoids trading on sparse data (e.g. first seconds after open).
        self._min_window_fill: int = max(10, delta_lookback // 3)

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

        # Keep local bid/ask reference up to date
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

        long_signal  = ratio >= self.imbalance_ratio and rolling_delta >= self.delta_min
        short_signal = ratio <= (1.0 / self.imbalance_ratio) and rolling_delta <= -self.delta_min

        # Update streaks — signal must persist across consecutive DOM snapshots
        # to filter out single noisy spikes.
        if long_signal:
            self._signal_streak["buy"]  += 1
            self._signal_streak["sell"]  = 0
        elif short_signal:
            self._signal_streak["sell"] += 1
            self._signal_streak["buy"]   = 0
        else:
            self._signal_streak["buy"]  = 0
            self._signal_streak["sell"] = 0

        if long_signal and self._signal_streak["buy"] >= self._required_streak:
            await self._try_entry(
                action="buy",
                display=f"bid/ask {ratio:.1f}:1 | delta {rolling_delta:+.0f} | streak {self._signal_streak['buy']}",
            )
        elif short_signal and self._signal_streak["sell"] >= self._required_streak:
            inv = 1.0 / ratio
            await self._try_entry(
                action="sell",
                display=f"ask/bid {inv:.1f}:1 | delta {rolling_delta:+.0f} | streak {self._signal_streak['sell']}",
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

        # ---- daily profit cap ---------------------------------------
        status      = self.risk_manager.get_status()
        session_pnl = status["realized_pnl"] + status["unrealized_pnl"]
        if session_pnl >= self.daily_profit_cap:
            logger.info(
                f"Daily profit cap ${self.daily_profit_cap:,.0f} reached "
                f"(session P&L: ${session_pnl:,.2f}). No new entries."
            )
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

        # ---- signal quality filters ---------------------------------
        if not self._window_filled():
            logger.debug(
                f"Order flow signal skipped: delta window only "
                f"{len(self._delta_window)}/{self._min_window_fill} prints filled."
            )
            return

        if not self._delta_accelerating(action):
            logger.debug(
                f"Order flow signal skipped ({action}): delta momentum fading — "
                f"recent half of window not confirming direction."
            )
            return

        if not self._spread_ok():
            spread = round(self._last_ask - self._last_bid, 2)
            logger.debug(
                f"Order flow signal skipped: spread {spread:.2f} pts > 1 tick threshold."
            )
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

        # ---- compute bracket prices before sending entry ------------
        #  Use last-known best ask for longs (lifted ask = expected fill)
        #  Use last-known best bid for shorts (hit bid = expected fill)
        ref         = self._last_ask if action == "buy" else self._last_bid
        stop_pts    = _to_points(self.stop_loss_dollars)    # e.g. 6.0
        tp_pts      = _to_points(self.take_profit_dollars)  # e.g. 12.0

        if action == "buy":
            stop_price = _tick(ref - stop_pts)
            tp_price   = _tick(ref + tp_pts)
            bracket_action = "Sell"
        else:
            stop_price = _tick(ref + stop_pts)
            tp_price   = _tick(ref - tp_pts)
            bracket_action = "Buy"

        # ---- place entry order -------------------------------------
        tradovate_action = "Buy" if action == "buy" else "Sell"
        logger.info(
            f"ORDER FLOW ENTRY: {tradovate_action.upper()} {self.qty} {symbol}  "
            f"[{display}]  "
            f"ref={ref:.2f}  SL={stop_price:.2f} (−${self.stop_loss_dollars:,.0f})  "
            f"TP={tp_price:.2f} (+${self.take_profit_dollars:,.0f})"
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
                f"Entry confirmed: {tradovate_action} {self.qty} {symbol} → {result}"
            )
            self._last_entry_time = now
        except TradovateError as exc:
            logger.error(f"Order flow entry FAILED: {exc}")
            return   # don't place brackets if entry failed

        # ---- place stop loss + take profit concurrently -------------
        asyncio.create_task(
            self._place_brackets(bracket_action, symbol, stop_price, tp_price)
        )

    # ------------------------------------------------------------------
    # Bracket helpers
    # ------------------------------------------------------------------

    async def _place_brackets(
        self,
        action:     str,
        symbol:     str,
        stop_price: float,
        tp_price:   float,
    ):
        """Place stop loss and take profit concurrently after entry."""
        await asyncio.gather(
            self._place_stop(action, symbol, stop_price),
            self._place_tp(action, symbol, tp_price),
            return_exceptions=True,
        )

    async def _place_stop(self, action: str, symbol: str, price: float):
        try:
            await asyncio.to_thread(
                self.client.place_stop_order,
                action, symbol, self.qty, price, "OrderFlow-SL",
            )
            logger.info(
                f"Stop loss placed: {action} {self.qty} {symbol} @ {price}  "
                f"(risk ${self.stop_loss_dollars:,.0f})"
            )
        except TradovateError as exc:
            logger.error(f"Stop loss order FAILED: {exc}")

    async def _place_tp(self, action: str, symbol: str, price: float):
        try:
            await asyncio.to_thread(
                self.client.place_limit_order,
                action, symbol, self.qty, price, "OrderFlow-TP",
            )
            logger.info(
                f"Take profit placed: {action} {self.qty} {symbol} @ {price}  "
                f"(target ${self.take_profit_dollars:,.0f})"
            )
        except TradovateError as exc:
            logger.error(f"Take profit order FAILED: {exc}")

    # ------------------------------------------------------------------
    # Signal quality filters
    # ------------------------------------------------------------------

    def _delta_accelerating(self, direction: str) -> bool:
        """
        Check that momentum is still building, not fading.
        Splits the delta window into two halves and confirms the recent half
        has stronger directional flow than the earlier half.
        """
        window = list(self._delta_window)
        if len(window) < self._min_window_fill:
            return False
        mid = len(window) // 2
        early_delta  = sum(window[:mid])
        recent_delta = sum(window[mid:])
        if direction == "buy":
            return recent_delta > 0 and recent_delta >= early_delta * 0.5
        else:
            return recent_delta < 0 and recent_delta <= early_delta * 0.5

    def _spread_ok(self) -> bool:
        """Block entries when the bid-ask spread is wider than 1 tick (0.25 pts).
        Wide spreads mean higher slippage cost and lower-quality fills."""
        if not self._last_bid or not self._last_ask:
            return False
        return (self._last_ask - self._last_bid) <= ES_TICK_SIZE

    def _window_filled(self) -> bool:
        """Require a minimum number of prints before acting to avoid
        trading on sparse data at the open or after a gap."""
        return len(self._delta_window) >= self._min_window_fill

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
