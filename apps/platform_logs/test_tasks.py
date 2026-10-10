"""Offline coverage of the scheduled tool-event retention wrapper."""

from unittest.mock import patch

from django.test import SimpleTestCase


class PurgeToolEventsTaskTests(SimpleTestCase):
    def test_scheduled_purge_uses_retention_and_worker_budgets(self):
        from apps.platform_logs.tasks import purge_tool_events_task

        with patch("apps.platform_logs.tasks.call_command") as purge:
            purge_tool_events_task()

        purge.assert_called_once_with(
            "purge_tool_events", older_than_days=90, batch_size=5000, max_batches=20, max_seconds=30
        )

    def test_purge_failure_propagates_for_qstash_retry(self):
        from apps.platform_logs.tasks import purge_tool_events_task

        with (
            patch("apps.platform_logs.tasks.call_command", side_effect=RuntimeError("offline failure")),
            self.assertRaisesMessage(RuntimeError, "offline failure"),
        ):
            purge_tool_events_task()
