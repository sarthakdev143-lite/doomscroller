from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable


class _Job:
    def __init__(self, id, func, trigger, next_run_time, max_instances=1, coalesce=True):
        self.id = id
        self.func = func
        self.trigger = trigger
        self.next_run_time = next_run_time
        self.max_instances = max_instances
        self.coalesce = coalesce
        self._running = False


class BackgroundScheduler:
    def __init__(self, timezone=None):
        self.timezone = timezone
        self._jobs: list[_Job] = []
        self._running = False
        self._thread = None

    def add_job(self, func, trigger=None, id=None, next_run_time=None, max_instances=1, coalesce=True):
        job = _Job(
            id=id,
            func=func,
            trigger=trigger,
            next_run_time=next_run_time,
            max_instances=max_instances,
            coalesce=coalesce,
        )
        self._jobs.append(job)
        return job

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        while self._running:
            now = datetime.now(timezone.utc)
            for job in self._jobs:
                if not self._running:
                    break
                if job.next_run_time is not None and job.next_run_time <= now and not job._running:
                    job._running = True
                    try:
                        job.func()
                    except Exception:
                        pass
                    finally:
                        job._running = False
                    if job.trigger is not None and hasattr(job.trigger, 'get_next_fire_time'):
                        job.next_run_time = job.trigger.get_next_fire_time(job.next_run_time, now)
            import time
            time.sleep(1)

    def shutdown(self, wait=False):
        self._running = False
        if wait and self._thread:
            self._thread.join()

    def get_jobs(self):
        return list(self._jobs)