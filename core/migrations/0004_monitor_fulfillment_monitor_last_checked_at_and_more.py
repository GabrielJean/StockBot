from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0003_monitor_postal_code_validation_postal_code")]

    operations = [
        migrations.AddField(model_name="monitor", name="availability", field=models.CharField(default="unknown", max_length=16)),
        migrations.AddField(model_name="monitor", name="fulfillment", field=models.CharField(default="shipping", max_length=12)),
        migrations.AddField(model_name="monitor", name="last_checked_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="monitor", name="last_error", field=models.CharField(blank=True, max_length=255)),
        migrations.AddField(model_name="monitor", name="location_keys", field=models.JSONField(default=list)),
        migrations.AddField(model_name="validation", name="fulfillment", field=models.CharField(default="shipping", max_length=12)),
        migrations.AddField(model_name="validation", name="location_keys", field=models.JSONField(default=list)),
    ]
