"""Calendar used to choose reporting weeks, independent of market bar timezones."""

import os
from datetime import date, datetime, timedelta
from typing import Optional, Union
from zoneinfo import ZoneInfo

REPORTING_TIMEZONE = ZoneInfo(os.environ.get("REPORT_TIMEZONE", "Asia/Singapore"))


def get_previous_full_week(as_of: Optional[Union[date, datetime]] = None):
    """Return naive Monday/Friday dates for the latest completed reporting week.

    An aware datetime is converted to the reporting timezone. A date is already
    a reporting-calendar date. Weekdays exclude the current, incomplete week;
    weekends include the week that just ended. Market session timestamps retain
    their own timezone when these calendar dates are used for detection.
    """
    if as_of is None:
        today = datetime.now(REPORTING_TIMEZONE).date()
    elif isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of datetime must include a timezone; use date for a calendar day")
        today = as_of.astimezone(REPORTING_TIMEZONE).date()
    else:
        today = as_of

    weekday = today.weekday()
    days_since_friday = weekday - 4 if weekday >= 5 else weekday + 3
    friday = today - timedelta(days=days_since_friday)
    monday = friday - timedelta(days=4)
    return datetime.combine(monday, datetime.min.time()), datetime.combine(friday, datetime.min.time())
