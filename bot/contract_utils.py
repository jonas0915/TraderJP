"""
ES futures contract symbol utilities.

ES is a quarterly contract that expires on the 3rd Friday of March, June,
September, and December. The front month rolls ~5 trading days before expiration.

Month codes: H=March, M=June, U=September, Z=December
"""

from datetime import date, timedelta


# Maps contract month to the calendar month (1-12) and letter code
_QUARTERS = [
    (3,  "H"),   # March
    (6,  "M"),   # June
    (9,  "U"),   # September
    (12, "Z"),   # December
]

ROLL_DAYS_BEFORE_EXPIRY = 5  # Roll to next contract N trading days before expiry


def _third_friday(year: int, month: int) -> date:
    """Return the date of the 3rd Friday of the given month/year."""
    # Find first day of month
    first = date(year, month, 1)
    # weekday(): Monday=0, Friday=4
    days_until_friday = (4 - first.weekday()) % 7
    first_friday = first + timedelta(days=days_until_friday)
    third_friday = first_friday + timedelta(weeks=2)
    return third_friday


def _expiry_date(year: int, month: int) -> date:
    return _third_friday(year, month)


def get_front_month_symbol(base: str = "ES", as_of: date = None) -> str:
    """
    Return the front-month ES contract symbol (e.g. 'ESH5', 'ESM5').

    We roll ROLL_DAYS_BEFORE_EXPIRY calendar days before the expiry date
    so we don't get caught holding into expiration.
    """
    today = as_of or date.today()
    year  = today.year

    # Check current and next year's quarters
    candidates = []
    for y in (year, year + 1):
        for month, code in _QUARTERS:
            exp = _expiry_date(y, month)
            roll_date = exp - timedelta(days=ROLL_DAYS_BEFORE_EXPIRY)
            candidates.append((roll_date, exp, y % 100, code))

    # Sort by roll_date ascending; pick first one whose roll_date is in the future
    candidates.sort(key=lambda x: x[0])
    for roll_date, exp, yr2, code in candidates:
        if today < roll_date:
            return f"{base}{code}{yr2}"

    # Fallback: last candidate (shouldn't happen)
    *_, (_, _, yr2, code) = candidates
    return f"{base}{code}{yr2}"


def all_active_symbols(base: str = "ES") -> list[str]:
    """
    Return the current front month + next two contracts (useful for
    setting up subscriptions or checking which symbol the exchange is using).
    """
    today = date.today()
    year  = today.year
    candidates = []
    for y in (year, year + 1):
        for month, code in _QUARTERS:
            exp = _expiry_date(y, month)
            candidates.append((exp, y % 100, code))
    candidates.sort(key=lambda x: x[0])
    future = [(yr2, code) for exp, yr2, code in candidates if exp >= today]
    return [f"{base}{code}{yr2}" for yr2, code in future[:3]]


if __name__ == "__main__":
    sym = get_front_month_symbol()
    print(f"Current front-month ES contract: {sym}")
    print(f"Active symbols: {all_active_symbols()}")
