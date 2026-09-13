"""
Time utilities
Unified helpers for fetching the current time.
"""

import cnlunar
from datetime import datetime

# Weekday label injected into the system prompt ({{today_weekday}}). Identity map today;
# kept as a map so a deployment can localise it in one place.
WEEKDAY_MAP = {
    "Monday": "Monday",
    "Tuesday": "Tuesday",
    "Wednesday": "Wednesday",
    "Thursday": "Thursday",
    "Friday": "Friday",
    "Saturday": "Saturday",
    "Sunday": "Sunday",
}


def get_current_time() -> str:
    """
    Return the current time string (format: HH:MM).
    """
    return datetime.now().strftime("%H:%M")


def get_current_date() -> str:
    """
    Return today's date string (format: YYYY-MM-DD).
    """
    return datetime.now().strftime("%Y-%m-%d")


def get_current_weekday() -> str:
    """
    Return today's weekday name.
    """
    now = datetime.now()
    return WEEKDAY_MAP[now.strftime("%A")]


def get_current_lunar_date() -> str:
    """
    Return the lunar calendar date string.
    """
    try:
        now = datetime.now()
        today_lunar = cnlunar.Lunar(now, godType="8char")
        return "%s年%s%s" % (
            today_lunar.lunarYearCn,
            today_lunar.lunarMonthCn[:-1],
            today_lunar.lunarDayCn,
        )
    except Exception:
        return "Lunar date unavailable"


def get_current_time_info() -> tuple:
    """
    Return the current time info.
    Returns: (current time string, today's date, today's weekday, lunar date)
    """
    current_time = get_current_time()
    today_date = get_current_date()
    today_weekday = get_current_weekday()
    lunar_date = get_current_lunar_date()
    
    return current_time, today_date, today_weekday, lunar_date
