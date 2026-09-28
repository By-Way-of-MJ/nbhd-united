# Projects v2 backend core (P1a)

All paths below are under `/api/v1/friends/`. New plan endpoints require an app
JWT, `neighborhood_enabled`, and the shared `PROJECTS_V2_TENANT_IDS` allowlist.
They return 404 when the rollout flag is off. Runtime internal credentials and
personal access tokens do not authenticate these new endpoints. The allowlist
uses the existing comma-list helper (empty denies all; exact `*` enables all).
`/api/v1/tenants/me/` exposes the same `projects_v2_enabled` decision.

| Method | Path | Caller |
| --- | --- | --- |
| GET | `missions/<id>/plan/` | Active project member |
| POST | `missions/<id>/steps/` | Active member |
| PATCH, DELETE | `missions/<id>/steps/<step_id>/` | Authorized step editor (below); PATCH requires integer `version` |
| POST | `missions/<id>/steps/<step_id>/ask/` | Active member; `membership_ids` must identify active members or valid invitees of this project |
| POST | `missions/<id>/steps/<step_id>/respond/` | Asked member; `answer`: yes, dates, smaller, no |
| POST | `missions/<id>/steps/<step_id>/complete/` | Active, accepted step owner |
| POST | `missions/<id>/steps/<step_id>/reopen/` | Active, accepted step owner |
| POST | `missions/<id>/milestones/` | Active member |
| PATCH, DELETE | `missions/<id>/milestones/<milestone_id>/` | Active milestone creator or project owner |
| POST | `missions/<id>/dependencies/` | Active member; `blocker_id`, `blocked_id` from this project |
| DELETE | `missions/<id>/dependencies/<dependency_id>/` | Authorized editor of the edge's **blocked** step |

The plan contains member identity (`id`, `handle`, `display_name`, `hue`, role,
status), `my_membership_id`, `my_role`, ordered milestones and
steps, assignment states/counters, and dependency edges. Private Task fields and
IDs are excluded. Only the caller's membership includes `muted`; it is absent from
other members, including their identity in step owners. No journal rows are read
by the projection.

Step PATCH and DELETE require active membership and at least one of:

- The caller is an accepted owner of the step.
- The caller created the step and **no other member** has an `asked` or `accepted`
  assignment on it. A self-ask does not remove creator rights; other members'
  `countered` and `declined` assignments do not block them. Outstanding asks or
  acceptances still block creator rights after that member leaves.
- The caller has project membership role `owner`.

Otherwise the API returns 403: "Ask the step's owner to change it."
Deleting a dependency uses these same rights on its blocked step; creating the
edge or owning its blocker grants no additional deletion rights. Milestone PATCH
and DELETE require the milestone's creator or a project owner, with active
membership. Any active member may still create steps, milestones and dependencies.
Project ownership alone does not authorize step completion or reopening: those
actions still require an accepted step assignment.

Step inputs are `title`, `description`, `start_date`, `due_date`, `milestone_id`,
`order`, and `status` (open/in_progress). Completion and reopen use their own
owner-only endpoints. A stale version returns 409 with refresh-and-retry copy
and the current integer version. Milestone inputs are `title`, `target_date`,
and `order`. Dates are ISO calendar dates or null. Start cannot exceed due.
Creation returns the object's ID, with version for steps; DELETE returns 204.

Responding `yes` creates exactly one private Task for that accepted owner.
`dates` requires `start` or `due`; `smaller` requires a note up to 200 characters;
other notes permit 500. Counteroffers record proposed dates without moving the
shared schedule. The app must drive this dates counter-offer flow:

1. The asked member POSTs `respond/` with `{"answer":"dates","start":"2026-10-03","due":"2026-10-05"}`
   (at least one date). This records a `countered` assignment and creates no Task.
2. The asker PATCHes the step with the current `version` and the agreed
   `start_date`/`due_date`. Being the asker grants no editing privilege: they must
   satisfy the step edit rules above. A creator can edit after this counter only
   if no other `asked`/`accepted` assignment blocks them; a project owner can edit.
   If the asker lacks rights, an authorized editor must make this change. A 409
   requires reloading the plan and reviewing the current dates before retrying.
3. The asker POSTs `ask/` with that member's `membership_ids`. This resets the
   countered assignment to `asked` and clears its counter dates and note.
4. The member POSTs `respond/` with `{"answer":"yes"}`. Only then is their private
   Task minted, using the agreed step title and due date.

Declined assignments remain as history but do
not count as owners. Repeated asks preserve accepted owners and outstanding
asks. Invitees can receive an ask but must join before responding. Neither asks nor counters create a Task. Notes never enter activity payloads.

A project-row lock serializes mutations, bounds (8 milestones, 60 steps, 120
edges), and dependency DFS. Task text preparation happens before the write
transaction; acceptance checks the step version and membership again under the
lock. No external work is performed inside that transaction.

Only an accepted active owner's exactly linked Task can complete a shared step.
The receiver uses a savepoint and service RLS context, and cannot raise into
Task.save. Re-saving already-done work does not undo a shared reopen. Completing
or reopening through the project changes only the caller's private Task. Other
owners' Tasks remain private and independent. Leaving or deleting a step keeps
private Tasks. When an accepted owner PATCHes a title, only their linked Task's
title is updated through journal text authoring. A start/due PATCH syncs the
resulting due date to that same Task (journal Tasks have no start date). Clearing
the due date clears it in their Task. Other owners' Tasks and private descriptions
are untouched; a project owner without an accepted assignment changes no Task.
The write locks the caller's Task before the project, then rechecks membership,
edit rights, version and assignment link before changing either row.
Completion/unblock/milestone events are control-plane updates;
P1a adds no pushes or scheduling.

`plan_projection.py` documents calendar-day math precisely. Slack is the signed
buffer from scheduled due to the earliest downstream scheduled start or reachable
milestone/goal deadline, propagated backward through durations. Null means no due
date, closed work, or no dated constraint; nonpositive slack is critical. Projected
finishes propagate open dependencies forward, with same-day handoff. An undated
open predecessor makes the dependent finish unknown. `moves_if_late` means a
one-calendar-day slip in this step's projected finish changes those descendants'
projected finishes. Done/skipped work resolves blockers. Health is late for an
unfinished overdue step/milestone/goal; otherwise at_risk for critical or forecast
late work; otherwise on_track. Missing estimates are not invented.

Legacy mission URLs and response keys remain available. `POST missions/` accepts
`member_friendship_ids` (including an empty list for a solo project) behind the
flag, while `friendship_id` still invites the legacy single neighbor. All newly created
projects leave the friendship FK null; historical projects retain it. An invitee's edge
to the creator must remain accepted to preview/join a friendship-less project.
`POST missions/<id>/tasks/` now also creates an accepted step for the caller and
links the private Task to `SharedGoalStep`. Its `{task_id, title}` response stays
unchanged. Approving an existing mission-task proposal also creates an accepted
step and now rechecks active membership under the project lock. Mission/step titles cap at 120; descriptions/notes cap at 500; existing
commitments cap at 200. Hygiene removes control/format characters, collapses
whitespace, and rejects overlength text. The API stores strings as plain text;
consumers must render text rather than interpret it as markup/instructions.

Migration 0014 retains one step per legacy task_added update, using the legacy
projection's case-insensitive stripped title match, same member, to detect done
work. It links only an existing Task belonging to that member. It preserves old
Task references; the receiver handles both reference formats. Removed historical
memberships leave an unassigned step. Historical rows are not discarded to meet
new creation limits. The migration is idempotent and its data reversal is a no-op.

The access module remains the primary application boundary. Migration 0015 adds
FORCE RLS with named `app_user` policies on all four new tables: active project
members can read, an unset/foreign/invited tenant fails closed, and trusted service
context supports the receiver. `check_friends_rls` includes them. All eight friends
backstop tables are exempt from the boot-time `disable_rls` sweep through
`RLS_KEEP_ENABLED`; the regression test checks the complete `FRIENDS_TABLES` set
and verifies enforcement remains enabled after the sweep.
