"""Format schedules for display."""

from .schedule import build_schedule


def format_schedule(tasks, days=7):
    """Return a schedule as a list of ``day N: task1, task2`` strings."""
    lines = []
    for index, day_tasks in enumerate(build_schedule(tasks, days)):
        if day_tasks:
            lines.append("day %d: %s" % (index + 1, ", ".join(day_tasks)))
    return lines
