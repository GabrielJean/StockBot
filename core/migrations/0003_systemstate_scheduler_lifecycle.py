from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_monitor_last_available_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemstate",
            name="scheduler_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="systemstate",
            name="scheduler_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
