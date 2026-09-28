"""Private journal Task → shared mission/step completion linkage.

A pre_save receiver remembers the persisted owner-scoped status. The post_save
receiver mirrors only an actual transition into done, with exact accepted-owner
Task linkage for steps. Legacy goal references keep their task_completed update.
Both receivers are defensive and use savepoints; project mutation runs under
service RLS context and the same project lock as app writes. Completing through
the app locks the private Task first to match journal writes' lock ordering.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_save, pre_save

logger = logging.getLogger(__name__)


def _before_task_saved(sender, instance, **kwargs):
    """A re-save of already-done private work must not undo a shared reopen."""
    instance._project_was_done = False
    fields = kwargs.get("update_fields")
    if fields is not None and "status" not in fields:
        instance._project_was_done = True
        return
    related = getattr(instance, "related_ref", None)
    if kwargs.get("raw") or instance.status != "done" or not isinstance(related, dict):
        return
    if related.get("object_type") not in {"SharedGoalStep", "shared_goal"}:
        return
    try:
        from django.db import transaction

        with transaction.atomic():
            instance._project_was_done = sender.objects.filter(
                id=instance.id, tenant_id=instance.tenant_id, status="done"
            ).exists()
    except Exception:  # noqa: BLE001 — fail closed without breaking Task.save
        instance._project_was_done = True
        logger.warning("project task prior-status check failed", exc_info=True)


def _on_task_saved(sender, instance, **kwargs) -> None:
    if kwargs.get("raw") or instance.status != "done" or getattr(instance, "_project_was_done", False):
        return
    related = getattr(instance, "related_ref", None) or {}
    if not isinstance(related, dict):
        return
    legacy = related.get("pillar") == "friends" and related.get("object_type") == "shared_goal"
    step_ref = related.get("pillar") == "neighborhood" and related.get("object_type") == "SharedGoalStep"
    if not (legacy or step_ref) or not related.get("object_id"):
        return
    try:
        from django.db import transaction

        from . import access, project_services, services

        # Savepoint keeps a bookkeeping failure from poisoning Task.save's txn.
        with transaction.atomic(), access.backstop_service_context():
            assignment = access.assignment_for_task(instance)
            if assignment:
                step = assignment.step
                expected = step.shared_goal_id if legacy else step.id
                if str(expected) != str(related["object_id"]):
                    return
                goal, member = access.lock_project(instance.tenant, step.shared_goal_id)
                # Re-read after the shared project lock: leave/delete/reassignment
                # must not grant stale consent to the receiver.
                assignment = access.assignment_for_task(instance)
                if assignment is None:
                    return
                step = project_services._row(access.project_steps(goal), step.id)
                project_services._finish(goal, step, instance.tenant, None)
                if (
                    not access.mission_updates()
                    .filter(shared_goal=goal, kind="task_completed", payload__task_id=str(instance.id))
                    .exists()
                ):
                    services._append_update(
                        goal,
                        instance.tenant,
                        None,
                        "task_completed",
                        text=step.title,
                        payload={"task_id": str(instance.id), "title": step.title},
                    )
            elif legacy:
                goal = access.get_mission(related["object_id"])
                if (
                    goal is None
                    or not access.mission_memberships()
                    .filter(shared_goal=goal, tenant_id=instance.tenant_id, status="active")
                    .exists()
                ):
                    return
                goal, _ = access.lock_project(instance.tenant, goal.id)
                if (
                    not access.mission_updates()
                    .filter(shared_goal=goal, kind="task_completed", payload__task_id=str(instance.id))
                    .exists()
                ):
                    services._append_update(
                        goal,
                        instance.tenant,
                        None,
                        "task_completed",
                        text=(instance.title or "")[:120],
                        payload={"task_id": str(instance.id), "title": instance.title or ""},
                    )
    except Exception:  # noqa: BLE001 — never break a Task save over crew bookkeeping
        logger.warning("mission task-completion linkage failed", exc_info=True)


def connect() -> None:
    """Wire the receiver. Called from apps.FriendsConfig.ready()."""
    from apps.journal.models import Task

    pre_save.connect(_before_task_saved, sender=Task, weak=False, dispatch_uid="friends.project_task_prior_status")
    post_save.connect(_on_task_saved, sender=Task, weak=False, dispatch_uid="friends.project_task_completion")
