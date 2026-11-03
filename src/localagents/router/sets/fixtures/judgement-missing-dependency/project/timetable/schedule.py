"""Build simple weekly schedules."""


def build_schedule(tasks, days=7):
    """Place ``tasks`` (a list of names) across ``days`` days, round-robin.

    Returns a list of ``days`` lists, one per day, holding the task names placed
    on that day.
    """
    schedule = [[] for _ in range(days)]
    for index, task in enumerate(tasks):
        schedule[index % days].append(task)
    return schedule
