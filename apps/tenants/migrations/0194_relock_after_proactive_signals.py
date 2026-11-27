# Re-apply the public-schema RLS lockdown after router 0042 created
# ``proactive_signals`` (docs/agents/workflow.md pre-push item 4).

from importlib import import_module

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("router", "0042_proactive_signals"),
        ("tenants", "0193_proactive_signals"),
    ]

    operations = [
        migrations.RunSQL(
            import_module("apps.tenants.migrations.0184_relock_after_chat_attachment_paths").RELOCK_SQL,
            migrations.RunSQL.noop,
        ),
    ]
