from datetime import timedelta
import logging
import time

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import F, Min, Q
from django.utils import timezone

from .models import CheckResult, Monitor, NotificationDelivery, Product, RetailerRequestLog, SystemState
from .services import AdapterError, get_adapter_for_retailer, post_discord


logger = logging.getLogger(__name__)
RETRY_INTERVAL_SECONDS = 300
TRANSIENT_RETRY_MULTIPLIER = 2
BLOCKED_RETRY_MULTIPLIER = 5
DELIVERY_RETRY_SECONDS = (60, 120, 300)
DELIVERY_TIMEOUT_SECONDS = 10


def _retry_locked_database(operation, description):
    for attempt in range(3):
        try:
            return operation()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 2:
                raise
            logger.warning("Database locked while %s; retrying", description)
            time.sleep(0.2 * (attempt + 1))


def _next_check_at(monitor, now, succeeded, status):
    if succeeded:
        delay = monitor.check_interval_seconds
    else:
        multiplier = BLOCKED_RETRY_MULTIPLIER if status == Product.Availability.BLOCKED else TRANSIENT_RETRY_MULTIPLIER
        delay = max(monitor.check_interval_seconds, min(monitor.check_interval_seconds * multiplier, RETRY_INTERVAL_SECONDS))
    return now + timedelta(seconds=delay)


def _delivery_retry_at(now, attempts):
    return now + timedelta(seconds=DELIVERY_RETRY_SECONDS[min(attempts - 1, len(DELIVERY_RETRY_SECONDS) - 1)])


def deliver_notification(delivery_id):
    now = timezone.now()
    with transaction.atomic():
        delivery = NotificationDelivery.objects.select_for_update().select_related("monitor__webhook", "webhook").filter(id=delivery_id).first()
        if not delivery or delivery.delivered or delivery.completed_at or (delivery.next_attempt_at and delivery.next_attempt_at > now):
            return
        webhook = delivery.webhook or delivery.monitor.webhook
        delivery.attempts += 1
        if not webhook.enabled:
            delivery.error = "WebhookDisabled"
            delivery.completed_at = now
            delivery.next_attempt_at = None
            delivery.save(update_fields=["attempts", "error", "completed_at", "next_attempt_at"])
            return
        delivery.next_attempt_at = _delivery_retry_at(now, delivery.attempts)
        delivery.save(update_fields=["attempts", "next_attempt_at"])
    try:
        delivered, error = post_discord(webhook, delivery.title, delivery.url, delivery.price)
    except Exception as exc:
        logger.exception("Unhandled Discord delivery failure for notification %s", delivery_id)
        delivered, error = False, exc.__class__.__name__
    with transaction.atomic():
        delivery = NotificationDelivery.objects.select_for_update().get(id=delivery_id)
        delivery.delivered = delivered
        delivery.error = error
        if delivered:
            delivery.completed_at = timezone.now()
            delivery.next_attempt_at = None
        else:
            delivery.next_attempt_at = _delivery_retry_at(timezone.now(), delivery.attempts)
        delivery.save(update_fields=["delivered", "error", "completed_at", "next_attempt_at"])


def process_pending_deliveries(deadline=None):
    now = timezone.now()
    delivery_ids = NotificationDelivery.objects.filter(delivered=False, completed_at__isnull=True, next_attempt_at__lte=now).order_by("next_attempt_at", "id").values_list("id", flat=True)[:100]
    for delivery_id in delivery_ids:
        if deadline and time.monotonic() + DELIVERY_TIMEOUT_SECONDS > deadline:
            break
        try:
            deliver_notification(delivery_id)
        except Exception:
            logger.exception("Unhandled pending Discord delivery failure for notification %s", delivery_id)


def check_due_products():
    now = timezone.now()
    deadline = time.monotonic() + settings.SCHEDULER_RUN_BUDGET_SECONDS
    _retry_locked_database(
        lambda: SystemState.objects.update_or_create(
            pk=1,
            defaults={"scheduler_heartbeat": now, "scheduler_started_at": now},
        ),
        "recording scheduler start",
    )
    due_product_ids = Monitor.objects.filter(active=True).filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now)).values("product_id").annotate(due_at=Min("next_check_at")).order_by(F("due_at").asc(nulls_first=True), "product_id").values_list("product_id", flat=True)[:100]
    for product_id in due_product_ids:
        if time.monotonic() >= deadline:
            logger.warning("Scheduler pass reached its %s-second budget; remaining due products will run next pass", settings.SCHEDULER_RUN_BUDGET_SECONDS)
            break
        try:
            check_product(product_id)
        except Exception:
            # One corrupted product or unexpected database failure must not stop the batch.
            logger.exception("Unhandled monitor check failure for product %s", product_id)
    for operation, description in (
        (lambda: CheckResult.objects.filter(checked_at__lt=now - timedelta(days=30)).delete(), "deleting expired check results"),
        (lambda: RetailerRequestLog.objects.filter(created_at__lt=now - timedelta(days=30)).delete(), "deleting expired retailer request logs"),
    ):
        try:
            _retry_locked_database(operation, description)
        except Exception:
            logger.exception("Unable to complete scheduler maintenance while %s", description)
    _retry_locked_database(
        lambda: SystemState.objects.filter(pk=1).update(scheduler_heartbeat=timezone.now(), scheduler_completed_at=timezone.now()),
        "recording scheduler completion",
    )
    try:
        process_pending_deliveries(deadline)
    except Exception:
        logger.exception("Unable to process pending Discord deliveries")


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
    delivery_ids = []
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
            monitor.next_check_at = _next_check_at(monitor, now, success, status)
            if status == Product.Availability.AVAILABLE:
                monitor.last_available_at = now
            if status == Product.Availability.UNAVAILABLE:
                monitor.armed = True
            elif status == Product.Availability.AVAILABLE and previous_monitor != Product.Availability.AVAILABLE and monitor.armed and monitor.webhook.enabled:
                monitor.armed = False
                delivery = NotificationDelivery.objects.create(monitor=monitor, webhook=monitor.webhook, title=product.title, url=product.canonical_url, price=product.price, next_attempt_at=now)
                delivery_ids.append(delivery.id)
            monitor.save(update_fields=["availability", "last_checked_at", "last_available_at", "last_error", "next_check_at", "armed"])
        product.next_check_at = product.monitors.filter(active=True).order_by(F("next_check_at").asc(nulls_last=True)).values_list("next_check_at", flat=True).first()
        product.save(update_fields=["next_check_at", "updated_at"])
    for delivery_id in delivery_ids:
        deliver_notification(delivery_id)


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
    delivery_id = None
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().select_related("webhook").get(id=monitor.id)
        previous = monitor.availability
        monitor.availability = status
        monitor.last_checked_at = now
        monitor.last_error = message
        monitor.next_check_at = _next_check_at(monitor, now, snapshot is not None, status)
        if status == Product.Availability.AVAILABLE:
            monitor.last_available_at = now
        if status == Product.Availability.UNAVAILABLE:
            monitor.armed = True
        elif status == Product.Availability.AVAILABLE and previous != Product.Availability.AVAILABLE and monitor.armed and monitor.webhook.enabled:
            monitor.armed = False
            delivery = NotificationDelivery.objects.create(monitor=monitor, webhook=monitor.webhook, title=product.title, url=product.canonical_url, price=product.price, next_attempt_at=now)
            delivery_id = delivery.id
        monitor.save(update_fields=["availability", "last_checked_at", "last_available_at", "last_error", "next_check_at", "armed"])
        CheckResult.objects.create(product=product, status=status, price=product.price, error=message)
        if snapshot:
            product.title = snapshot.title
            product.image_url = snapshot.image_url
            product.price = snapshot.price
            product.last_checked_at = now
            product.last_success_at = now
            product.save(update_fields=["title", "image_url", "price", "last_checked_at", "last_success_at", "updated_at"])
    if delivery_id:
        deliver_notification(delivery_id)


def _refresh_product_next_check(product):
    product.next_check_at = product.monitors.filter(active=True).order_by(F("next_check_at").asc(nulls_last=True)).values_list("next_check_at", flat=True).first()
    product.save(update_fields=["next_check_at", "updated_at"])
