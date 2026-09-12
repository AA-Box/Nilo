from datetime import datetime
import cnlunar
from plugins_func.register import register_function, ToolType, ActionResponse, Action

get_lunar_function_desc = {
    "type": "function",
    "function": {
        "name": "get_lunar",
        "description": (
            "Returns Chinese lunar calendar and almanac information for a specific date. "
            "The user may specify what to look up, e.g. lunar date, sexagenary (stems and branches), "
            "solar term, zodiac animal, star sign, eight characters (bazi), auspicious/inauspicious activities. "
            "If nothing is specified, the sexagenary year and lunar date are returned by default. "
            "For basic queries such as 'what is today's lunar date', use the information already in the "
            "context instead of calling this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date to look up in YYYY-MM-DD format, e.g. 2024-01-01. Defaults to the current date if omitted",
                },
                "query": {
                    "type": "string",
                    "description": "What to look up, e.g. lunar date, sexagenary, festivals, solar term, zodiac animal, star sign, eight characters, auspicious/inauspicious activities",
                },
            },
            "required": [],
        },
    },
}


@register_function("get_lunar", get_lunar_function_desc, ToolType.WAIT)
def get_lunar(date=None, query=None):
    """
    Get the lunar calendar date plus almanac info: sexagenary cycle, solar term,
    zodiac animal, star sign, eight characters, auspicious/inauspicious activities.
    """
    from core.utils.cache.manager import cache_manager, CacheType

    # Use the given date if provided, otherwise the current date
    if date:
        try:
            now = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return ActionResponse(
                Action.REQLLM,
                f"Invalid date format; use YYYY-MM-DD, e.g. 2024-01-01",
                None,
            )
    else:
        now = datetime.now()

    current_date = now.strftime("%Y-%m-%d")

    # Fall back to the default query text when none is given
    if query is None:
        query = "the sexagenary year and lunar date (default)"

    # Try the cache first
    lunar_cache_key = f"lunar_info_{current_date}"
    cached_lunar_info = cache_manager.get(CacheType.LUNAR, lunar_cache_key)
    if cached_lunar_info:
        return ActionResponse(Action.REQLLM, cached_lunar_info, None)

    response_text = f"Use the following information to answer the user's request, focusing on {query}:\n"

    lunar = cnlunar.Lunar(now, godType="8char")
    response_text += (
        "Lunar calendar info:\n"
        "Lunar date: year %s, month %s, day %s\n" % (lunar.lunarYearCn, lunar.lunarMonthCn[:-1], lunar.lunarDayCn)
        + "Sexagenary: year %s, month %s, day %s\n" % (lunar.year8Char, lunar.month8Char, lunar.day8Char)
        + "Zodiac animal: %s\n" % (lunar.chineseYearZodiac)
        + "Eight characters (bazi): %s\n"
        % (
            " ".join(
                [lunar.year8Char, lunar.month8Char, lunar.day8Char, lunar.twohour8Char]
            )
        )
        + "Festivals today: %s\n"
        % (
            ",".join(
                filter(
                    None,
                    (
                        lunar.get_legalHolidays(),
                        lunar.get_otherHolidays(),
                        lunar.get_otherLunarHolidays(),
                    ),
                )
            )
        )
        + "Solar term today: %s\n" % (lunar.todaySolarTerms)
        + "Next solar term: %s on %s-%s-%s\n"
        % (
            lunar.nextSolarTerm,
            lunar.nextSolarTermYear,
            lunar.nextSolarTermDate[0],
            lunar.nextSolarTermDate[1],
        )
        + "Solar terms this year (month/day): %s\n"
        % (
            ", ".join(
                [
                    f"{term}({date[0]}/{date[1]})"
                    for term, date in lunar.thisYearSolarTermsDic.items()
                ]
            )
        )
        + "Zodiac clash: %s\n" % (lunar.chineseZodiacClash)
        + "Star sign: %s\n" % (lunar.starZodiac)
        + "Nayin: %s\n" % lunar.get_nayin()
        + "Pengzu taboos: %s\n" % (lunar.get_pengTaboo(delimit=", "))
        + "Day officer: %s presiding\n" % lunar.get_today12DayOfficer()[0]
        + "Day deity: %s (%s)\n"
        % (lunar.get_today12DayOfficer()[1], lunar.get_today12DayOfficer()[2])
        + "Lunar mansion (28 mansions): %s\n" % lunar.get_the28Stars()
        + "Auspicious directions: %s\n" % " ".join(lunar.get_luckyGodsDirection())
        + "Fetal god today: %s\n" % lunar.get_fetalGod()
        + "Auspicious for: %s\n" % ", ".join(lunar.goodThing[:10])
        + "Inauspicious for: %s\n" % ", ".join(lunar.badThing[:10])
        + "(By default return only the sexagenary year and lunar date; include auspicious/inauspicious activities only when asked.)"
    )

    # Cache the result
    cache_manager.set(CacheType.LUNAR, lunar_cache_key, response_text)

    return ActionResponse(Action.REQLLM, response_text, None)
