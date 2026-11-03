"""timetable: build and format simple weekly schedules."""

from .format import format_schedule
from .schedule import build_schedule

__all__ = ["build_schedule", "format_schedule"]
