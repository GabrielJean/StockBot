from datetime import timedelta
import logging

from django.conf import settings
from django.db import transaction
from django.core.management import call_command
from django.utils import timezone

from .models import CheckResult, Monitor, NotificationDelivery, Product, SystemState
from .services import AdapterError, get_adapter_for_retailer, post_discord

logger = logging.getLogger(__name__)


def refresh_apple_catalog():
    """Refresh permissioned Apple configuration data without interrupting monitor checks."""
    try:
        call_command("import_apple_catalog", settings.APPLE_CATALOG_SOURCE_URL, verbosity=0)
    except Exception:
        logger.exception("Apple catalogue refresh failed")


def check_due_products():
    now = timezone.now()
    SystemState.objects.update_or_create(pk=1, defaults={"scheduler_heartbeat": now})
    products = Product.objects.filter(monitors__active=True).distinct().filter(next_check_at__isnull=True) | Product.objects.filter(monitors__active=True, next_check_at__lte=now).distinct()
    for product in products[:100]:
        check_product(product.id)
    CheckResult.objects.filter(checked_at__lt=now - timedelta(days=30)).delete()


def check_product(product_id):
    product = Product.objects.filter(id=product_id).first()
    if not product:
        return
    now = timezone.now()
    source = get_adapter_for_retailer(product.retailer)
    if product.retailer in {"bestbuy_ca", "apple_ca"} and source:
        monitors = list(product.monitors.select_related("webhook").filter(active=True))
        for monitor in monitors:
            check_monitor(monitor, product, source, now)
        product.next_check_at = now + timedelta(seconds=settings.MONITOR_INTERVAL_SECONDS)
        product.save(update_fields=["next_check_at", "updated_at"])
        return
    try:
        if not source:
            raise AdapterError("Retailer adapter is unavailable.")
        postal_code = product.monitors.filter(active=True).values_list("postal_code", flat=True).first() or ""
        snapshot = source.check(product, postal_code)
        status, message = snapshot.availability, ""
        success = True
    except AdapterError as exc:
        message = str(exc)
        status = Product.Availability.BLOCKED if "blocked" in message.lower() else Product.Availability.ERROR
        snapshot = None
        success = False
    with transaction.atomic():
        product = Product.objects.select_for_update().get(id=product_id)
        previous = product.availability
        product.last_checked_at = now
        product.availability = status
        product.last_error = message
        product.next_check_at = now + timedelta(seconds=settings.MONITOR_INTERVAL_SECONDS if success else min(settings.MONITOR_INTERVAL_SECONDS * 5, 300))
        if success:
            product.title = snapshot.title
            product.image_url = snapshot.image_url
            product.price = snapshot.price
            product.last_success_at = now
        product.save()
        CheckResult.objects.create(product=product, status=status, price=product.price, error=message)
        monitors = list(product.monitors.select_related("webhook").filter(active=True))
        if status == Product.Availability.UNAVAILABLE:
            for monitor in monitors:
                if not monitor.armed:
                    monitor.armed = True
                    monitor.save(update_fields=["armed"])
            return
        if status != Product.Availability.AVAILABLE or previous == Product.Availability.AVAILABLE:
            return
        for monitor in monitors:
            if not monitor.armed or not monitor.webhook.enabled:
                continue
            monitor.armed = False
            monitor.save(update_fields=["armed"])
            delivered, error = post_discord(monitor.webhook, product.title, product.canonical_url, product.price)
            NotificationDelivery.objects.create(monitor=monitor, delivered=delivered, error=error)


def check_monitor(monitor, product, source, now):
    try:
        snapshot = source.check(product, monitor.postal_code, monitor.fulfillment, monitor.location_keys)
        status, message = snapshot.availability, ""
    except AdapterError as exc:
        status = Product.Availability.BLOCKED if "blocked" in str(exc).lower() else Product.Availability.ERROR
        message = str(exc)
        snapshot = None
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().select_related("webhook").get(id=monitor.id)
        previous = monitor.availability
        monitor.availability = status
        monitor.last_checked_at = now
        monitor.last_error = message
        if status == Product.Availability.UNAVAILABLE:
            monitor.armed = True
        elif status == Product.Availability.AVAILABLE and previous != Product.Availability.AVAILABLE and monitor.armed and monitor.webhook.enabled:
            monitor.armed = False
            delivered, error = post_discord(monitor.webhook, product.title, product.canonical_url, product.price)
            NotificationDelivery.objects.create(monitor=monitor, delivered=delivered, error=error)
        monitor.save(update_fields=["availability", "last_checked_at", "last_error", "armed"])
        CheckResult.objects.create(product=product, status=status, price=product.price, error=message)
        if snapshot:
            product.title = snapshot.title
            product.image_url = snapshot.image_url
            product.price = snapshot.price
            product.last_checked_at = now
            product.last_success_at = now
            product.save(update_fields=["title", "image_url", "price", "last_checked_at", "last_success_at", "updated_at"])
