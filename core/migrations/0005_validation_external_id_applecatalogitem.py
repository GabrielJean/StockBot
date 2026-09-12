from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0004_monitor_fulfillment_monitor_last_checked_at_and_more")]
    operations = [
        migrations.AddField(model_name="validation", name="external_id", field=models.CharField(blank=True, max_length=128)),
        migrations.CreateModel(name="AppleCatalogItem", fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("order_number", models.CharField(max_length=32, unique=True)), ("title", models.CharField(max_length=255)), ("configuration", models.CharField(blank=True, max_length=255)), ("source_url", models.URLField()), ("imported_at", models.DateTimeField(auto_now=True)), ("active", models.BooleanField(default=True))]),
    ]
