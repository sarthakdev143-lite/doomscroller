from datetime import timedelta


class IntervalTrigger:
    def __init__(self, hours=None, minutes=None, seconds=None, **kwargs):
        self.hours = hours or 0
        self.minutes = minutes or 0
        self.seconds = seconds or 0
        self._interval = timedelta(hours=self.hours, minutes=self.minutes, seconds=self.seconds)

    def get_next_fire_time(self, previous_fire_time, now):
        if previous_fire_time is None:
            return now + self._interval
        return previous_fire_time + self._interval

    def __str__(self):
        return f"interval[hours={self.hours}]"