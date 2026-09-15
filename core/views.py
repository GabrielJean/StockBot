import json
import re
from datetime import timedelta

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import DiscordWebhook, Monitor, Product, RetailerRequestLog, SystemState, User, Validation
from .services import AdapterError, encrypt, get_adapter_for_url, masked_url, post_discord


CHECK_INTERVALS = {30, 60, 300, 900, 1800, 3600, 86400}
FAST_CHECK_INTERVALS = {1, 15}
IMPERSONATOR_SESSION_KEY = "stockbot_impersonator_id"


def payload(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def result(data=None, status=200):
    return JsonResponse(data or {}, status=status)


def error(message, status=400):
    return result({"error": message}, status)


def require_user(request):
    return request.user.is_authenticated and request.user.is_active and request.user.status == User.Status.APPROVED


def valid_check_interval(user, interval):
    return (
        not isinstance(interval, bool)
        and interval in CHECK_INTERVALS | (FAST_CHECK_INTERVALS if user.fast_check_intervals_allowed else set())
    )


def serialize_webhook(item):
    from .services import decrypt
    return {"id": item.id, "name": item.name, "url": masked_url(decrypt(item.encrypted_url)), "enabled": item.enabled, "lastDeliveryError": item.last_delivery_error, "lastTestedAt": item.last_tested_at}


def serialize_monitor(item):
    product = item.product
    status = item.availability if item.availability != "unknown" else product.availability
    return {"id": item.id, "active": item.active, "webhookId": item.webhook_id, "webhookName": item.webhook.name, "fulfillment": item.fulfillment, "locationKeys": item.location_keys, "checkIntervalSeconds": item.check_interval_seconds, "lastAvailableAt": item.last_available_at, "product": {"id": product.id, "retailer": product.retailer, "title": product.title, "url": product.canonical_url, "imageUrl": product.image_url, "price": product.price, "availability": status, "lastCheckedAt": item.last_checked_at or product.last_checked_at, "lastError": item.last_error or product.last_error}}


def serialize_staff_monitor(item):
    data = serialize_monitor(item)
    data["owner"] = {"id": item.owner_id, "email": item.owner.email, "displayName": item.owner.display_name}
    return data


def serialize_request_log(item):
    return {"id": item.id, "retailer": item.retailer, "endpoint": item.endpoint, "httpStatus": item.http_status, "error": item.error, "createdAt": item.created_at}


@require_http_methods(["GET"])
def health(request):
    state = SystemState.objects.first()
    completed_at = state.scheduler_completed_at if state else None
    scheduler_healthy = not settings.SCHEDULER_ENABLED or (
        completed_at is not None
        and completed_at >= timezone.now() - timedelta(seconds=settings.SCHEDULER_STALE_SECONDS)
    )
    return result(
        {
            "ok": scheduler_healthy,
            "schedulerEnabled": settings.SCHEDULER_ENABLED,
            "schedulerHealthy": scheduler_healthy,
            "schedulerHeartbeat": state.scheduler_heartbeat if state else None,
            "schedulerCompletedAt": completed_at,
        },
        200 if scheduler_healthy else 503,
    )


def spa(request):
    return render(request, "index.html")


@require_http_methods(["GET"])
def api_root(request):
    return result({"csrfToken": get_token(request), "user": current_user(request), "appVersion": settings.APP_VERSION})


def current_user(request):
    if not request.user.is_authenticated:
        return None
    data = {"id": request.user.id, "email": request.user.email, "displayName": request.user.display_name, "staff": request.user.is_staff, "status": request.user.status, "fastCheckIntervalsAllowed": request.user.fast_check_intervals_allowed}
    impersonator_id = request.session.get(IMPERSONATOR_SESSION_KEY)
    if impersonator_id:
        impersonator = User.objects.filter(id=impersonator_id, is_staff=True, is_active=True).first()
        if impersonator:
            data["impersonating"] = {"id": impersonator.id, "email": impersonator.email, "displayName": impersonator.display_name}
        else:
            request.session.pop(IMPERSONATOR_SESSION_KEY, None)
    return data


@require_http_methods(["GET", "POST"])
def api_collection(request, resource):
    data = payload(request)
    if data is None:
        return error("Request body must be a JSON object.")
    if resource == "register" and request.method == "POST":
        email_value = data.get("email", "")
        password = data.get("password", "")
        if not isinstance(email_value, str) or not isinstance(password, str):
            return error("Email and password must be strings.")
        email = email_value.strip().lower()
        if not email or not password:
            return error("Email and password are required.")
        try:
            validate_password(password)
        except Exception as exc:
            return error(" ".join(exc.messages))
        try:
            with transaction.atomic():
                state, _ = SystemState.objects.select_for_update().get_or_create(pk=1)
                first = not state.initial_admin_claimed
                user = User(email=email, display_name=data.get("displayName", "").strip()[:80], status=User.Status.APPROVED if first else User.Status.PENDING, is_active=first, is_staff=first, is_superuser=first, approved_at=timezone.now() if first else None)
                user.set_password(password)
                user.save()
                if first:
                    state.initial_admin_claimed = True
                    state.save(update_fields=["initial_admin_claimed"])
        except IntegrityError:
            return error("An account with that email already exists.", 409)
        return result({"created": True, "approved": first, "message": "Your administrator account is ready." if first else "Your account is pending administrator approval."}, 201)
    if resource == "login" and request.method == "POST":
        email = data.get("email", "")
        password = data.get("password", "")
        if not isinstance(email, str) or not isinstance(password, str):
            return error("Email and password must be strings.")
        user = authenticate(request, email=email.strip().lower(), password=password)
        if not user:
            pending = User.objects.filter(email=email.strip().lower(), status=User.Status.PENDING).exists()
            return error("Your account is awaiting administrator approval." if pending else "Invalid email or password.", 403)
        login(request, user)
        return result({"user": current_user(request), "csrfToken": get_token(request)})
    if resource == "logout" and request.method == "POST":
        logout(request)
        return result({"ok": True})
    if resource == "session" and request.method == "GET":
        return result({"user": current_user(request), "csrfToken": get_token(request)})
    if not require_user(request):
        return error("Authentication required.", 401)
    if resource == "impersonation" and request.method == "POST":
        action = data.get("action")
        if action == "stop":
            impersonator_id = request.session.get(IMPERSONATOR_SESSION_KEY)
            impersonator = User.objects.filter(id=impersonator_id, is_staff=True, is_active=True).first()
            if not impersonator:
                return error("No active impersonation session exists.", 400)
            login(request, impersonator)
            request.session.pop(IMPERSONATOR_SESSION_KEY, None)
            return result({"user": current_user(request), "csrfToken": get_token(request)})
        if action == "start":
            if IMPERSONATOR_SESSION_KEY in request.session or not request.user.is_staff:
                return error("Staff access required.", 403)
            target = User.objects.filter(id=data.get("userId"), is_active=True, status=User.Status.APPROVED).first()
            if not target:
                return error("Choose an approved user to impersonate.", 422)
            if target.id == request.user.id:
                return error("You are already signed in as this user.")
            impersonator_id = request.user.id
            login(request, target)
            request.session[IMPERSONATOR_SESSION_KEY] = impersonator_id
            return result({"user": current_user(request), "csrfToken": get_token(request)})
        return error("Choose a valid impersonation action.")
    if resource == "stores" and request.method == "GET":
        return result({"stores": [{"key": "nintendo_ca", "name": "Nintendo Canada", "enabled": True, "description": "Shipping availability"}, {"key": "bestbuy_ca", "name": "Best Buy Canada", "enabled": True, "description": "Shipping and selected-store pickup"}, {"key": "apple_ca", "name": "Apple Canada", "enabled": True, "description": "Shipping and selected Apple Store pickup"}, {"key": "walmart_ca", "name": "Walmart Canada", "enabled": False}, {"key": "amazon_ca", "name": "Amazon Canada", "enabled": False}]})
    if resource in {"bestbuy-stores", "apple-stores"} and request.method == "POST":
        postal_value = data.get("postalCode", "")
        if not isinstance(postal_value, str):
            return error("Enter a valid Canadian postal code.")
        postal_code = re.sub(r"\s+", "", postal_value.upper())
        if not re.fullmatch(r"[ABCEGHJKLMNPRSTVXY]\d[ABCEGHJKLMNPRSTVWXYZ]\d[ABCEGHJKLMNPRSTVWXYZ]\d", postal_code):
            return error("Enter a valid Canadian postal code.")
        try:
            if resource == "bestbuy-stores":
                source = get_adapter_for_url("https://www.bestbuy.ca/en-ca/product/example/1")
                stores = source.nearby_stores(postal_code)
            else:
                part_value = data.get("applePartNumber", "")
                if not isinstance(part_value, str):
                    return error("Enter the Canada Apple Order No. before finding stores.")
                part = part_value.strip().upper()
                if not part:
                    return error("Enter the Canada Apple Order No. before finding stores.")
                source = get_adapter_for_url("https://www.apple.com/ca/shop/")
                stores = source.nearby_stores(postal_code, part)
            return result({"stores": stores})
        except AdapterError as exc:
            return error(str(exc), 422)
    if resource == "webhooks":
        if request.method == "GET":
            return result({"webhooks": [serialize_webhook(item) for item in request.user.webhooks.all()]})
        if request.method == "POST":
            url_value = data.get("url", "")
            if not isinstance(url_value, str):
                return error("Enter a valid Discord webhook URL.")
            url = url_value.strip()
            if not url.startswith("https://discord.com/api/webhooks/") and not url.startswith("https://discordapp.com/api/webhooks/"):
                return error("Enter a valid Discord webhook URL.")
            try:
                item = DiscordWebhook.objects.create(owner=request.user, name=data.get("name", "Discord")[:80], encrypted_url=encrypt(url))
            except IntegrityError:
                return error("Webhook names must be unique.")
            return result({"webhook": serialize_webhook(item)}, 201)
    if resource == "monitors" and request.method == "GET":
        return result({"monitors": [serialize_monitor(item) for item in request.user.monitors.select_related("product", "webhook").order_by("-created_at")]})
    if resource == "validate" and request.method == "POST":
        raw_url = data.get("url", "")
        postal_value = data.get("postalCode", "")
        external_id_value = data.get("applePartNumber", "")
        location_keys = data.get("locationKeys", [])
        if not isinstance(raw_url, str) or not isinstance(postal_value, str) or not isinstance(external_id_value, str):
            return error("Product details must use text values.")
        if not isinstance(location_keys, list) or len(location_keys) > 25 or any(not isinstance(key, str) or not key.strip() for key in location_keys):
            return error("Choose up to 25 valid store locations.")
        source = get_adapter_for_url(raw_url)
        postal_code = re.sub(r"\s+", "", postal_value.upper())
        if postal_code and not re.fullmatch(r"[ABCEGHJKLMNPRSTVXY]\d[ABCEGHJKLMNPRSTVWXYZ]\d[ABCEGHJKLMNPRSTVWXYZ]\d", postal_code):
            return error("Enter a valid Canadian postal code.")
        if not source:
            return error("This retailer URL is not supported yet.", 422)
        fulfillment = data.get("fulfillment", "shipping")
        check_interval_seconds = data.get("checkIntervalSeconds", 60)
        if not valid_check_interval(request.user, check_interval_seconds):
            return error("Choose a valid check interval.")
        location_keys = [key.strip()[:64] for key in location_keys]
        external_id = external_id_value.strip().upper()
        if fulfillment not in {"shipping", "pickup", "either"}:
            return error("Choose shipping, pickup, or either fulfillment.")
        if source.key not in {"bestbuy_ca", "apple_ca"}:
            fulfillment, location_keys = "shipping", []
        if source.key in {"bestbuy_ca", "apple_ca"} and fulfillment in {"pickup", "either"} and (not postal_code or not location_keys):
            return error("Pickup monitoring requires a postal code and at least one store.")
        if source.key == "apple_ca" and not external_id:
            return error("Enter the Canada Apple Order No. for this exact configuration.")
        if source.key == "apple_ca" and fulfillment in {"pickup", "either"} and not postal_code:
            return error("Apple fulfillment requires a Canadian postal code.")
        try:
            snapshot = source.validate(raw_url, postal_code, fulfillment, location_keys, external_id)
        except AdapterError as exc:
            return error(str(exc), 422)
        validation = Validation.objects.create(owner=request.user, canonical_url=snapshot.canonical_url, title=snapshot.title, image_url=snapshot.image_url, price=snapshot.price, availability=snapshot.availability, postal_code=postal_code, fulfillment=fulfillment, location_keys=location_keys, check_interval_seconds=check_interval_seconds, external_id=snapshot.external_id, expires_at=timezone.now() + timedelta(minutes=15))
        return result({"validation": {"id": validation.id, "title": validation.title, "url": validation.canonical_url, "imageUrl": validation.image_url, "price": validation.price, "availability": validation.availability}})
    if resource == "confirm-monitor" and request.method == "POST":
        with transaction.atomic():
            validation = Validation.objects.select_for_update().filter(id=data.get("validationId"), owner=request.user, expires_at__gt=timezone.now()).first()
            webhook = request.user.webhooks.select_for_update().filter(id=data.get("webhookId"), enabled=True).first()
            if not validation or not webhook:
                return error("Choose a current validation and enabled webhook.")
            source = get_adapter_for_url(validation.canonical_url)
            if not source:
                return error("This monitor validation has an unsupported product URL.", 422)
            product, _ = Product.objects.update_or_create(canonical_url=validation.canonical_url, defaults={"retailer": source.key, "external_id": validation.external_id, "title": validation.title, "image_url": validation.image_url, "price": validation.price, "availability": validation.availability})
            monitor, created = Monitor.objects.get_or_create(owner=request.user, product=product, defaults={"webhook": webhook, "postal_code": validation.postal_code, "fulfillment": validation.fulfillment, "location_keys": validation.location_keys, "check_interval_seconds": validation.check_interval_seconds})
            if not created:
                monitor.webhook = webhook
                monitor.active = True
                monitor.postal_code = validation.postal_code
                monitor.fulfillment = validation.fulfillment
                monitor.location_keys = validation.location_keys
                monitor.check_interval_seconds = validation.check_interval_seconds
                monitor.next_check_at = None
                monitor.save(update_fields=["webhook", "active", "postal_code", "fulfillment", "location_keys", "check_interval_seconds", "next_check_at", "updated_at"])
            validation.delete()
        return result({"monitor": serialize_monitor(monitor)}, 201)
    if resource == "staff-users" and request.method == "GET":
        if not request.user.is_staff:
            return error("Staff access required.", 403)
        return result({"users": [{"id": user.id, "email": user.email, "displayName": user.display_name, "status": user.status, "staff": user.is_staff, "fastCheckIntervalsAllowed": user.fast_check_intervals_allowed, "joined": user.date_joined} for user in User.objects.order_by("status", "date_joined")]})
    if resource == "staff-monitors" and request.method == "GET":
        if not request.user.is_staff:
            return error("Staff access required.", 403)
        monitors = Monitor.objects.select_related("owner", "product", "webhook").order_by("owner__email", "-created_at")
        return result({"monitors": [serialize_staff_monitor(item) for item in monitors]})
    if resource == "staff-request-logs" and request.method == "GET":
        if not request.user.is_staff:
            return error("Staff access required.", 403)
        logs = RetailerRequestLog.objects.all()
        retailer = request.GET.get("retailer", "")
        if retailer:
            logs = logs.filter(retailer=retailer)
        return result({"logs": [serialize_request_log(item) for item in logs.order_by("-created_at")[:100]]})
    return error("Resource not found.", 404)


@require_http_methods(["PATCH", "DELETE", "POST"])
def api_item(request, resource, object_id):
    if not require_user(request):
        return error("Authentication required.", 401)
    data = payload(request)
    if data is None:
        return error("Request body must be a JSON object.")
    if resource == "monitors":
        item = request.user.monitors.filter(id=object_id).first()
        if not item:
            return error("Monitor not found.", 404)
        if request.method == "DELETE":
            item.delete()
            return result({"ok": True})
        if "active" in data:
            item.active = bool(data["active"])
        if "checkIntervalSeconds" in data:
            interval = data["checkIntervalSeconds"]
            if not valid_check_interval(request.user, interval):
                return error("Choose a valid check interval.")
            item.check_interval_seconds = interval
            item.next_check_at = None
        if "active" in data or "checkIntervalSeconds" in data:
            item.save(update_fields=["active", "check_interval_seconds", "next_check_at", "updated_at"])
        return result({"monitor": serialize_monitor(item)})
    if resource == "staff-monitors":
        if not request.user.is_staff:
            return error("Staff access required.", 403)
        item = Monitor.objects.select_related("owner", "product", "webhook").filter(id=object_id).first()
        if not item:
            return error("Monitor not found.", 404)
        if request.method == "DELETE":
            item.delete()
            return result({"ok": True})
        if "active" in data:
            item.active = bool(data["active"])
            item.save(update_fields=["active", "updated_at"])
        return result({"monitor": serialize_staff_monitor(item)})
    if resource == "webhooks":
        item = request.user.webhooks.filter(id=object_id).first()
        if not item:
            return error("Webhook not found.", 404)
        if request.method == "DELETE":
            if item.monitors.exists():
                return error("Reassign monitors before deleting this webhook.")
            item.delete()
            return result({"ok": True})
        if request.method == "POST" and data.get("action") == "test":
            delivered, message = post_discord(item, "StockBot webhook test", "https://www.nintendo.com/en-ca/", test=True)
            return result({"delivered": delivered, "error": message}, 200 if delivered else 422)
        item.name = data.get("name", item.name)[:80]
        item.enabled = bool(data.get("enabled", item.enabled))
        item.save()
        return result({"webhook": serialize_webhook(item)})
    if resource == "staff-users" and request.user.is_staff:
        user = User.objects.filter(id=object_id).first()
        action = data.get("action")
        if not user:
            return error("User not found.", 404)
        if action == "approve":
            user.status, user.is_active, user.approved_at = User.Status.APPROVED, True, timezone.now()
        elif action == "reject":
            user.status, user.is_active = User.Status.REJECTED, False
        elif action == "set-fast-check-intervals":
            allowed = data.get("allowed")
            if not isinstance(allowed, bool):
                return error("Choose whether to allow fast check intervals.")
            user.fast_check_intervals_allowed = allowed
            if not allowed:
                Monitor.objects.filter(owner=user, check_interval_seconds__in=FAST_CHECK_INTERVALS).update(
                    check_interval_seconds=30,
                    next_check_at=None,
                    updated_at=timezone.now(),
                )
        else:
            return error("Unsupported action.")
        user.save(update_fields=["status", "is_active", "approved_at", "fast_check_intervals_allowed"])
        return result({"ok": True})
    return error("Resource not found.", 404)
