from datetime import timedelta
import logging
import time

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import CheckResult, Monitor, NotificationDelivery, Product, RetailerRequestLog, SystemState
from .services import AdapterError, get_adapter_for_retailer, post_discord


logger = logging.getLogger(__name__)
RETRY_INTERVAL_SECONDS = 300


def _retry_locked_database(operation, description):
    for attempt in range(3):
        try:
            return operation()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 2:
                raise
            logger.warning("Database locked while %s; retrying", description)
            time.sleep(0.2 * (attempt + 1))


def check_due_products():
    now = timezone.now()
    _retry_locked_database(
        lambda: SystemState.objects.update_or_create(
            pk=1,
            defaults={"scheduler_heartbeat": now, "scheduler_started_at": now},
        ),
        "recording scheduler start",
    )
    products = Product.objects.filter(Q(monitors__active=True) & (Q(monitors__next_check_at__isnull=True) | Q(monitors__next_check_at__lte=now))).distinct().order_by(F("monitors__next_check_at").asc(nulls_first=True), "id")
    for product in products[:100]:
        try:
            check_product(product.id)
        except Exception:
            # One corrupted product or unexpected database failure must not stop the batch.
            logger.exception("Unhandled monitor check failure for product %s", product.id)
    CheckResult.objects.filter(checked_at__lt=now - timedelta(days=30)).delete()
    RetailerRequestLog.objects.filter(created_at__lt=now - timedelta(days=30)).delete()
    SystemState.objects.filter(pk=1).update(scheduler_heartbeat=timezone.now(), scheduler_completed_at=timezone.now())


def check_product(product_id):
    product = Product.objects.filter(id=product_id).first()
    if not product:
        return
    now = timezone.now()
    source = get_adapter_for_retailer(product.retailer)
    due_filter = Q(next_check_at__isnull=True) | Q(next_check_at__lte=now)
    if product.retailer in {"bestbuy_ca", "apple_ca"} and source:
        monitors = list(product.monitors.select_related("webhook").filter(active=True).filter(due_filter))
        for monitor in monitors:
            try:
                check_monitor(monitor, product, source, now)
            except Exception:
                logger.exception("Unhandled monitor check failure for monitor %s", monitor.id)
        _refresh_product_next_check(product)
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
        if success:
            product.title = snapshot.title
            product.image_url = snapshot.image_url
            product.price = snapshot.price
            product.last_success_at = now
        product.save()
        CheckResult.objects.create(product=product, status=status, price=product.price, error=message)
        monitors = list(product.monitors.select_related("webhook").filter(active=True).filter(due_filter))
        for monitor in monitors:
            previous_monitor = monitor.availability
            monitor.availability = status
            monitor.last_checked_at = now
            monitor.last_error = message
            monitor.next_check_at = now + timedelta(seconds=monitor.check_interval_seconds if success else min(monitor.check_interval_seconds * 5, RETRY_INTERVAL_SECONDS))
            if status == Product.Availability.AVAILABLE:
                monitor.last_available_at = now
            if status == Product.Availability.UNAVAILABLE:
                monitor.armed = True
            elif status == Product.Availability.AVAILABLE and previous_monitor != Product.Availability.AVAILABLE and monitor.armed and monitor.webhook.enabled:
                monitor.armed = False
                deliveries.append((monitor, monitor.webhook, product.title, product.canonical_url, product.price))
            monitor.save(update_fields=["availability", "last_checked_at", "last_available_at", "last_error", "next_check_at", "armed"])
        product.next_check_at = product.monitors.filter(active=True).order_by(F("next_check_at").asc(nulls_last=True)).values_list("next_check_at", flat=True).first()
        product.save(update_fields=["next_check_at", "updated_at"])
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
        monitor.next_check_at = now + timedelta(seconds=monitor.check_interval_seconds if snapshot else min(monitor.check_interval_seconds * 5, RETRY_INTERVAL_SECONDS))
        if status == Product.Availability.AVAILABLE:
            monitor.last_available_at = now
        if status == Product.Availability.UNAVAILABLE:
            monitor.armed = True
        elif status == Product.Availability.AVAILABLE and previous != Product.Availability.AVAILABLE and monitor.armed and monitor.webhook.enabled:
            monitor.armed = False
            delivery = (monitor, monitor.webhook, product.title, product.canonical_url, product.price)
        monitor.save(update_fields=["availability", "last_checked_at", "last_available_at", "last_error", "next_check_at", "armed"])
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


def _refresh_product_next_check(product):
    product.next_check_at = product.monitors.filter(active=True).order_by(F("next_check_at").asc(nulls_last=True)).values_list("next_check_at", flat=True).first()
    product.save(update_fields=["next_check_at", "updated_at"])
