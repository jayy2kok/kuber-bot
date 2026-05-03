"""
NSE Trading Holiday Calendar.

Updated annually. Source: https://www.nseindia.com/
Last updated: 2026-05-01
"""

from datetime import date

# NSE trading holidays for each year (excludes weekends)
NSE_HOLIDAYS: dict[int, set[date]] = {
    2025: {
        date(2025, 2, 26),   # Mahashivratri
        date(2025, 3, 14),   # Holi
        date(2025, 3, 31),   # Eid-ul-Fitr
        date(2025, 4, 10),   # Shri Ram Navami
        date(2025, 4, 14),   # Dr. Ambedkar Jayanti
        date(2025, 4, 18),   # Good Friday
        date(2025, 5, 1),    # Maharashtra Day
        date(2025, 6, 7),    # Eid-ul-Adha (Bakri Id)
        date(2025, 7, 6),    # Muharram
        date(2025, 8, 15),   # Independence Day
        date(2025, 8, 16),   # Janmashtami
        date(2025, 8, 27),   # Milad-un-Nabi
        date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra
        date(2025, 10, 21),  # Diwali-Laxmi Pujan
        date(2025, 10, 22),  # Diwali-Balipratipada
        date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev
        date(2025, 12, 25),  # Christmas
    },
    2026: {
        date(2026, 1, 26),   # Republic Day
        date(2026, 3, 3),    # Holi
        date(2026, 3, 26),   # Shri Ram Navami
        date(2026, 3, 31),   # Shri Mahavir Jayanti
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 14),   # Dr. Ambedkar Jayanti
        date(2026, 5, 1),    # Maharashtra Day
        date(2026, 5, 28),   # Bakri Id (Eid-ul-Adha)
        date(2026, 6, 26),   # Muharram
        date(2026, 9, 14),   # Ganesh Chaturthi
        date(2026, 10, 2),   # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 10),  # Diwali-Balipratipada
        date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
        date(2026, 12, 25),  # Christmas
    },
}


def is_nse_holiday(d: date) -> bool:
    """Check if a given date is an NSE trading holiday."""
    # Weekends are always non-trading
    if d.weekday() >= 5:
        return True
    # Check holiday calendar (if year not in calendar, assume no holidays)
    return d in NSE_HOLIDAYS.get(d.year, set())


def is_trading_day(d: date) -> bool:
    """Check if a given date is an NSE trading day."""
    return not is_nse_holiday(d)
