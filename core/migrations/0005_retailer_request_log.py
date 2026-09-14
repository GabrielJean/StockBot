from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_monitor_check_intervals"),
    ]

    operations = [
        migrations.CreateModel(
            name="RetailerRequestLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("retailer", models.CharField(max_length=32)),
                ("endpoint", models.CharField(max_length=64)),
                ("http_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("error", models.CharField(blank=True, max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "indexes": [models.Index(fields=["retailer", "-created_at"], name="core_retail_retailer_5e1_idx")],
            },
        ),
    ]
