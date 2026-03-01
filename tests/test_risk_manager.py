"""Tests for risk_manager — Apex limit enforcement."""

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bot.risk_manager import RiskManager, ApexConfig, DayStats, _PEAK_STATE_FILE


def _make_risk_manager(**config_overrides) -> RiskManager:
    """Create a RiskManager with a mock client and optional config overrides."""
    client = MagicMock()
    defaults = dict(
        daily_loss_limit=500.0,
        max_trailing_dd=2500.0,
        max_contracts=4,
        consistency_rule=False,
    )
    defaults.update(config_overrides)
    config = ApexConfig(**defaults)
    # Patch out file I/O during construction
    with patch.object(RiskManager, "_load_persisted_state"):
        rm = RiskManager(client=client, config=config)
    rm._peak_equity = 50_000.0
    rm._initialized = True
    rm._day = DayStats(
        date=date.today(),
        starting_balance=50_000.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
    )
    return rm


class TestCheckOrderAllowed:
    def test_normal_order_allowed(self):
        rm = _make_risk_manager()
        allowed, reason = rm.check_order_allowed(qty=1)
        assert allowed is True
        assert reason == "OK"

    def test_qty_exceeds_max_contracts(self):
        rm = _make_risk_manager(max_contracts=4)
        allowed, reason = rm.check_order_allowed(qty=5)
        assert allowed is False
        assert "max contracts" in reason.lower()

    def test_daily_loss_at_limit(self):
        rm = _make_risk_manager(daily_loss_limit=500.0)
        rm._day.realized_pnl = -500.0
        rm._day.unrealized_pnl = 0.0
        allowed, reason = rm.check_order_allowed(qty=1)
        assert allowed is False
        assert "daily loss" in reason.lower()

    def test_daily_loss_includes_unrealized(self):
        rm = _make_risk_manager(daily_loss_limit=500.0)
        rm._day.realized_pnl = -200.0
        rm._day.unrealized_pnl = -300.0
        allowed, reason = rm.check_order_allowed(qty=1)
        assert allowed is False

    def test_trailing_dd_at_limit(self):
        rm = _make_risk_manager(max_trailing_dd=2500.0)
        rm._peak_equity = 52_500.0
        rm._day.starting_balance = 50_000.0
        rm._day.realized_pnl = 0.0
        rm._day.unrealized_pnl = 0.0
        allowed, reason = rm.check_order_allowed(qty=1)
        assert allowed is False
        assert "trailing drawdown" in reason.lower()

    def test_locked_after_flatten(self):
        rm = _make_risk_manager()
        rm._day.flattened_today = True
        allowed, reason = rm.check_order_allowed(qty=1)
        assert allowed is False
        assert "locked" in reason.lower()

    def test_profitable_day_allowed(self):
        rm = _make_risk_manager()
        rm._day.realized_pnl = 400.0
        rm._day.unrealized_pnl = 100.0
        allowed, _ = rm.check_order_allowed(qty=1)
        assert allowed is True


class TestCheckPositionSizeOk:
    def test_within_limit(self):
        rm = _make_risk_manager(max_contracts=4)
        ok, _ = rm.check_position_size_ok(current_qty=2, new_qty=1)
        assert ok is True

    def test_at_limit(self):
        rm = _make_risk_manager(max_contracts=4)
        ok, _ = rm.check_position_size_ok(current_qty=2, new_qty=2)
        assert ok is True

    def test_exceeds_limit(self):
        rm = _make_risk_manager(max_contracts=4)
        ok, reason = rm.check_position_size_ok(current_qty=3, new_qty=2)
        assert ok is False
        assert "exceed" in reason.lower()

    def test_short_position_counted(self):
        rm = _make_risk_manager(max_contracts=4)
        # Short 3 contracts + adding 2 = total 5
        ok, _ = rm.check_position_size_ok(current_qty=-3, new_qty=2)
        assert ok is False


class TestConsistencyRule:
    def test_disabled_by_default(self):
        rm = _make_risk_manager(consistency_rule=False)
        rm._day.realized_pnl = 5000.0
        rm._cumulative_net_profit = 1000.0
        allowed, _ = rm.check_order_allowed(qty=1)
        assert allowed is True

    def test_blocks_when_day_exceeds_pct(self):
        rm = _make_risk_manager(consistency_rule=True, max_day_profit_pct=0.30)
        rm._cumulative_net_profit = 3000.0
        rm._day.realized_pnl = 2000.0  # 2000 / 5000 = 40% > 30%
        rm._day.unrealized_pnl = 0.0
        allowed, reason = rm.check_order_allowed(qty=1)
        assert allowed is False
        assert "consistency" in reason.lower()

    def test_allows_when_under_pct(self):
        rm = _make_risk_manager(consistency_rule=True, max_day_profit_pct=0.30)
        rm._cumulative_net_profit = 10_000.0
        rm._day.realized_pnl = 500.0  # 500 / 10500 = 4.7% < 30%
        rm._day.unrealized_pnl = 0.0
        allowed, _ = rm.check_order_allowed(qty=1)
        assert allowed is True

    def test_losing_day_not_blocked(self):
        rm = _make_risk_manager(consistency_rule=True, max_day_profit_pct=0.30)
        rm._cumulative_net_profit = 3000.0
        rm._day.realized_pnl = -200.0
        rm._day.unrealized_pnl = 0.0
        allowed, _ = rm.check_order_allowed(qty=1)
        assert allowed is True

    def test_no_cumulative_profit_not_blocked(self):
        rm = _make_risk_manager(consistency_rule=True, max_day_profit_pct=0.30)
        rm._cumulative_net_profit = 0.0
        rm._day.realized_pnl = 500.0
        rm._day.unrealized_pnl = 0.0
        allowed, _ = rm.check_order_allowed(qty=1)
        assert allowed is True


class TestDailyLossCalculation:
    def test_no_loss(self):
        rm = _make_risk_manager()
        rm._day.realized_pnl = 100.0
        rm._day.unrealized_pnl = 50.0
        assert rm._current_daily_loss() == 0.0

    def test_combined_loss(self):
        rm = _make_risk_manager()
        rm._day.realized_pnl = -200.0
        rm._day.unrealized_pnl = -150.0
        assert rm._current_daily_loss() == 350.0

    def test_mixed_pnl(self):
        rm = _make_risk_manager()
        rm._day.realized_pnl = 100.0
        rm._day.unrealized_pnl = -400.0
        assert rm._current_daily_loss() == 300.0


class TestTrailingDrawdown:
    def test_no_drawdown(self):
        rm = _make_risk_manager()
        rm._peak_equity = 50_000.0
        rm._day.starting_balance = 50_000.0
        assert rm._current_trailing_dd() == 0.0

    def test_drawdown_from_peak(self):
        rm = _make_risk_manager()
        rm._peak_equity = 52_000.0
        rm._day.starting_balance = 50_000.0
        rm._day.realized_pnl = 0.0
        rm._day.unrealized_pnl = -500.0
        # Current equity = 49,500. Peak = 52,000. DD = 2,500
        assert rm._current_trailing_dd() == 2500.0


class TestGetStatus:
    def test_returns_all_keys(self):
        rm = _make_risk_manager()
        status = rm.get_status()
        expected_keys = {
            "date", "starting_balance", "realized_pnl", "unrealized_pnl",
            "daily_loss", "daily_loss_limit", "daily_loss_pct",
            "trailing_drawdown", "trailing_dd_limit", "trailing_dd_pct",
            "peak_equity", "cumulative_profit", "profit_target",
            "locked_today", "max_contracts",
        }
        assert set(status.keys()) == expected_keys

    def test_status_values_are_numeric(self):
        rm = _make_risk_manager()
        status = rm.get_status()
        for key in ("daily_loss", "trailing_drawdown", "peak_equity"):
            assert isinstance(status[key], (int, float))


class TestPeakEquityPersistence:
    def test_save_and_load(self, tmp_path):
        state_file = tmp_path / "risk_state.json"
        with patch("bot.risk_manager._PEAK_STATE_FILE", state_file):
            client = MagicMock()
            config = ApexConfig()
            rm = RiskManager(client=client, config=config)
            rm._peak_equity = 55_000.0
            rm._cumulative_net_profit = 1234.56
            rm._save_state()

            assert state_file.exists()
            data = json.loads(state_file.read_text())
            assert data["peak_equity"] == 55_000.0
            assert data["cumulative_net_profit"] == 1234.56

            # Reload
            rm2 = RiskManager(client=client, config=config)
            assert rm2._peak_equity == 55_000.0
            assert rm2._cumulative_net_profit == 1234.56
