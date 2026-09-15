from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_retailer_request_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationdelivery",
            name="attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="next_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="price",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="title",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="url",
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name="notificationdelivery",
            name="webhook",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deliveries", to="core.discordwebhook"),
        ),
        migrations.AddIndex(
            model_name="notificationdelivery",
            index=models.Index(fields=["delivered", "next_attempt_at"], name="core_deliv_deliver_34b586_idx"),
        ),
    ]
