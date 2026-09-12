from django.contrib import admin
from .models import CheckResult, DiscordWebhook, Monitor, NotificationDelivery, Product, SystemState, User, Validation

admin.site.register([User, SystemState, DiscordWebhook, Product, Validation, Monitor, CheckResult, NotificationDelivery])
