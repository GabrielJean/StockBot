from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0002_alter_user_options_alter_user_managers_and_more")]

    operations = [
        migrations.AddField(model_name="monitor", name="postal_code", field=models.CharField(blank=True, max_length=7)),
        migrations.AddField(model_name="validation", name="postal_code", field=models.CharField(blank=True, max_length=7)),
    ]
