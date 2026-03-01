"""
Tradovate Market Data WebSocket feed.

Streams real-time DOM (depth of market) and quote/trade data for a single
futures contract.  Runs as a long-lived asyncio task and auto-reconnects on
disconnection or error.

Usage:
    feed = MarketDataFeed(md_token="...", live=True)
    feed.on_dom   = my_async_dom_callback
    feed.on_quote = my_async_quote_callback
    await feed.run(symbol="ESH5", contract_id=12345)
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import websockets
from loguru import logger


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DOMSnapshot:
    """Level-2 order book snapshot from a single DOM event."""
    contract_id: int
    timestamp:   str
    bids:        List[Dict]   # [{"price": float, "size": int}, ...]  best bid first
    asks:        List[Dict]   # [{"price": float, "size": int}, ...]  best ask first


@dataclass
class QuoteUpdate:
    """Best-bid/ask + last trade from a single quote event."""
    contract_id: int
    timestamp:   str
    bid_price:   float
    bid_size:    int
    ask_price:   float
    ask_size:    int
    trade_price: float
    trade_size:  int


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

class MarketDataFeed:
    """Tradovate market-data WebSocket feed (DOM + quotes)."""

    WS_LIVE = "wss://md.tradovateapi.com/v1/websocket"
    WS_DEMO = "wss://md-demo.tradovateapi.com/v1/websocket"

    HEARTBEAT_INTERVAL = 2.5   # seconds — Tradovate closes the WS if silent > 3 s
    RECONNECT_DELAY    = 5     # seconds between reconnect attempts

    def __init__(self, md_token: str, live: bool = False):
        self._token   = md_token
        self._url     = self.WS_LIVE if live else self.WS_DEMO
        self._running = False

        # Callbacks (set by strategy before calling run())
        self.on_dom:   Optional[Callable] = None
        self.on_quote: Optional[Callable] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, symbol: str, contract_id: int):
        """Connect, subscribe, and stream data.  Reconnects automatically."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_stream(symbol, contract_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"MarketDataFeed error ({type(exc).__name__}: {exc}). "
                    f"Reconnecting in {self.RECONNECT_DELAY}s..."
                )
                await asyncio.sleep(self.RECONNECT_DELAY)

    async def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _connect_and_stream(self, symbol: str, contract_id: int):
        logger.info(f"MarketDataFeed: connecting to {self._url}")

        async with websockets.connect(
            self._url,
            ping_interval=None,   # we handle heartbeats manually
            open_timeout=15,
        ) as ws:
            logger.info("MarketDataFeed: WebSocket connected.")

            # Authorize
            await self._send(ws, {"url": "md/authorize", "body": {"token": self._token}})

            # Subscribe DOM + quotes
            sub_body = {"symbol": symbol, "contractId": contract_id}
            await self._send(ws, {"url": "md/subscribeDom",   "body": sub_body})
            await self._send(ws, {"url": "md/subscribeQuote", "body": sub_body})

            logger.info(
                f"MarketDataFeed: subscribed DOM + quotes for {symbol} "
                f"(contractId={contract_id})"
            )

            # Heartbeat + receive concurrently; either raises on disconnect
            await asyncio.gather(
                self._heartbeat_loop(ws),
                self._receive_loop(ws),
            )

    async def _heartbeat_loop(self, ws):
        """Send an empty-array heartbeat every HEARTBEAT_INTERVAL seconds."""
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            try:
                await ws.send("[]")
            except Exception as exc:
                logger.warning(f"MarketDataFeed heartbeat failed: {exc}")
                # Raise so asyncio.gather cancels the sibling receive_loop and
                # _connect_and_stream returns, triggering a clean reconnect.
                raise

    async def _receive_loop(self, ws):
        """Read frames and dispatch to handlers."""
        async for raw in ws:
            await self._handle_frame(raw)

    async def _handle_frame(self, raw: str):
        """Parse a raw WebSocket frame and call on_dom / on_quote."""
        # Ignore heartbeat / SockJS open frames
        if not raw or raw.strip() in ("", "o", "h", "[]"):
            return

        # SockJS wraps data frames as "m<json>"
        msg = raw[1:] if raw.startswith("m") else raw

        try:
            events = json.loads(msg)
        except (json.JSONDecodeError, ValueError):
            return

        if not isinstance(events, list):
            return

        for event in events:
            e_type = event.get("e", "")
            e_data = event.get("d", {})

            if e_type == "dom":
                await self._dispatch_dom(e_data)
            elif e_type == "quote":
                await self._dispatch_quote(e_data)

    async def _dispatch_dom(self, data: dict):
        if not self.on_dom:
            return
        snapshot = DOMSnapshot(
            contract_id = data.get("contractId", 0),
            timestamp   = data.get("timestamp", ""),
            bids        = sorted(data.get("bids", []), key=lambda x: -x.get("price", 0)),
            asks        = sorted(data.get("asks", []), key=lambda x:  x.get("price", 0)),
        )
        try:
            await self.on_dom(snapshot)
        except Exception as exc:
            logger.error(f"on_dom callback error: {exc}")

    async def _dispatch_quote(self, data: dict):
        if not self.on_quote:
            return
        entries    = data.get("entries", {})
        bid        = entries.get("Bid",   {})
        ask        = entries.get("Ask",   {})
        trade      = entries.get("Trade", {})
        bid_price  = bid.get("price",   0.0)
        ask_price  = ask.get("price",   0.0)

        # Reject updates with missing or crossed prices — these appear at feed
        # startup before the exchange sends a full snapshot and must not be fed
        # to the delta classifier, which would corrupt the rolling delta window.
        if bid_price <= 0 or ask_price <= 0:
            return
        if bid_price >= ask_price:
            return   # crossed market — stale or malformed frame

        quote = QuoteUpdate(
            contract_id = data.get("contractId", 0),
            timestamp   = data.get("timestamp", ""),
            bid_price   = bid_price,
            bid_size    = bid.get("size",    0),
            ask_price   = ask_price,
            ask_size    = ask.get("size",    0),
            trade_price = trade.get("price", 0.0),
            trade_size  = trade.get("size",  0),
        )
        try:
            await self.on_quote(quote)
        except Exception as exc:
            logger.error(f"on_quote callback error: {exc}")

    @staticmethod
    async def _send(ws, payload: dict):
        await ws.send(json.dumps(payload))
