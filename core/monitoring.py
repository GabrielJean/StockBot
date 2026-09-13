from datetime import timedelta
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import CheckResult, Monitor, NotificationDelivery, Product, SystemState
from .services import AdapterError, get_adapter_for_retailer, post_discord


logger = logging.getLogger(__name__)


def check_due_products():
    now = timezone.now()
    SystemState.objects.update_or_create(pk=1, defaults={"scheduler_heartbeat": now})
    products = Product.objects.filter(monitors__active=True).distinct().filter(next_check_at__isnull=True) | Product.objects.filter(monitors__active=True, next_check_at__lte=now).distinct()
    for product in products[:100]:
        try:
            check_product(product.id)
        except Exception:
            # One corrupted product or unexpected database failure must not stop the batch.
            logger.exception("Unhandled monitor check failure for product %s", product.id)
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
    except Exception:
        logger.exception("Unhandled retailer check failure for product %s", product_id)
        message = "Retailer availability is currently unavailable."
        status = Product.Availability.ERROR
        snapshot = None
        success = False
    deliveries = []
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
        if status == Product.Availability.AVAILABLE:
            Monitor.objects.filter(id__in=[monitor.id for monitor in monitors]).update(last_available_at=now)
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
            deliveries.append((monitor, monitor.webhook, product.title, product.canonical_url, product.price))
    for monitor, webhook, title, url, price in deliveries:
        delivered, error = post_discord(webhook, title, url, price)
        NotificationDelivery.objects.create(monitor=monitor, delivered=delivered, error=error)


def check_monitor(monitor, product, source, now):
    try:
        snapshot = source.check(product, monitor.postal_code, monitor.fulfillment, monitor.location_keys)
        status, message = snapshot.availability, ""
    except AdapterError as exc:
        status = Product.Availability.BLOCKED if "blocked" in str(exc).lower() else Product.Availability.ERROR
        message = str(exc)
        snapshot = None
    except Exception:
        logger.exception("Unhandled retailer check failure for monitor %s", monitor.id)
        status = Product.Availability.ERROR
        message = "Retailer availability is currently unavailable."
        snapshot = None
    delivery = None
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().select_related("webhook").get(id=monitor.id)
        previous = monitor.availability
        monitor.availability = status
        monitor.last_checked_at = now
        monitor.last_error = message
        if status == Product.Availability.AVAILABLE:
            monitor.last_available_at = now
        if status == Product.Availability.UNAVAILABLE:
            monitor.armed = True
        elif status == Product.Availability.AVAILABLE and previous != Product.Availability.AVAILABLE and monitor.armed and monitor.webhook.enabled:
            monitor.armed = False
            delivery = (monitor, monitor.webhook, product.title, product.canonical_url, product.price)
        monitor.save(update_fields=["availability", "last_checked_at", "last_available_at", "last_error", "armed"])
        CheckResult.objects.create(product=product, status=status, price=product.price, error=message)
        if snapshot:
            product.title = snapshot.title
            product.image_url = snapshot.image_url
            product.price = snapshot.price
            product.last_checked_at = now
            product.last_success_at = now
            product.save(update_fields=["title", "image_url", "price", "last_checked_at", "last_success_at", "updated_at"])
    if delivery:
        monitor, webhook, title, url, price = delivery
        delivered, error = post_discord(webhook, title, url, price)
        NotificationDelivery.objects.create(monitor=monitor, delivered=delivered, error=error)
