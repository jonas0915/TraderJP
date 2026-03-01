"""Tests for session_filter — trading hours enforcement."""

from datetime import datetime
import pytz
import pytest
from bot.session_filter import SessionFilter, SessionConfig

CT = pytz.timezone("America/Chicago")


def _ct(year, month, day, hour, minute):
    """Helper to create a CT-aware datetime."""
    naive = datetime(year, month, day, hour, minute)
    return CT.localize(naive)


class TestSessionFilterRTH:
    """Tests with default config: RTH=True, ETH=False."""

    def setup_method(self):
        self.sf = SessionFilter(SessionConfig(trade_rth=True, trade_eth=False))

    def test_rth_open_allowed(self):
        # Tuesday 10:00 CT — inside RTH
        allowed, reason = self.sf.is_trading_allowed(_ct(2025, 3, 4, 10, 0))
        assert allowed is True
        assert "RTH" in reason

    def test_rth_start_boundary(self):
        # 08:30 CT exactly — should be allowed
        allowed, _ = self.sf.is_trading_allowed(_ct(2025, 3, 4, 8, 30))
        assert allowed is True

    def test_before_rth(self):
        # 08:29 CT — ETH, not allowed
        allowed, _ = self.sf.is_trading_allowed(_ct(2025, 3, 4, 8, 29))
        assert allowed is False

    def test_after_rth(self):
        # 15:15 CT — maintenance break
        allowed, reason = self.sf.is_trading_allowed(_ct(2025, 3, 4, 15, 15))
        assert allowed is False
        assert "maintenance" in reason.lower() or "break" in reason.lower()

    def test_evening_blocked(self):
        # 20:00 CT — ETH, blocked
        allowed, _ = self.sf.is_trading_allowed(_ct(2025, 3, 4, 20, 0))
        assert allowed is False

    def test_weekend_blocked(self):
        # Saturday 12:00 CT
        allowed, reason = self.sf.is_trading_allowed(_ct(2025, 3, 1, 12, 0))
        assert allowed is False

    def test_sunday_before_open(self):
        # Sunday 10:00 CT — market not open yet (blocked by day-of-week filter)
        allowed, reason = self.sf.is_trading_allowed(_ct(2025, 3, 2, 10, 0))
        assert allowed is False
        assert "day" in reason.lower()

    def test_sunday_after_open(self):
        # Sunday 17:30 CT — Globex open, but ETH disabled
        allowed, _ = self.sf.is_trading_allowed(_ct(2025, 3, 2, 17, 30))
        assert allowed is False


class TestSessionFilterETH:
    """Tests with ETH enabled."""

    def setup_method(self):
        self.sf = SessionFilter(SessionConfig(trade_rth=True, trade_eth=True))

    def test_evening_allowed(self):
        # Tuesday 20:00 CT — ETH allowed
        allowed, reason = self.sf.is_trading_allowed(_ct(2025, 3, 4, 20, 0))
        assert allowed is True
        assert "ETH" in reason or "Globex" in reason

    def test_maintenance_break_still_blocked(self):
        # 15:20 CT — maintenance break blocks even with ETH
        allowed, _ = self.sf.is_trading_allowed(_ct(2025, 3, 4, 15, 20))
        assert allowed is False


class TestSessionFilterForceWindow:
    def test_force_window_inside(self):
        sf = SessionFilter(SessionConfig(
            trade_rth=True, trade_eth=False,
            force_start="09:00", force_end="14:00",
        ))
        allowed, _ = sf.is_trading_allowed(_ct(2025, 3, 4, 10, 0))
        assert allowed is True

    def test_force_window_outside(self):
        sf = SessionFilter(SessionConfig(
            trade_rth=True, trade_eth=False,
            force_start="09:00", force_end="14:00",
        ))
        # 14:30 is inside RTH but outside forced window
        allowed, reason = sf.is_trading_allowed(_ct(2025, 3, 4, 14, 30))
        assert allowed is False
        assert "forced" in reason.lower()


class TestCurrentSession:
    def setup_method(self):
        self.sf = SessionFilter()

    def test_rth(self):
        assert self.sf.current_session(_ct(2025, 3, 4, 10, 0)) == "RTH"

    def test_break(self):
        assert self.sf.current_session(_ct(2025, 3, 4, 15, 20)) == "BREAK"

    def test_eth(self):
        assert self.sf.current_session(_ct(2025, 3, 4, 20, 0)) == "ETH"
