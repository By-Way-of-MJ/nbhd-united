"""Demo A2UI surfaces: build-only tests and the gated app-feed path."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase, override_settings

from apps.common.tenant_tz import tenant_today
from apps.journal.models import Task
from apps.router.chat_history import build_since_page
from apps.router.models import ChatThread, ProactiveOutbound
from apps.router.surfaces.catalog import REF_PATTERN
from apps.router.surfaces.demo import SCENARIOS, build_demo_surface, post_demo_surface
from apps.router.surfaces.validate import validate_surface
from apps.tenants.models import Tenant, User

MISSION_ID = "123e4567-e89b-42d3-a456-426614174000"

MESSAGES = {
    "gym": "Short night, so let's keep tonight lighter.",
    "review": "Here's your week. Two quick decisions, then pick a focus.",
    "stuck": "Let's get this moving with smaller steps.",
    "rough": "Thanks for telling me. Tap whatever fits.",
}
SURFACE_IDS = {
    "gym": "gym-tonight",
    "review": "week-review",
    "stuck": "mission-unstick",
    "rough": "checkin",
}
DATA_KEYS = {
    "gym": {"sleep", "workout", "tasks", "checkin"},
    "review": {"overdue", "focus"},
    "stuck": {"mission", "plan"},
    "rough": {"mood", "energy", "help", "off_plate"},
}
REF_COUNTS = {"gym": 3, "review": 1, "stuck": 1, "rough": 0}


def _build(scenario: str) -> tuple[str, dict]:
    return build_demo_surface(
        scenario,
        mission_id=MISSION_ID if scenario == "stuck" else None,
    )


def _binding_paths(value):
    if isinstance(value, dict):
        if set(value) == {"@path"}:
            yield value["@path"]
            return
        for item in value.values():
            yield from _binding_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _binding_paths(item)


def _when_path(component: dict) -> str | None:
    binding = component.get("when")
    if isinstance(binding, dict) and set(binding) == {"@path"}:
        return binding["@path"]
    return None


def _gate_covers(gates, key: str) -> bool:
    root = f"/{key}"
    return any(path == root or path.startswith(f"{root}/") for path in gates if path.startswith("/"))


def _ungated_ref_bindings(components: list[dict], data_model: dict) -> list[str]:
    """Report bindings to ref-backed data whose own/ancestor ``when`` misses the key."""
    by_id = {component["id"]: component for component in components}
    ref_keys = {key for key, value in data_model.items() if isinstance(value, dict) and "$ref" in value}
    violations: list[str] = []
    pending: list[tuple[str, frozenset[str], str | None]] = [("root", frozenset(), None)]
    seen = set()
    while pending:
        component_id, inherited, template_key = pending.pop()
        state = (component_id, inherited, template_key)
        if state in seen:
            continue
        seen.add(state)
        component = by_id[component_id]
        own = _when_path(component)
        gates = inherited | ({own} if own else set())
        props = {key: value for key, value in component.items() if key != "when"}
        for path in _binding_paths(props):
            key = path.split("/", 2)[1] if path.startswith("/") else template_key
            if key in ref_keys and not _gate_covers(gates, key):
                violations.append(f"{component_id}: {path} is not gated on /{key}")

        children = component.get("children")
        if isinstance(children, list):
            pending.extend((child, gates, None) for child in children)
        elif component["component"] == "List":
            path_key = children["path"].split("/", 2)[1]
            pending.append((children["template"], gates, path_key if path_key in ref_keys else None))
    return violations


class DemoSurfaceBuildTests(SimpleTestCase):
    def _assert_refs_and_static_values_only(self, node) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                self.assertEqual(set(node), {"$ref"})
                self.assertTrue(REF_PATTERN.fullmatch(node["$ref"]))
                return
            for value in node.values():
                self._assert_refs_and_static_values_only(value)
            return
        if isinstance(node, list):
            for item in node:
                self._assert_refs_and_static_values_only(item)
            return
        if isinstance(node, str):
            self.assertEqual(node, "", "dataModel must not store user text")

    def test_every_scenario_builds_a_valid_envelope(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                message_text, envelope = _build(scenario)
                self.assertEqual(message_text, MESSAGES[scenario])
                self.assertEqual(validate_surface(envelope), envelope)
                body = envelope["createSurface"]
                self.assertEqual(body["surfaceId"], SURFACE_IDS[scenario])
                self.assertEqual(body["catalogId"], "nbhd/v1")
                self.assertTrue(body["components"])

    def test_stuck_requires_mission_id(self):
        with self.assertRaisesMessage(ValueError, "mission_id required"):
            build_demo_surface("stuck")
        with self.assertRaisesMessage(ValueError, "mission_id required"):
            build_demo_surface("stuck", mission_id="")

    def test_data_model_has_refs_and_static_values_only(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                _message_text, envelope = _build(scenario)
                data_model = envelope["createSurface"]["dataModel"]
                self.assertEqual(set(data_model), DATA_KEYS[scenario])
                self._assert_refs_and_static_values_only(data_model)
                refs = [item["$ref"] for item in data_model.values() if isinstance(item, dict) and "$ref" in item]
                self.assertEqual(len(refs), REF_COUNTS[scenario])
                if scenario == "stuck":
                    self.assertEqual(refs, [f"mission:{MISSION_ID}"])

    def test_every_ref_bound_component_is_gated_on_its_data_key(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                _message_text, envelope = _build(scenario)
                body = envelope["createSurface"]
                violations = _ungated_ref_bindings(body["components"], body.get("dataModel", {}))
                self.assertEqual(violations, [], f"{scenario}: wrong or missing when gate")

    def test_stuck_root_hides_without_mission(self):
        _message_text, envelope = _build("stuck")
        by_id = {component["id"]: component for component in envelope["createSurface"]["components"]}
        self.assertEqual(by_id["root"]["when"], {"@path": "/mission"})

    def test_guard_reports_a_gate_on_the_wrong_data_key(self):
        _message_text, envelope = _build("gym")
        body = envelope["createSurface"]
        components = [dict(component) for component in body["components"]]
        for component in components:
            if component["id"] == "start-workout":
                component["when"] = {"@path": "/tasks"}
        violations = _ungated_ref_bindings(components, body["dataModel"])
        self.assertTrue(any(item.startswith("start-workout:") for item in violations), violations)

    def test_gym_hides_parts_that_depend_on_missing_data(self):
        _message_text, envelope = _build("gym")
        by_id = {component["id"]: component for component in envelope["createSurface"]["components"]}
        self.assertEqual(by_id["title"]["text"], "Go easier today")
        self.assertEqual(by_id["sleep-summary"]["when"], {"@path": "/sleep/summary"})
        self.assertEqual(by_id["workout-panel"]["when"], {"@path": "/workout"})
        self.assertEqual(by_id["due-label"]["when"], {"@path": "/tasks"})
        self.assertEqual(by_id["due-list"]["when"], {"@path": "/tasks"})
        self.assertEqual(by_id["start-workout"]["when"], {"@path": "/workout"})
        self.assertEqual(by_id["move-first-task"]["when"], {"@path": "/tasks"})

    def test_unknown_scenario_raises(self):
        with self.assertRaisesMessage(ValueError, "unknown scenario: nope"):
            build_demo_surface("nope")


class PostDemoSurfaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="a2ui-demo-owner")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")

    def test_gated_off_tenant_is_refused_and_writes_no_row(self):
        with override_settings(CHAT_SURFACES_TENANT_IDS=""):
            result = post_demo_surface(str(self.tenant.id), "gym")
        self.assertEqual(result, {"status": "refused", "reason": "gate"})
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=self.tenant).exists())

    def test_gated_on_tenant_gets_one_app_proactive_row(self):
        with override_settings(CHAT_SURFACES_TENANT_IDS=str(self.tenant.id)):
            result = post_demo_surface(str(self.tenant.id), "gym")
        self.assertEqual(result["status"], "posted")
        self.assertEqual(result["scenario"], "gym")
        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(result["id"], str(row.id))
        self.assertEqual(row.channel, ProactiveOutbound.Channel.APP)
        self.assertEqual(row.channel_user_id, str(self.user.id))
        self.assertEqual(row.job_name, "a2ui_demo")
        self.assertIsNone(row.thread_id)
        self.assertEqual(len(row.a2ui), 1)

    def test_since_feed_returns_the_row_with_live_refs_resolved(self):
        today = tenant_today(self.tenant)
        task = Task.objects.create(
            tenant=self.tenant,
            title="Water the plants",
            due_date=today,
            status=Task.Status.OPEN,
        )
        with override_settings(CHAT_SURFACES_TENANT_IDS=str(self.tenant.id)):
            result = post_demo_surface(str(self.tenant.id), "gym")
            messages, _cursor = build_since_page(self.tenant, str(self.thread.id), cursor=None, limit=50)

        rows = [message for message in messages if message["id"] == f"cron:{result['id']}"]
        self.assertEqual(len(rows), 1)
        surface = rows[0]["a2ui"][0]
        self.assertEqual(surface["createSurface"]["surfaceId"], "gym-tonight")
        self.assertEqual(
            surface["createSurface"]["dataModel"]["tasks"],
            [
                {
                    "id": str(task.id),
                    "title": "Water the plants",
                    "due_date": today.isoformat(),
                    "status": "open",
                    "done": False,
                }
            ],
        )
