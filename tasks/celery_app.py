"""Celery app configuration."""
from __future__ import annotations

from celery import Celery

from app.config import settings


celery_app = Celery(
    "doomscroller",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["tasks.pipeline"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "scrape-cycle": {
            "task": "tasks.pipeline.scrape_task",
            "schedule": settings.scrape_interval_hours * 3600.0,
        },
    },
)
