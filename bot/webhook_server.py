"""
FastAPI webhook server.

Listens for incoming POST alerts from TradingView (or any other source) and
routes them through the OrderManager.

Endpoints:
  POST /webhook           — receive a TradingView alert
  GET  /status            — bot health and risk snapshot
  GET  /position          — current ES position and P&L
  GET  /events            — today's scheduled high-impact news events
  POST /flatten           — emergency manual flatten (requires secret)

--- TradingView Alert Message Format ---
Configure your TradingView alert to send a JSON body to:
  http(s)://YOUR_SERVER:8000/webhook

Example alert message (paste into TradingView "Message" field):
{
  "secret":      "{{strategy.order.comment}}",
  "action":      "buy",
  "qty":         1,
  "stop_loss":   {{strategy.position_avg_price}} - 10,
  "take_profit": {{strategy.position_avg_price}} + 20,
  "comment":     "EMA Cross Bull"
}

Simpler version (hard-coded):
{
  "secret":  "YOUR_WEBHOOK_SECRET",
  "action":  "buy",
  "qty":     1,
  "comment": "My Strategy Long"
}
"""

import os
from datetime import datetime
from typing import Optional

import pytz
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field, validator

from bot.order_manager import OrderManager, TradeSignal
from bot.risk_manager import RiskManager
from bot.news_filter import NewsFilter
from bot.session_filter import SessionFilter


# ------------------------------------------------------------------
# Request / response models
# ------------------------------------------------------------------

class WebhookPayload(BaseModel):
    secret:      str
    action:      str
    qty:         int         = Field(default=1, ge=1, le=20)
    order_type:  str         = "market"      # market | limit
    price:       Optional[float] = None
    stop_loss:   Optional[float] = None
    take_profit: Optional[float] = None
    comment:     str         = ""
    symbol:      str         = ""            # optional symbol override

    @validator("action")
    def action_lowercase(cls, v):
        return v.lower().strip()

    @validator("order_type")
    def order_type_lowercase(cls, v):
        return v.lower().strip()


class FlattenPayload(BaseModel):
    secret: str


# ------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------

def create_app(
    order_manager:   OrderManager,
    risk_manager:    RiskManager,
    news_filter:     NewsFilter,
    session_filter:  SessionFilter,
    webhook_secret:  str,
) -> FastAPI:

    app = FastAPI(
        title="TraderJP — ES Bot",
        description="TradingView webhook receiver for ES futures on Tradovate/Apex",
        version="1.0.0",
    )
    ET = pytz.timezone("America/New_York")
    CT = pytz.timezone("America/Chicago")

    # ----------------------------------------------------------------
    # /webhook  — main TradingView alert endpoint
    # ----------------------------------------------------------------

    @app.post("/webhook")
    async def webhook(payload: WebhookPayload, request: Request):
        # Validate secret
        if payload.secret != webhook_secret:
            logger.warning(
                f"Webhook rejected: bad secret from {request.client.host}"
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid webhook secret.")

        signal = TradeSignal(
            action      = payload.action,
            qty         = payload.qty,
            order_type  = payload.order_type,
            price       = payload.price,
            stop_loss   = payload.stop_loss,
            take_profit = payload.take_profit,
            comment     = payload.comment,
            symbol      = payload.symbol,
        )

        logger.info(
            f"Webhook received from {request.client.host}: "
            f"{payload.action.upper()} qty={payload.qty} comment='{payload.comment}'"
        )

        result = await order_manager.handle_signal(signal)

        http_status = status.HTTP_200_OK if result["success"] else status.HTTP_422_UNPROCESSABLE_ENTITY
        return JSONResponse(content=result, status_code=http_status)

    # ----------------------------------------------------------------
    # /status  — bot health snapshot
    # ----------------------------------------------------------------

    @app.get("/status")
    async def bot_status():
        risk = risk_manager.get_status()
        session_ok, session_reason = session_filter.is_trading_allowed()
        news_ok, news_reason       = news_filter.is_news_blackout()

        return {
            "bot":     "TraderJP v1.0",
            "time_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
            "time_ct": datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S CT"),
            "session": {
                "current":  session_filter.current_session(),
                "allowed":  session_ok,
                "reason":   session_reason,
            },
            "news": {
                "blackout": news_ok,
                "reason":   news_reason,
            },
            "risk": risk,
        }

    # ----------------------------------------------------------------
    # /position  — current position
    # ----------------------------------------------------------------

    @app.get("/position")
    async def position():
        import asyncio
        from bot.tradovate_client import TradovateError
        try:
            positions = await asyncio.to_thread(
                order_manager.client.get_positions
            )
            snapshot  = await asyncio.to_thread(
                order_manager.client.get_cash_balance_snapshot
            )
            return {
                "positions": positions,
                "balance":   snapshot,
            }
        except TradovateError as e:
            raise HTTPException(status_code=502, detail=str(e))

    # ----------------------------------------------------------------
    # /events  — today's news events
    # ----------------------------------------------------------------

    @app.get("/events")
    async def events():
        return {
            "today": news_filter.get_todays_events(),
            "next_event_minutes": news_filter.next_event_minutes(),
        }

    # ----------------------------------------------------------------
    # /flatten  — emergency flatten (manual trigger)
    # ----------------------------------------------------------------

    @app.post("/flatten")
    async def manual_flatten(payload: FlattenPayload, request: Request):
        if payload.secret != webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid secret.")
        import asyncio
        logger.warning(
            f"MANUAL FLATTEN triggered from {request.client.host}"
        )
        await asyncio.to_thread(order_manager.client.flatten_all)
        return {"success": True, "message": "All positions flattened."}

    # ----------------------------------------------------------------
    # /health  — simple ping
    # ----------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


# ------------------------------------------------------------------
# Runner helper (called from main.py)
# ------------------------------------------------------------------

async def run_server(
    app:  FastAPI,
    host: str = "0.0.0.0",
    port: int = 8000,
):
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",  # uvicorn's own logs; loguru handles app logs
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info(f"Webhook server listening on {host}:{port}")
    await server.serve()
