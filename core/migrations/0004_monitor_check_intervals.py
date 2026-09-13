from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_systemstate_scheduler_lifecycle"),
    ]

    operations = [
        migrations.AddField(
            model_name="monitor",
            name="check_interval_seconds",
            field=models.PositiveIntegerField(default=60),
        ),
        migrations.AddField(
            model_name="monitor",
            name="next_check_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="validation",
            name="check_interval_seconds",
            field=models.PositiveIntegerField(default=60),
        ),
    ]
