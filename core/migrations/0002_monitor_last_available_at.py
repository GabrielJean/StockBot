from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="monitor",
            name="last_available_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
