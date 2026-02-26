"""
TraderJP — ES Futures Trading Bot
===================================
Apex Trader Funding  ·  Tradovate  ·  TradingView Webhooks

Entry point: reads configuration from .env, wires all components together,
and runs the async event loop with:
  - Risk monitor (background task polling Tradovate every N seconds)
  - Token refresh loop
  - FastAPI webhook server (blocking, always in foreground)

Usage:
  cp config/.env.example .env
  # edit .env with your credentials and Apex limits
  python main.py
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# Load .env before importing any bot modules so env vars are available
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)


# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

def setup_logging():
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file  = os.getenv("LOG_FILE", "logs/traderjp.log")

    # Remove default handler
    logger.remove()

    # Console (colourised)
    logger.add(
        sys.stdout,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>"
        ),
        colorize=True,
    )

    # File (rotating)
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_file,
        level=log_level,
        rotation="10 MB",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} — {message}",
    )


# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------

def _req(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise ValueError(
            f"Required environment variable '{key}' is not set. "
            f"Copy config/.env.example to .env and fill in your credentials."
        )
    return val


def _opt(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, str(default)).strip().lower()
    return v in ("1", "true", "yes", "on")


def _int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

async def _token_refresh_loop(client):
    """Hourly token-refresh task (module-level so it can be passed to gather)."""
    from bot.tradovate_client import TradovateError
    while True:
        await asyncio.sleep(3600)
        try:
            client.ensure_auth()
        except TradovateError as e:
            logger.error(f"Token refresh failed: {e}")


async def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("  TraderJP — ES Futures Bot  starting up")
    logger.info("=" * 60)

    # ---- Import modules here (after logging is configured) --------
    from bot.tradovate_client    import TradovateClient, TradovateError
    from bot.risk_manager        import RiskManager, ApexConfig
    from bot.news_filter         import NewsFilter
    from bot.session_filter      import SessionFilter, SessionConfig
    from bot.order_manager       import OrderManager
    from bot.webhook_server      import create_app, run_server
    from bot.contract_utils      import get_front_month_symbol
    from bot.market_data         import MarketDataFeed
    from bot.order_flow_strategy import OrderFlowStrategy

    # ---- Tradovate credentials ------------------------------------
    username    = _req("TRADOVATE_USERNAME")
    password    = _req("TRADOVATE_PASSWORD")
    device_id   = _opt("TRADOVATE_DEVICE_ID") or f"TraderJP-{uuid.uuid4().hex[:8]}"
    cid         = _int("TRADOVATE_CID", 0)
    secret      = _opt("TRADOVATE_SECRET", "")
    live        = _bool("TRADOVATE_LIVE", True)
    account_name = _opt("TRADOVATE_ACCOUNT_NAME", "")

    env_label = "LIVE" if live else "DEMO"
    logger.info(f"Tradovate mode: {env_label}")
    if not live:
        logger.warning(
            "Running in DEMO mode — orders will go to your sim account."
        )

    # ---- Apex risk config ----------------------------------------
    apex_cfg = ApexConfig(
        account_size       = _float("APEX_ACCOUNT_SIZE",          100_000),
        daily_loss_limit   = _float("APEX_DAILY_LOSS_LIMIT",        3_000),
        max_trailing_dd    = _float("APEX_MAX_TRAILING_DRAWDOWN",    3_000),
        profit_target      = _float("APEX_PROFIT_TARGET",            6_000),
        max_contracts      = _int(  "APEX_MAX_CONTRACTS",                6),
        consistency_rule   = _bool( "APEX_CONSISTENCY_RULE",         True),
        max_day_profit_pct = _float("APEX_MAX_DAY_PROFIT_PCT",        0.30),
        eod_flatten_minutes= _int(  "APEX_EOD_FLATTEN_MINUTES",          0),
        warning_threshold  = _float("APEX_WARNING_THRESHOLD",         0.80),
        poll_interval      = _int(  "RISK_POLL_INTERVAL",               10),
    )

    logger.info(
        f"Apex limits — Daily loss: ${apex_cfg.daily_loss_limit:,.0f}  "
        f"Trailing DD: ${apex_cfg.max_trailing_dd:,.0f}  "
        f"Max contracts: {apex_cfg.max_contracts}"
    )

    # ---- Session filter ------------------------------------------
    session_cfg = SessionConfig(
        trade_rth    = _bool("TRADE_RTH", True),
        trade_eth    = _bool("TRADE_ETH", False),
        force_start  = _opt("FORCE_START", ""),
        force_end    = _opt("FORCE_END",   ""),
    )
    session_filter = SessionFilter(session_cfg)

    # ---- News filter ---------------------------------------------
    news_impacts = [
        i.strip() for i in _opt("NEWS_IMPACTS", "High").split(",")
    ]
    news_countries = [
        c.strip() for c in _opt("NEWS_COUNTRIES", "USD").split(",")
    ]
    news_filter = NewsFilter(
        enabled              = _bool("NEWS_FILTER_ENABLED", True),
        blackout_before_min  = _int("NEWS_BLACKOUT_BEFORE_MIN", 10),
        blackout_after_min   = _int("NEWS_BLACKOUT_AFTER_MIN",   5),
        impact_levels        = tuple(news_impacts),
        countries            = tuple(news_countries),
    )

    # ---- Tradovate client ----------------------------------------
    client = TradovateClient(
        username     = username,
        password     = password,
        device_id    = device_id,
        cid          = cid,
        secret       = secret,
        live         = live,
        account_name = account_name,
    )

    logger.info("Connecting to Tradovate...")
    try:
        client.authenticate()
    except TradovateError as e:
        logger.critical(f"Authentication failed: {e}")
        sys.exit(1)

    # ---- Front-month contract ------------------------------------
    symbol = _opt("SYMBOL", "ES")
    front_month = get_front_month_symbol(symbol)
    logger.info(f"Trading symbol: {front_month}")

    # ---- Risk manager --------------------------------------------
    risk_manager = RiskManager(
        client = client,
        config = apex_cfg,
    )

    # ---- Order manager -------------------------------------------
    order_manager = OrderManager(
        client         = client,
        risk_manager   = risk_manager,
        news_filter    = news_filter,
        session_filter = session_filter,
        base_symbol    = symbol,
        default_qty    = _int("DEFAULT_QTY", 1),
    )

    # ---- Webhook server ------------------------------------------
    webhook_secret = _req("WEBHOOK_SECRET")
    webhook_host   = _opt("WEBHOOK_HOST", "0.0.0.0")
    webhook_port   = _int("WEBHOOK_PORT", 8000)

    app = create_app(
        order_manager   = order_manager,
        risk_manager    = risk_manager,
        news_filter     = news_filter,
        session_filter  = session_filter,
        webhook_secret  = webhook_secret,
    )

    logger.info(f"Webhook URL: http://{webhook_host}:{webhook_port}/webhook")
    logger.info(f"Status URL:  http://{webhook_host}:{webhook_port}/status")
    logger.info("Bot is running. Send alerts from TradingView to start trading.")
    logger.info("-" * 60)

    # ---- Order-flow autonomous trading --------------------------
    order_flow_enabled = _bool("ORDER_FLOW_ENABLED", False)

    tasks = [
        risk_manager.monitor(),
        _token_refresh_loop(client),
        run_server(app, host=webhook_host, port=webhook_port),
    ]

    if order_flow_enabled:
        logger.info("Order-flow autonomous trading: ENABLED")

        # Resolve contract ID for WebSocket subscription
        try:
            contract_info = await asyncio.to_thread(client.find_contract, front_month)
            contract_id   = contract_info.get("id", 0)
            if not contract_id:
                raise ValueError(f"find_contract returned no id for {front_month}")
            logger.info(f"Order-flow contract: {front_month}  id={contract_id}")
        except Exception as exc:
            logger.error(
                f"Could not resolve contract id for {front_month}: {exc}. "
                f"Order-flow disabled."
            )
            contract_id = 0

        if contract_id:
            strategy = OrderFlowStrategy(
                client                = client,
                risk_manager          = risk_manager,
                news_filter           = news_filter,
                session_filter        = session_filter,
                base_symbol           = symbol,
                qty                   = _int(  "ORDER_FLOW_QTY",            1),
                imbalance_ratio       = _float("ORDER_FLOW_IMBALANCE_RATIO", 3.0),
                delta_min             = _float("ORDER_FLOW_DELTA_MIN",       50.0),
                delta_lookback        = _int(  "ORDER_FLOW_DELTA_LOOKBACK",  30),
                large_print_threshold = _int(  "ORDER_FLOW_LARGE_PRINT",     100),
                dom_levels            = _int(  "ORDER_FLOW_DOM_LEVELS",       5),
                cooldown_seconds      = _int(  "ORDER_FLOW_COOLDOWN",         30),
                stop_loss_dollars     = _float("ORDER_FLOW_STOP_DOLLARS",  300.0),
                take_profit_dollars   = _float("ORDER_FLOW_TP_DOLLARS",    600.0),
                daily_profit_cap      = _float("ORDER_FLOW_DAILY_CAP",   1_000.0),
            )

            feed = MarketDataFeed(
                md_token = client.md_access_token,
                live     = live,
            )
            feed.on_dom   = strategy.on_dom_update
            feed.on_quote = strategy.on_quote_update

            tasks.append(feed.run(symbol=front_month, contract_id=contract_id))

            logger.info(
                f"Order-flow config — "
                f"qty={strategy.qty}  "
                f"imbalance={strategy.imbalance_ratio}:1  "
                f"delta_min={strategy.delta_min:+.0f}  "
                f"cooldown={strategy.cooldown_seconds}s  "
                f"SL=${strategy.stop_loss_dollars:,.0f}  "
                f"TP=${strategy.take_profit_dollars:,.0f}  "
                f"daily_cap=${strategy.daily_profit_cap:,.0f}"
            )
    else:
        logger.info(
            "Order-flow autonomous trading: DISABLED "
            "(set ORDER_FLOW_ENABLED=true to enable)"
        )

    # ---- Run everything concurrently ----------------------------
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C).")
