"""Pin the local adapter's promise: flags-off renders match unmodified code."""

import os
import runpy
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import django
from django.test import override_settings

django.setup()


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 1, 9, 0, tzinfo=UTC).astimezone(tz)


class RenderParity(unittest.TestCase):
    def test_flags_off_production_config_and_prompts_are_byte_identical(self):
        from apps.orchestrator import local_test
        from apps.orchestrator.config_generator import config_to_json, generate_openclaw_config
        from apps.orchestrator.personas import render_workspace_files
        from apps.orchestrator.workspace_envelope import render_managed_region
        from apps.tenants.models import Tenant

        tenant = Tenant.objects.select_related("user").get(id=os.environ["NBHD_TENANT_ID"])
        source = Path(local_test.__file__)
        original_adapter = runpy.run_path(str(source))["configure_gateway"]

        def render():
            return (
                config_to_json(generate_openclaw_config(tenant)).encode(),
                {key: value.encode() for key, value in render_workspace_files("neighbor", tenant=tenant).items()},
                render_managed_region(tenant).encode(),
            )

        with patch("apps.orchestrator.workspace_envelope.datetime", FrozenDatetime):
            with override_settings(LOCAL_TEST_ROOT=""), patch.object(local_test, "configure_gateway", original_adapter):
                baseline = render()
            actual = render()
        self.assertEqual(baseline, actual)
        print("PASS: config, all workspace prompts and USER.md are byte-identical with rollout flags off")


if __name__ == "__main__":
    unittest.main()
