"""Celery application (worker entrypoint: `celery -A celery_app.celery worker ...`)."""
import logging

from celery import Celery
from celery.signals import worker_process_shutdown, worker_ready

from app import extensions
from app.config import load_settings

if not extensions.is_configured():
    extensions.configure(load_settings())
settings = extensions.get_settings()
extensions.configure_logging(settings.log_level)

celery = Celery("webpilot", broker=settings.redis_url, include=["app.tasks.jobs"])
_limit = settings.max_task_duration_seconds + 3600 + 300  # room for administrator think-time
celery.conf.update(
    task_default_queue=settings.celery_queue,
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    worker_prefetch_multiplier=1,
    task_acks_late=False,            # never auto-redeliver a task that may already have acted on a website
    worker_max_tasks_per_child=25,   # recycle processes: Chromium and OCR models leak over time
    task_time_limit=_limit,
    task_soft_time_limit=_limit - 120,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=5,
    broker_transport_options={"socket_timeout": 10, "socket_connect_timeout": 5, "visibility_timeout": 2 * 3600},
    worker_hijack_root_logger=False,
    enable_utc=True,
)


@worker_ready.connect
def _on_ready(**_):
    from app.services import task_service
    from app.tasks import jobs
    extensions.ensure_indexes()
    n = task_service.recover_abandoned()
    if n:
        logging.getLogger(__name__).warning("Marked %d abandoned task(s) as failed", n)
    jobs.cleanup_artifacts(force=True)


@worker_process_shutdown.connect
def _on_child_shutdown(**_):
    from app.tasks import jobs
    jobs.shutdown_runtime()
