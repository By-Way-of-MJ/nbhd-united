"""Pure calendar-day evidence builder over access.project_snapshot(goal).

A dated step has duration max(0, due_date - start_date); absent start means
zero duration. Open work starts no earlier than today or its scheduled start,
and no earlier than any open predecessor's projected finish (same-day handoff).
Done/skipped predecessors do not constrain unfinished work. Undated work has
unknown finish, never an invented due date.

Slack is the signed number of calendar days a scheduled due date can slip
before moving a dated dependent's scheduled start or a reachable milestone
(target_date), including the goal target as a terminal deadline. We propagate
those latest finishes backwards, subtracting intermediate durations. Negative
slack means the schedule already conflicts; zero/negative slack is critical.
No due date, closed work, or no dated downstream constraint => slack null.
``moves_if_late`` lists descendants whose projected finish would change if this
step's projected finish slipped ONE day, accounting for intervening buffers.
These are schedule facts, not predictions about a person's capacity.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
from uuid import UUID

CLOSED = {"done", "skipped"}
# "Needs someone": how long an owned step may sit past its date, how long an
# owner's "still mine" quiets that, and how soon an ownerless step counts as due.
QUIET_AFTER_DAYS = 3
KEPT_QUIET_DAYS = 7
UNOWNED_DUE_SOON_DAYS = 2


def _attention(step, owners, assignments, today):
    """Why the group should look at this step, or None. Schedule facts only —
    never a judgement about a person."""
    if step["status"] == "in_review":
        return "needs_look"
    if step["status"] in CLOSED:
        return None
    due = step.get("due_date")
    if not owners:
        if any(a["status"] == "released" for a in assignments):
            return "open_again"
        return "unowned_due" if due and (due - today).days <= UNOWNED_DUE_SOON_DAYS else None
    if due and (today - due).days >= QUIET_AFTER_DAYS:
        owner_ids = {o["id"] for o in owners}
        kept = [
            a["kept_at"].date()
            for a in assignments
            if a.get("kept_at") and a["status"] == "accepted" and a["membership_id"] in owner_ids
        ]
        if not any((today - day).days < KEPT_QUIET_DAYS for day in kept):
            return "overdue"
    return None


def _json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def build_plan(goal, *, today):
    """Accept an eager control-plane dict; perform no I/O or ORM access."""
    data = deepcopy(goal)
    steps = {str(s["id"]): s for s in data["steps"]}
    milestones = {str(m["id"]): m for m in data["milestones"]}
    parents = {sid: [] for sid in steps}
    children = {sid: [] for sid in steps}
    for edge in data["edges"]:
        a, b = str(edge["blocker_id"]), str(edge["blocked_id"])
        if a not in steps or b not in steps:
            raise ValueError("Dependency references a foreign step")
        parents[b].append(a)
        children[a].append(b)
    order, visiting, visited = [], set(), set()

    def visit(sid):
        if sid in visiting:
            raise ValueError("Dependency cycle")
        if sid in visited:
            return
        visiting.add(sid)
        for parent in parents[sid]:
            visit(parent)
        visiting.remove(sid)
        visited.add(sid)
        order.append(sid)

    for sid in steps:
        visit(sid)
    duration = {
        sid: max(0, (s["due_date"] - s["start_date"]).days) if s.get("due_date") and s.get("start_date") else 0
        for sid, s in steps.items()
    }

    def schedule(delay=None):
        finish = {}
        for sid in order:
            s = steps[sid]
            if s["status"] in CLOSED:
                stamp = s.get("completed_at")
                finish[sid] = stamp.date() if stamp else s.get("due_date")
                continue
            if not s.get("due_date"):
                finish[sid] = None
                continue
            open_parents = [p for p in parents[sid] if steps[p]["status"] not in CLOSED]
            if any(finish[p] is None for p in open_parents):
                finish[sid] = None
                continue
            start = max([today, s.get("start_date") or s["due_date"]] + [finish[p] for p in open_parents])
            finish[sid] = start + timedelta(days=duration[sid])
            if sid == delay:
                finish[sid] += timedelta(days=1)
        return finish

    finish = schedule()
    latest = {}
    for sid in reversed(order):
        s = steps[sid]
        constraints = []
        if s["status"] not in CLOSED:
            milestone = milestones.get(str(s.get("milestone_id")))
            if milestone and milestone.get("target_date"):
                constraints.append(milestone["target_date"])
            if data.get("target_date"):
                constraints.append(data["target_date"])
            for child in children[sid]:
                if steps[child]["status"] in CLOSED:
                    continue
                if steps[child].get("start_date"):
                    constraints.append(steps[child]["start_date"])
                elif steps[child].get("due_date"):
                    constraints.append(steps[child]["due_date"])
                if latest.get(child):
                    constraints.append(latest[child] - timedelta(days=duration[child]))
        latest[sid] = min(constraints) if constraints else None

    members = {str(m["id"]): m for m in data["members"]}
    for sid, s in steps.items():
        assignments = [a for a in data.get("assignments", []) if str(a["step_id"]) == sid]
        s["assignments"] = assignments
        s["owners"] = [
            dict(members[str(a["membership_id"])], assignment_status=a["status"])
            for a in assignments
            if a["status"] == "accepted"
            and str(a["membership_id"]) in members
            and members[str(a["membership_id"])]["status"] == "active"
        ]
        # Who let go of it (stepped back or left), newest first, with their hand-off line.
        s["released"] = sorted(
            (
                {"membership_id": a["membership_id"], "note": a.get("note") or "", "released_at": a.get("released_at")}
                for a in assignments
                if a["status"] == "released" and str(a["membership_id"]) in members
            ),
            key=lambda r: str(r["released_at"] or ""),
            reverse=True,
        )
        s["attention"] = _attention(s, s["owners"], assignments, today)
        s["blocked_by_open"] = sorted(p for p in parents[sid] if steps[p]["status"] not in CLOSED)
        s["ready"] = s["status"] not in CLOSED and not s["blocked_by_open"]
        s["slack_days"] = (
            (latest[sid] - s["due_date"]).days
            if latest[sid] and s.get("due_date") and s["status"] not in CLOSED
            else None
        )
        s["on_critical_path"] = s["slack_days"] is not None and s["slack_days"] <= 0
        shifted = schedule(sid) if s["status"] not in CLOSED else finish
        s["moves_if_late"] = sorted(other for other in steps if other != sid and shifted[other] != finish[other])
        s["projected_date"] = finish[sid]
    late = any(s.get("due_date") and s["due_date"] < today for s in steps.values() if s["status"] not in CLOSED)
    risk = any(
        s["on_critical_path"] or (finish[sid] and s.get("due_date") and finish[sid] > s["due_date"])
        for sid, s in steps.items()
        if s["status"] not in CLOSED
    )
    for mid, m in milestones.items():
        ids = [sid for sid, s in steps.items() if str(s.get("milestone_id")) == mid]
        m["done_count"] = sum(steps[sid]["status"] == "done" for sid in ids)
        m["total"] = len(ids)
        m["projected_date"] = (
            max((finish[sid] for sid in ids), default=None) if ids and all(finish[sid] for sid in ids) else None
        )
        unfinished = any(steps[sid]["status"] not in CLOSED for sid in ids)
        if m.get("target_date") and unfinished:
            late |= m["target_date"] < today
            risk |= bool(m["projected_date"] and m["projected_date"] > m["target_date"])
    if data.get("target_date") and any(s["status"] not in CLOSED for s in steps.values()):
        late |= data["target_date"] < today
        risk |= any(d and d > data["target_date"] for d in finish.values())
    data.update(
        health="late" if late else "at_risk" if risk else "on_track",
        done_count=sum(s["status"] == "done" for s in steps.values()),
        total=len(steps),
    )
    data.pop("assignments", None)
    return _json(data)
