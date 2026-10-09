"""QStash maintenance tasks for diagnostic tool-event telemetry."""

from django.core.management import call_command


def purge_tool_events_task() -> None:
    """Purge up to 100k expired rows daily without consuming the 300s worker budget.

    Twenty 5k-row batches cap work per firing. Stop starting batches after
    30s; the command caps each PostgreSQL statement/lock wait at 5s within
    a batch transaction. Successful batches commit independently. Database
    failures propagate for QStash retry; leftovers wait for the next firing.
    """
    call_command("purge_tool_events", older_than_days=90, batch_size=5000, max_batches=20, max_seconds=30)
