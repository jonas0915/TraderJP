"""Tests for order_manager — signal routing and order placement."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.order_manager import OrderManager, TradeSignal
from bot.risk_manager import RiskManager, ApexConfig
from bot.news_filter import NewsFilter
from bot.session_filter import SessionFilter, SessionConfig
from bot.tradovate_client import TradovateError


def _make_order_manager() -> OrderManager:
    """Create an OrderManager with fully mocked dependencies."""
    client = MagicMock()
    client.get_positions.return_value = []
    client.place_market_order.return_value = {"orderId": 1, "ordStatus": "Working"}
    client.place_oso_order.return_value = {"orderId": 2, "ordStatus": "Working"}
    client.place_limit_order.return_value = {"orderId": 3, "ordStatus": "Working"}
    client.place_stop_order.return_value = {"orderId": 4, "ordStatus": "Working"}
    client.liquidate_position.return_value = {"ordStatus": "Filled"}
    client.cancel_all_orders.return_value = None

    with patch.object(RiskManager, "_load_persisted_state"):
        rm = RiskManager(client=client, config=ApexConfig(max_contracts=4))
    rm._initialized = True
    rm._peak_equity = 50_000.0
    rm._day.starting_balance = 50_000.0

    nf = NewsFilter(enabled=False)
    # Mock session filter to always allow trading regardless of actual time
    sf = MagicMock(spec=SessionFilter)
    sf.is_trading_allowed.return_value = (True, "Test mode")

    om = OrderManager(
        client=client,
        risk_manager=rm,
        news_filter=nf,
        session_filter=sf,
        base_symbol="ES",
        default_qty=1,
    )
    return om


class TestSignalNormalization:
    def test_buy_action(self):
        assert OrderManager._NORMALISE.get("buy") == "buy"
        assert OrderManager._NORMALISE.get("long") == "buy"

    def test_sell_action(self):
        assert OrderManager._NORMALISE.get("sell") == "sell"
        assert OrderManager._NORMALISE.get("short") == "sell"

    def test_close_actions(self):
        for action in ("close", "flatten", "close_all", "closeall", "exit"):
            assert OrderManager._NORMALISE.get(action) == "close"

    def test_unknown_action(self):
        assert OrderManager._NORMALISE.get("invalid") is None


@pytest.mark.asyncio
class TestHandleSignalBuy:
    async def test_simple_buy(self):
        om = _make_order_manager()
        signal = TradeSignal(action="buy", qty=1)
        result = await om.handle_signal(signal)
        assert result["success"] is True
        om.client.place_market_order.assert_called_once()

    async def test_buy_with_oso_brackets(self):
        om = _make_order_manager()
        signal = TradeSignal(action="buy", qty=1, stop_loss=5000.0, take_profit=5100.0)
        result = await om.handle_signal(signal)
        assert result["success"] is True
        # Should use OSO, not separate bracket orders
        om.client.place_oso_order.assert_called_once()
        om.client.place_market_order.assert_not_called()

    async def test_buy_duplicate_blocked(self):
        om = _make_order_manager()
        om.client.get_positions.return_value = [
            {"accountId": om.client.account_id, "symbol": "ESH26", "netPos": 1}
        ]
        signal = TradeSignal(action="buy", qty=1, symbol="ESH26")
        result = await om.handle_signal(signal)
        assert result["success"] is False
        assert "already" in result["message"].lower()


@pytest.mark.asyncio
class TestHandleSignalSell:
    async def test_simple_sell(self):
        om = _make_order_manager()
        signal = TradeSignal(action="sell", qty=1)
        result = await om.handle_signal(signal)
        assert result["success"] is True

    async def test_sell_duplicate_blocked(self):
        om = _make_order_manager()
        om.client.get_positions.return_value = [
            {"accountId": om.client.account_id, "symbol": "ESH26", "netPos": -1}
        ]
        signal = TradeSignal(action="sell", qty=1, symbol="ESH26")
        result = await om.handle_signal(signal)
        assert result["success"] is False


@pytest.mark.asyncio
class TestHandleSignalClose:
    async def test_close_with_position(self):
        om = _make_order_manager()
        om.client.get_positions.return_value = [
            {"accountId": om.client.account_id, "symbol": "ESH26", "netPos": 2}
        ]
        signal = TradeSignal(action="close", qty=1, symbol="ESH26")
        result = await om.handle_signal(signal)
        assert result["success"] is True
        om.client.liquidate_position.assert_called_once()

    async def test_close_no_position(self):
        om = _make_order_manager()
        signal = TradeSignal(action="close", qty=1)
        result = await om.handle_signal(signal)
        assert result["success"] is False
        assert "no open position" in result["message"].lower()


@pytest.mark.asyncio
class TestRiskBlocking:
    async def test_daily_loss_blocks_entry(self):
        om = _make_order_manager()
        om.risk_manager._day.realized_pnl = -500.0
        om.risk_manager._day.unrealized_pnl = -100.0
        signal = TradeSignal(action="buy", qty=1)
        result = await om.handle_signal(signal)
        assert result["success"] is False
        assert "risk" in result["message"].lower()

    async def test_position_size_blocks_entry(self):
        om = _make_order_manager()
        om.client.get_positions.return_value = [
            {"accountId": om.client.account_id, "symbol": "ESH26", "netPos": -3}
        ]
        # Short 3 contracts, trying to sell 2 more = total 5 > max 4
        signal = TradeSignal(action="sell", qty=2, symbol="ESH26")
        # This will be blocked by the "already short" check since we're
        # trying to add to a short position, which is caught before size check.
        result = await om.handle_signal(signal)
        assert result["success"] is False


@pytest.mark.asyncio
class TestUnknownAction:
    async def test_unknown_action_rejected(self):
        om = _make_order_manager()
        signal = TradeSignal(action="invalid_action", qty=1)
        result = await om.handle_signal(signal)
        assert result["success"] is False
        assert "unknown" in result["message"].lower()


@pytest.mark.asyncio
class TestTradovateErrorHandling:
    async def test_entry_order_failure(self):
        om = _make_order_manager()
        om.client.place_market_order.side_effect = TradovateError("API error")
        signal = TradeSignal(action="buy", qty=1)
        result = await om.handle_signal(signal)
        assert result["success"] is False
        assert "tradovate" in result["message"].lower()
