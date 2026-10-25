"""Shared, query-free permission rules for project writes and plan capabilities.

Callers must authenticate active membership before using these role predicates.
Assignment rows contain only membership_id and status, never private Tasks.
"""


def can_edit_project(role):
    return role == "owner"


def can_remove_member(role, target_role, target_status):
    return can_edit_project(role) and target_role != "owner" and target_status == "active"


def can_cancel_invite(role, target_status):
    return can_edit_project(role) and target_status == "invited"


def can_edit_step(role, tenant_id, membership_id, created_by_id, assignments):
    if can_edit_project(role) or any(
        row["membership_id"] == membership_id and row["status"] == "accepted" for row in assignments
    ):
        return True
    return created_by_id == tenant_id and not any(
        row["membership_id"] != membership_id and row["status"] in {"asked", "accepted"} for row in assignments
    )


def can_edit_milestone(role, tenant_id, created_by_id):
    return can_edit_project(role) or tenant_id == created_by_id
