from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.update({"is_staff": True, "is_superuser": True, "is_active": True, "status": "approved"})
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    username = None
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    approved_at = models.DateTimeField(null=True, blank=True)
    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []


class SystemState(models.Model):
    initial_admin_claimed = models.BooleanField(default=False)
    scheduler_heartbeat = models.DateTimeField(null=True, blank=True)
    scheduler_started_at = models.DateTimeField(null=True, blank=True)
    scheduler_completed_at = models.DateTimeField(null=True, blank=True)


class DiscordWebhook(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="webhooks")
    name = models.CharField(max_length=80)
    encrypted_url = models.TextField()
    enabled = models.BooleanField(default=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_delivery_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("owner", "name")]


class Product(models.Model):
    class Availability(models.TextChoices):
        AVAILABLE = "available", "Available"
        UNAVAILABLE = "unavailable", "Unavailable"
        UNKNOWN = "unknown", "Unknown"
        ERROR = "error", "Error"
        BLOCKED = "blocked", "Blocked"

    retailer = models.CharField(max_length=32, default="nintendo_ca")
    canonical_url = models.URLField(unique=True)
    external_id = models.CharField(max_length=128, blank=True)
    title = models.CharField(max_length=255)
    image_url = models.URLField(blank=True)
    price = models.CharField(max_length=32, blank=True)
    availability = models.CharField(max_length=16, choices=Availability.choices, default=Availability.UNKNOWN)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class Validation(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    canonical_url = models.URLField()
    title = models.CharField(max_length=255)
    image_url = models.URLField(blank=True)
    price = models.CharField(max_length=32, blank=True)
    availability = models.CharField(max_length=16)
    postal_code = models.CharField(max_length=7, blank=True)
    fulfillment = models.CharField(max_length=12, default="shipping")
    location_keys = models.JSONField(default=list)
    check_interval_seconds = models.PositiveIntegerField(default=60)
    external_id = models.CharField(max_length=128, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class Monitor(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="monitors")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="monitors")
    webhook = models.ForeignKey(DiscordWebhook, on_delete=models.PROTECT, related_name="monitors")
    active = models.BooleanField(default=True)
    armed = models.BooleanField(default=True)
    postal_code = models.CharField(max_length=7, blank=True)
    fulfillment = models.CharField(max_length=12, default="shipping")
    location_keys = models.JSONField(default=list)
    check_interval_seconds = models.PositiveIntegerField(default=60)
    next_check_at = models.DateTimeField(null=True, blank=True)
    availability = models.CharField(max_length=16, default="unknown")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_available_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("owner", "product")]


class CheckResult(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="checks")
    status = models.CharField(max_length=16)
    price = models.CharField(max_length=32, blank=True)
    error = models.CharField(max_length=255, blank=True)
    checked_at = models.DateTimeField(auto_now_add=True)


class RetailerRequestLog(models.Model):
    retailer = models.CharField(max_length=32)
    endpoint = models.CharField(max_length=64)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["retailer", "-created_at"], name="core_retail_retailer_5e1_idx")]


class NotificationDelivery(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="deliveries")
    event_type = models.CharField(max_length=32, default="restock")
    delivered = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True)
    attempted_at = models.DateTimeField(auto_now_add=True)
