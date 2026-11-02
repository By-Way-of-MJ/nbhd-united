# Private step preparation

For tenants in `STEP_PREP_TENANT_IDS`, Start and an answered prep choice request
one bounded research run after the write commits. The request finishes without
waiting for a container wake or a share write. Existing working preparations,
Stop, and shared notes do not request a run. Notes remain on the daily update job.

The scheduler locks the tenant only while checking the flag, budget, pending
one-shot and rolling limit, and creating its claim. It permits one pending
`Step Prep Now ` job and at most six claims per tenant in 24 hours, including
failed wakes. Disabled and spent claims survive gateway cache refreshes so the
limit remains durable. Other internal jobs keep their existing cache behavior.

The one-shot fires 90 seconds after an awake claim or 270 seconds after a
hibernated claim. Wake runs after commit through `wake_for_cron_task`, preserving
the existing cron idle check and re-hibernation. Failed wakes disable the claim;
other scheduling failures are logged. The daily research job remains the
backstop. Both daily and one-shot research use `step_prep_job()` for the prompt,
model, isolated session, light context, exact tool wall and silent delivery.
The cron carries no member text; `next` supplies the frozen brief at execution.

OpenClaw 2026.9.4 receives one-shots through the signed cron file and operator
CLI, without Django gateway `cron.add` or `cron.run`. Its CLI rejects timestamps
more than 60 seconds in the past. Our helper is stricter: it skips any past fire
without re-adding it. The helper keeps any running job and a fired one-shot
without a recorded run for up to two hours, covering queued work. After a run
settles, successful and failed spent declarations are removed; a one-shot that
never ran is removed once its fire is two hours old. The 270-second delay allows the
normal cold wake and helper poll; an unusually slow boot can miss it and rely
on the daily backstop. Successful silent one-shots auto-delete in OpenClaw.
Django's retirement sweep disables spent rows, and neither the seed refresher
nor reconciliation recreates these unmanaged jobs.

A signed research run token marks completion as eligible for one `prep_result`
push to the requesting holder's devices. Update tokens never qualify, including
when another member's note changes the result status. Delivery runs after commit
and is best-effort. The quiet-project rule treats this result as attention.
Only the shortened step title appears in its body; neither the page nor the
question is included. The APNs collapse ID identifies the prep. The payload uses
`type=step_answer`, `kind=prep_result`, `mission_id` and `step_id`, which the iOS
project router already opens on that step. An already-used report token becomes
stale, so retrying the report cannot dispatch a second push.
