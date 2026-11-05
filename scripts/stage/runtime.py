"""Commands inside stage Django; all product work stays in production code."""

import json
import os
import sys
from pathlib import Path

import django
from django.core.serializers.json import DjangoJSONEncoder


def main():
    django.setup()
    from apps.tenants.models import Tenant

    action = sys.argv[1]
    if action in ("serve", "seed"):
        from django.core.management import call_command
        from seed import seed

        call_command("migrate", interactive=False, verbosity=0)
        result = seed()
        if action == "seed":
            print(json.dumps(result, default=str))
            return
        root = Path("/home/node/.openclaw")
        # Django and the image's node user must own the same atomic share writes.
        for path in [root, *root.rglob("*")]:
            os.chown(path, 1000, 1000)
        Path("/stage-state/seed.json").write_text(json.dumps(result, default=str))
        os.setgid(1000)
        os.setuid(1000)
        os.execvp(
            "gunicorn",
            [
                "gunicorn",
                "config.wsgi:application",
                "--bind",
                "0.0.0.0:8000",
                "--workers",
                "1",
                "--threads",
                "8",
                "--timeout",
                "360",
                "--no-control-socket",
                "--access-logfile",
                "-",
            ],
        )
    tenant = Tenant.objects.select_related("user").get(id=os.environ["NBHD_TENANT_ID"], is_synthetic=True)
    if action == "token":
        from rest_framework_simplejwt.tokens import AccessToken

        token = AccessToken.for_user(tenant.user)
        changed = tenant.user.password_last_changed_at
        token["pw_iat"] = int(changed.timestamp()) if changed else 0
        print(str(token))
    elif action == "sql":
        from apps.billing.models import UsageRecord
        from apps.datebook.models import MirrorEvent
        from apps.journal.models import DailyNote, Document, Goal, Task, UserMemory
        from apps.router.models import AppChatMessage, PendingMessage, ProactiveOutbound

        scope = {
            "tenant": tenant,
            "UsageRecord": UsageRecord,
            "DailyNote": DailyNote,
            "Document": Document,
            "MirrorEvent": MirrorEvent,
            "Goal": Goal,
            "Task": Task,
            "UserMemory": UserMemory,
            "AppChatMessage": AppChatMessage,
            "PendingMessage": PendingMessage,
            "ProactiveOutbound": ProactiveOutbound,
        }
        result = eval(sys.argv[2], scope)  # noqa: S307 - explicit local operator helper
        output = json.dumps(result, cls=DjangoJSONEncoder, default=str)
        for name, value in os.environ.items():
            if any(word in name for word in ("KEY", "TOKEN", "SECRET", "SEED")) and len(value) >= 8:
                output = output.replace(value, "[secret omitted]")
        print(output)
    elif action == "outbound":
        from apps.router.models import ProactiveOutbound

        # .values() deliberately includes every column, not a curated summary.
        rows = list(ProactiveOutbound.objects.filter(tenant=tenant, created_at__gte=sys.argv[2]).values())
        for row in rows:
            for key, value in row.items():
                if isinstance(value, (bytes, memoryview)):
                    row[key] = bytes(value).hex()
        print(json.dumps(rows, cls=DjangoJSONEncoder))


if __name__ == "__main__":
    main()
