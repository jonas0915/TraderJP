"""Tests for news_filter — economic calendar blackout enforcement."""

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytz
import pytest
from bot.news_filter import NewsFilter, NewsEvent

UTC = pytz.utc
ET = pytz.timezone("America/New_York")


def _utc(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_filter(**kwargs) -> NewsFilter:
    defaults = dict(
        enabled=True,
        blackout_before_min=10,
        blackout_after_min=5,
        impact_levels=("High",),
        countries=("USD",),
    )
    defaults.update(kwargs)
    nf = NewsFilter(**defaults)
    # Prevent actual HTTP calls — set last_refresh far in the future so
    # _maybe_refresh never triggers during tests.
    nf._last_refresh = datetime(2099, 1, 1, tzinfo=UTC)
    nf._refresh_failures = 0
    return nf


class TestNewsBlackout:
    def test_no_events_no_blackout(self):
        nf = _make_filter()
        nf._events = []
        blocked, _ = nf.is_news_blackout(_utc(2025, 3, 4, 14, 0))
        assert blocked is False

    def test_blackout_before_event(self):
        event_time = _utc(2025, 3, 4, 14, 30)
        nf = _make_filter(blackout_before_min=10)
        nf._events = [NewsEvent("NFP", "USD", event_time, "High")]
        # 5 minutes before event — inside blackout
        blocked, reason = nf.is_news_blackout(_utc(2025, 3, 4, 14, 25))
        assert blocked is True
        assert "NFP" in reason

    def test_blackout_after_event(self):
        event_time = _utc(2025, 3, 4, 14, 30)
        nf = _make_filter(blackout_after_min=5)
        nf._events = [NewsEvent("CPI", "USD", event_time, "High")]
        # 3 minutes after event — inside blackout
        blocked, _ = nf.is_news_blackout(_utc(2025, 3, 4, 14, 33))
        assert blocked is True

    def test_no_blackout_well_before_event(self):
        event_time = _utc(2025, 3, 4, 14, 30)
        nf = _make_filter(blackout_before_min=10)
        nf._events = [NewsEvent("FOMC", "USD", event_time, "High")]
        # 15 minutes before — outside blackout
        blocked, _ = nf.is_news_blackout(_utc(2025, 3, 4, 14, 15))
        assert blocked is False

    def test_no_blackout_well_after_event(self):
        event_time = _utc(2025, 3, 4, 14, 30)
        nf = _make_filter(blackout_after_min=5)
        nf._events = [NewsEvent("FOMC", "USD", event_time, "High")]
        # 10 minutes after — outside blackout
        blocked, _ = nf.is_news_blackout(_utc(2025, 3, 4, 14, 40))
        assert blocked is False

    def test_disabled_filter(self):
        nf = NewsFilter(enabled=False)
        nf._events = [NewsEvent("NFP", "USD", _utc(2025, 3, 4, 14, 30), "High")]
        blocked, _ = nf.is_news_blackout(_utc(2025, 3, 4, 14, 25))
        assert blocked is False


class TestStalenessDetection:
    def test_stale_calendar_blocks_trading(self):
        nf = _make_filter()
        nf._events = []
        nf._refresh_failures = 3
        now = datetime.now(UTC)
        # Set last refresh to 7 hours before now
        nf._last_refresh = now - timedelta(hours=7)
        # Patch _maybe_refresh so it doesn't reset our intentionally stale state
        with patch.object(nf, "_maybe_refresh"):
            blocked, reason = nf.is_news_blackout(now=now)
        assert blocked is True
        assert "stale" in reason.lower()

    def test_fresh_calendar_does_not_block(self):
        nf = _make_filter()
        nf._events = []
        nf._refresh_failures = 0
        now = datetime.now(UTC)
        nf._last_refresh = now - timedelta(minutes=30)
        blocked, _ = nf.is_news_blackout(now=now)
        assert blocked is False

    def test_few_failures_does_not_block(self):
        nf = _make_filter()
        nf._events = []
        nf._refresh_failures = 2  # Below threshold of 3
        now = datetime.now(UTC)
        nf._last_refresh = now - timedelta(hours=7)
        blocked, _ = nf.is_news_blackout(now=now)
        assert blocked is False


class TestNextEventMinutes:
    def test_no_events(self):
        nf = _make_filter()
        nf._events = []
        assert nf.next_event_minutes(_utc(2025, 3, 4, 12, 0)) is None

    def test_upcoming_event(self):
        event_time = _utc(2025, 3, 4, 14, 30)
        nf = _make_filter()
        nf._events = [NewsEvent("NFP", "USD", event_time, "High")]
        now = _utc(2025, 3, 4, 14, 0)
        mins = nf.next_event_minutes(now)
        assert mins == pytest.approx(30.0)

    def test_past_events_ignored(self):
        past = _utc(2025, 3, 4, 10, 0)
        nf = _make_filter()
        nf._events = [NewsEvent("Old", "USD", past, "High")]
        result = nf.next_event_minutes(_utc(2025, 3, 4, 12, 0))
        assert result is None


class TestDateTimeParsing:
    def test_parse_standard_time(self):
        dt = NewsFilter._parse_dt("2025-03-04", "8:30am")
        assert dt is not None
        assert dt.tzinfo == UTC
        # 8:30 AM ET = 13:30 UTC (standard time)
        assert dt.hour == 13
        assert dt.minute == 30

    def test_parse_pm_time(self):
        dt = NewsFilter._parse_dt("2025-03-04", "2:00pm")
        assert dt is not None
        assert dt.hour == 19  # 2 PM ET = 19:00 UTC

    def test_parse_no_time(self):
        dt = NewsFilter._parse_dt("2025-03-04", "")
        assert dt is not None  # defaults to midnight ET

    def test_parse_invalid_date(self):
        dt = NewsFilter._parse_dt("not-a-date", "8:30am")
        assert dt is None
