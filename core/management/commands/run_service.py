import os
import signal
import subprocess

from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.core.management import BaseCommand, call_command
from django.utils import timezone

from core.monitoring import check_due_products


class Command(BaseCommand):
    help = "Runs migrations, embedded scheduler, and the single Gunicorn web worker."

    def handle(self, *args, **options):
        call_command("migrate", interactive=False)
        scheduler = None
        if settings.SCHEDULER_ENABLED:
            scheduler = BackgroundScheduler(timezone=settings.TIME_ZONE)
            scheduler.add_job(check_due_products, "interval", seconds=min(settings.MONITOR_INTERVAL_SECONDS, 30), id="stock-checks", max_instances=1, coalesce=True, next_run_time=timezone.now())
            scheduler.start()
        command = ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", str(settings.GUNICORN_TIMEOUT_SECONDS), "--access-logfile", "-", "--error-logfile", "-"]
        process = subprocess.Popen(command)

        def stop(*_):
            process.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            process.wait()
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
        if process.returncode:
            raise SystemExit(process.returncode)
