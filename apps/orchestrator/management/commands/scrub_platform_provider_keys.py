"""Remove legacy platform provider-key bindings from tenant containers.

Existing tenants' per-secret OpenAI/Anthropic Key Vault role assignments
should be revoked in the same ops pass; that revocation is an Azure ops step.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.azure_client import (
    force_new_container_revision,
    get_container_client,
    is_mock,
    scrub_platform_provider_bindings,
)
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Scrub platform OpenAI/Anthropic secret and env bindings from tenant containers"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect and count changes without creating Container App revisions",
        )
        parser.add_argument(
            "--tenant",
            type=uuid.UUID,
            help="Limit the scrub to one tenant UUID for a canary run",
        )

    def handle(self, *args, **options):
        if is_mock():
            raise CommandError(
                "Azure client is mocked (AZURE_MOCK=true); run from an environment "
                "with real read access, including for --dry-run inspection"
            )

        tenants = Tenant.objects.filter(container_id__gt="").order_by("id")
        if options.get("tenant"):
            tenants = tenants.filter(id=options["tenant"])

        client = get_container_client()
        if client is None:
            raise CommandError("Azure Container Apps client is unavailable")

        dry_run = options.get("dry_run", False)
        scrubbed = 0
        unchanged = 0
        failed = 0

        for tenant in tenants.iterator():
            try:
                app = client.container_apps.get(
                    settings.AZURE_RESOURCE_GROUP,
                    tenant.container_id,
                )
                if not scrub_platform_provider_bindings(app):
                    unchanged += 1
                    continue

                if not dry_run:
                    force_new_container_revision(app, "provider-scrub")
                    client.container_apps.begin_create_or_update(
                        settings.AZURE_RESOURCE_GROUP,
                        tenant.container_id,
                        app,
                    ).result()
                scrubbed += 1
            except Exception:
                failed += 1
                logger.exception(
                    "Provider-key scrub failed for tenant=%s container=%s",
                    tenant.id,
                    tenant.container_id,
                )

        self.stdout.write(f"scrubbed={scrubbed} unchanged={unchanged} failed={failed}")
