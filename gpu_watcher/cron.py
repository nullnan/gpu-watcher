"""Small, dependency-free parser for standard five-field cron schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAY_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


@dataclass(frozen=True)
class CronExpression:
    """A parsed five-field cron expression.

    Fields are minute, hour, day of month, month, and day of week.  Day of
    week follows cron convention: Sunday is both 0 and 7.  Named months and
    weekdays, comma lists, ranges, and slash steps are supported.
    """

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    day_of_month_wildcard: bool
    day_of_week_wildcard: bool

    @classmethod
    def parse(cls, value: str) -> "CronExpression":
        expression = value.strip()
        if not expression:
            raise ValueError("cron expression cannot be empty")
        expression = _ALIASES.get(expression.lower(), expression)
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("cron expression must contain exactly 5 fields: minute hour day month weekday")
        minute, hour, day, month, weekday = fields
        return cls(
            expression=expression,
            minutes=frozenset(_parse_field(minute, 0, 59, "minute")),
            hours=frozenset(_parse_field(hour, 0, 23, "hour")),
            days_of_month=frozenset(_parse_field(day, 1, 31, "day of month")),
            months=frozenset(_parse_field(month, 1, 12, "month", _MONTH_NAMES)),
            days_of_week=frozenset(_parse_field(weekday, 0, 7, "day of week", _WEEKDAY_NAMES, weekday=True)),
            day_of_month_wildcard=day == "*",
            day_of_week_wildcard=weekday == "*",
        )

    def matches(self, moment: datetime) -> bool:
        """Return whether an aware or naive datetime matches this schedule."""
        cron_weekday = (moment.weekday() + 1) % 7  # Python Monday=0, cron Sunday=0.
        dom_matches = moment.day in self.days_of_month
        dow_matches = cron_weekday in self.days_of_week
        if self.day_of_month_wildcard or self.day_of_week_wildcard:
            day_matches = dom_matches and dow_matches
        else:
            # Traditional cron uses OR when both day fields are restricted.
            day_matches = dom_matches or dow_matches
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.month in self.months
            and day_matches
        )


def validate_cron_expression(value: str) -> str:
    """Validate and normalize an optional cron expression for task storage."""
    normalized = value.strip()
    if not normalized:
        return ""
    return CronExpression.parse(normalized).expression


def _parse_field(
    field: str,
    minimum: int,
    maximum: int,
    field_name: str,
    names: dict[str, int] | None = None,
    *,
    weekday: bool = False,
) -> set[int]:
    if not field:
        raise ValueError(f"cron {field_name} field cannot be empty")
    result: set[int] = set()
    for part in field.lower().split(","):
        if not part:
            raise ValueError(f"invalid cron {field_name} list")
        base, separator, step_text = part.partition("/")
        if separator:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"cron {field_name} step must be a positive integer")
            step = int(step_text)
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _parse_value(start_text, minimum, maximum, field_name, names, weekday, normalize_weekday=False)
            end = _parse_value(end_text, minimum, maximum, field_name, names, weekday, normalize_weekday=False)
            if start > end:
                raise ValueError(f"cron {field_name} range start must not exceed its end")
        else:
            if separator:
                raise ValueError(f"cron {field_name} steps require '*' or a range")
            result.add(_parse_value(base, minimum, maximum, field_name, names, weekday))
            continue
        values = range(start, end + 1, step)
        result.update(value % 7 if weekday else value for value in values)
    return result


def _parse_value(
    text: str,
    minimum: int,
    maximum: int,
    field_name: str,
    names: dict[str, int] | None,
    weekday: bool,
    *,
    normalize_weekday: bool = True,
) -> int:
    if names and text in names:
        value = names[text]
    elif text.isdigit():
        value = int(text)
    else:
        raise ValueError(f"invalid cron {field_name} value: {text!r}")
    if not minimum <= value <= maximum:
        raise ValueError(f"cron {field_name} must be between {minimum} and {maximum}")
    if weekday and normalize_weekday and value == 7:
        return 0
    return value
