"""Post one fixed A2UI demo surface into a tenant's app chat (operator tool)."""

from __future__ import annotations

import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.router.surfaces.demo import SCENARIOS, post_demo_surface
from apps.router.surfaces.validate import SurfaceError


class Command(BaseCommand):
    help = "Post a fixed A2UI demo surface as a proactive assistant message."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--tenant", required=True, type=UUID, help="Tenant UUID.")
        parser.add_argument(
            "--scenario",
            required=True,
            choices=SCENARIOS,
            help="Which demo surface to post.",
        )
        parser.add_argument("--mission", type=UUID, default=None, help="Mission UUID (required for --scenario stuck).")

    def handle(self, *args, **options) -> None:
        try:
            result = post_demo_surface(
                str(options["tenant"]),
                options["scenario"],
                str(options["mission"]) if options["mission"] else None,
            )
        except (SurfaceError, ValueError) as exc:
            # Operator tool: a bad scenario/mission is a one-line error, not a
            # traceback. SurfaceError is a ValueError, but list it explicitly so
            # this remains correct if that hierarchy ever changes.
            raise CommandError(f"post_demo_surface failed: {exc}") from None
        self.stdout.write(json.dumps(result, default=str))
