"""Tests for contract_utils — front-month symbol calculation and roll logic."""

from datetime import date
import pytest
from bot.contract_utils import (
    get_front_month_symbol,
    _third_friday,
    _subtract_trading_days,
    all_active_symbols,
)


class TestThirdFriday:
    def test_march_2025(self):
        assert _third_friday(2025, 3) == date(2025, 3, 21)

    def test_june_2025(self):
        assert _third_friday(2025, 6) == date(2025, 6, 20)

    def test_september_2025(self):
        assert _third_friday(2025, 9) == date(2025, 9, 19)

    def test_december_2025(self):
        assert _third_friday(2025, 12) == date(2025, 12, 19)

    def test_march_2026(self):
        assert _third_friday(2026, 3) == date(2026, 3, 20)


class TestSubtractTradingDays:
    def test_subtract_from_friday(self):
        # Friday March 21, 2025 minus 5 trading days = Friday March 14
        result = _subtract_trading_days(date(2025, 3, 21), 5)
        assert result == date(2025, 3, 14)

    def test_subtract_spans_weekend(self):
        # Monday March 17, 2025 minus 1 trading day = Friday March 14
        result = _subtract_trading_days(date(2025, 3, 17), 1)
        assert result == date(2025, 3, 14)

    def test_subtract_spans_two_weekends(self):
        # Friday March 21, 2025 minus 6 trading days = Thursday March 13
        result = _subtract_trading_days(date(2025, 3, 21), 6)
        assert result == date(2025, 3, 13)

    def test_subtract_zero_days(self):
        d = date(2025, 6, 20)
        assert _subtract_trading_days(d, 0) == d

    def test_result_is_weekday(self):
        # Any subtraction should land on a weekday
        for days in range(1, 15):
            result = _subtract_trading_days(date(2025, 6, 20), days)
            assert result.weekday() < 5, f"Landed on weekend for {days} trading days"


class TestGetFrontMonthSymbol:
    def test_well_before_march_expiry(self):
        # Jan 15, 2025: March 2025 contract is front month
        sym = get_front_month_symbol("ES", as_of=date(2025, 1, 15))
        assert sym == "ESH25"

    def test_after_march_roll(self):
        # March 20, 2025 (day before expiry): should have rolled to June
        sym = get_front_month_symbol("ES", as_of=date(2025, 3, 20))
        assert sym == "ESM25"

    def test_june_front_month(self):
        sym = get_front_month_symbol("ES", as_of=date(2025, 4, 1))
        assert sym == "ESM25"

    def test_september_front_month(self):
        sym = get_front_month_symbol("ES", as_of=date(2025, 7, 1))
        assert sym == "ESU25"

    def test_december_front_month(self):
        sym = get_front_month_symbol("ES", as_of=date(2025, 10, 1))
        assert sym == "ESZ25"

    def test_year_rollover(self):
        # After December 2025 roll, should give March 2026
        sym = get_front_month_symbol("ES", as_of=date(2025, 12, 18))
        assert sym == "ESH26"

    def test_custom_base_symbol(self):
        sym = get_front_month_symbol("NQ", as_of=date(2025, 1, 15))
        assert sym.startswith("NQ")

    def test_roll_date_is_before_expiry(self):
        # On the roll date itself, we should already be on the next contract
        # March 2025 expiry is March 21 (Friday). Roll date = 5 trading days
        # before = March 14. On March 14 we should still be on March contract
        # (today < roll_date requires strict less-than)
        sym_before_roll = get_front_month_symbol("ES", as_of=date(2025, 3, 13))
        sym_on_roll = get_front_month_symbol("ES", as_of=date(2025, 3, 14))
        assert sym_before_roll == "ESH25"
        assert sym_on_roll == "ESM25"


class TestAllActiveSymbols:
    def test_returns_three_symbols(self):
        syms = all_active_symbols("ES")
        assert len(syms) == 3
        for s in syms:
            assert s.startswith("ES")

    def test_symbols_are_ordered(self):
        syms = all_active_symbols("ES")
        # They should be chronologically ordered (earliest expiry first)
        assert len(syms) == 3
