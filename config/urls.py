from django.contrib import admin
from django.urls import path, re_path
from core import views

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/v1/", views.api_root),
    path("api/v1/<str:resource>/", views.api_collection),
    path("api/v1/<str:resource>/<int:object_id>/", views.api_item),
    path("healthz/", views.health),
    re_path(r"^(?!api/|django-admin/|healthz/|static/).*$", views.spa),
]
