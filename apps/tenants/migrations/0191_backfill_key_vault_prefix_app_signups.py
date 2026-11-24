"""Backfill key_vault_prefix for app sign-ups.

``prepare_tenant_provisioning`` (the iOS/web sign-up path) never set the
prefix, so every app-created tenant since 0020 has an empty one. That made
``secret_name_for_tenant`` raise during provisioning and silently put the
tenant on the shared OpenRouter key; integration / BYO / recall secret
names are built from the same prefix. Same naming rule as 0020.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    for tenant in Tenant.objects.filter(key_vault_prefix="").iterator():
        tenant.key_vault_prefix = f"tenants-{tenant.user_id}"
        tenant.save(update_fields=["key_vault_prefix"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0190_relock_after_keep_chatting"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
