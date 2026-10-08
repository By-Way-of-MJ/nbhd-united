"""Web Neighborhood "reach" + the absorbed-ledger grouping.

``reach`` = friends-of-friends as NAMELESS bucketed counts: never ids, names or
handles; null below 3; the viewer, their own neighbors, blocked-either-way and
gone/opted-out accounts never count; a friend with the Neighborhood off hides
their network entirely. ``reach_total`` buckets the deduped union.

Absorbed: chat is logged one ledger row per message under one neutral label, so
the list carries an opaque ``group_key`` + ``kind_label`` + ``created_at`` and
``purge-group`` tombstones a whole group.
"""

from __future__ import annotations

import json
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User

from . import access, services
from .models import AbsorbedItem, Friendship, NeighborProfile


def _tenant(name: str, **fields) -> Tenant:
    user = User.objects.create_user(username=name, password="pass", display_name=name.title())
    tenant = Tenant.objects.create(user=user, status=fields.pop("status", "active"), friends_enabled=True, **fields)
    NeighborProfile.objects.create(tenant=tenant, handle=name, display_name=name.title())
    return tenant


def _edge(a, b, status=Friendship.Status.ACCEPTED, blocked_by=None) -> Friendship:
    return Friendship.objects.create(
        requester=a, addressee=b, status=status, blocked_by=blocked_by, responded_at=timezone.now()
    )


def _fof(friend, n: int, prefix: str) -> list[Tenant]:
    people = [_tenant(f"{prefix}{i}") for i in range(n)]
    for p in people:
        _edge(friend, p)
    return people


def _home_row(viewer, friend) -> dict:
    home = services.neighborhood_home(viewer)
    return next(n for n in home["neighbors"] if n["handle"] == friend.neighbor_profile.handle)


class ReachBucketTest(TestCase):
    def test_bucket_edges(self):
        cases = {
            0: None,
            2: None,
            3: "3+",
            4: "3+",
            5: "5+",
            9: "5+",
            10: "10+",
            24: "10+",
            25: "25+",
            49: "25+",
            50: "50+",
            99: "50+",
            100: "100+",
            5000: "100+",
        }
        for count, expected in cases.items():
            self.assertEqual(access.reach_bucket(count), expected, count)


class ReachExclusionTest(TestCase):
    def setUp(self):
        self.me = _tenant("me")
        self.kiho = _tenant("kiho")
        _edge(self.me, self.kiho)

    def test_counts_only_strangers_one_step_away(self):
        _fof(self.kiho, 3, "stranger")
        mutual = _tenant("mutual")
        _edge(self.me, mutual)
        _edge(self.kiho, mutual)  # already my neighbor → never counted
        i_blocked = _tenant("iblocked")
        _edge(self.kiho, i_blocked)
        _edge(self.me, i_blocked, status=Friendship.Status.BLOCKED, blocked_by=self.me)
        blocked_me = _tenant("blockedme")
        _edge(self.kiho, blocked_me)
        _edge(blocked_me, self.me, status=Friendship.Status.BLOCKED, blocked_by=blocked_me)
        deleted = _tenant("deleted", status=Tenant.Status.DELETED)
        _edge(self.kiho, deleted)
        suspended = _tenant("suspended", status=Tenant.Status.SUSPENDED)
        _edge(self.kiho, suspended)
        deactivated = _tenant("deactivated")
        deactivated.user.is_active = False
        deactivated.user.save(update_fields=["is_active"])
        _edge(self.kiho, deactivated)
        nbhd_off = _tenant("nbhdoff", neighborhood_enabled=False)
        _edge(self.kiho, nbhd_off)
        pending = _tenant("pendingfof")
        _edge(self.kiho, pending, status=Friendship.Status.PENDING)  # not a friend of kiho yet

        home = services.neighborhood_home(self.me)
        row = next(n for n in home["neighbors"] if n["handle"] == "kiho")
        self.assertEqual(row["reach"], "3+")  # exactly the 3 strangers
        self.assertEqual(home["reach_total"], "3+")

        # 2 more strangers → exactly 5, proving the excluded 8 were never counted.
        _fof(self.kiho, 2, "more")
        self.assertEqual(_home_row(self.me, self.kiho)["reach"], "5+")

    def test_two_friends_of_friend_is_null_three_is_bucketed(self):
        _fof(self.kiho, 2, "s")
        self.assertIsNone(_home_row(self.me, self.kiho)["reach"])
        _fof(self.kiho, 1, "third")
        self.assertEqual(_home_row(self.me, self.kiho)["reach"], "3+")

    def test_nine_is_five_plus_ten_is_ten_plus(self):
        _fof(self.kiho, 9, "s")
        self.assertEqual(_home_row(self.me, self.kiho)["reach"], "5+")
        _fof(self.kiho, 1, "tenth")
        self.assertEqual(_home_row(self.me, self.kiho)["reach"], "10+")

    def test_friend_with_neighborhood_off_hides_their_network(self):
        _fof(self.kiho, 6, "s")
        Tenant.objects.filter(id=self.kiho.id).update(neighborhood_enabled=False)
        home = services.neighborhood_home(self.me)
        self.assertIsNone(home["neighbors"][0]["reach"])
        self.assertIsNone(home["reach_total"])

    def test_deleted_friend_hides_their_network(self):
        _fof(self.kiho, 6, "s")
        Tenant.objects.filter(id=self.kiho.id).update(status=Tenant.Status.DELETED)
        self.assertIsNone(_home_row(self.me, self.kiho)["reach"])

    def test_payload_never_names_anyone_beyond_my_neighbors(self):
        _fof(self.kiho, 12, "hidden")
        home = services.neighborhood_home(self.me)
        body = json.dumps(home, default=str)
        self.assertNotIn("hidden", body)
        self.assertEqual(home["neighbors"][0]["reach"], "10+")
        self.assertEqual(home["reach_total"], "10+")


class ReachTotalTest(TestCase):
    def test_total_dedupes_people_reachable_through_several_friends(self):
        me = _tenant("me")
        a, b = _tenant("a"), _tenant("b")
        _edge(me, a)
        _edge(me, b)
        shared = _fof(a, 4, "shared")
        for p in shared:
            _edge(b, p)
        home = services.neighborhood_home(me)
        self.assertEqual({n["reach"] for n in home["neighbors"]}, {"3+"})
        self.assertEqual(home["reach_total"], "3+")  # 4 people, not 8
        _fof(b, 1, "only_b")
        self.assertEqual(services.neighborhood_home(me)["reach_total"], "5+")

    def test_no_friends_means_null_total(self):
        me = _tenant("lonely")
        self.assertIsNone(services.neighborhood_home(me)["reach_total"])


class ReachQueryBoundTest(TestCase):
    def test_twenty_friends_cost_two_queries(self):
        me = _tenant("me")
        for i in range(20):
            friend = _tenant(f"f{i}")
            _edge(me, friend)
            _fof(friend, 3, f"f{i}_")
        edges = list(Friendship.objects.filter(requester=me, status=Friendship.Status.ACCEPTED))
        with self.assertNumQueries(2):
            per, total = access.reach_by_counterpart(me, edges)
        self.assertEqual(set(per.values()), {"3+"})
        self.assertEqual(total, "50+")  # 60 distinct people

    def test_home_query_count_is_flat_in_friends(self):
        me = _tenant("me")

        def home_queries():
            with CaptureQueriesContext(connection) as ctx:
                services.neighborhood_home(me)
            return len(ctx.captured_queries)

        first = _tenant("f0")
        _edge(me, first)
        _fof(first, 3, "f0_")
        one = home_queries()
        for i in range(1, 20):
            friend = _tenant(f"f{i}")
            _edge(me, friend)
            _fof(friend, 3, f"f{i}_")
        self.assertEqual(home_queries(), one)


class AbsorbedGroupingTest(TestCase):
    def setUp(self):
        self.me = _tenant("me")
        self.kiho = _tenant("kiho")
        self.ana = _tenant("ana")
        _edge(self.me, self.kiho)
        _edge(self.me, self.ana)
        for _ in range(4):  # the chat absorb logs one row per friend message
            services._log_absorbed(
                self.me, AbsorbedItem.SourceKind.FRIEND_MESSAGE, uuid.uuid4(), self.kiho.id, "Chat with @kiho"
            )
        services._log_absorbed(
            self.me, AbsorbedItem.SourceKind.FRIEND_MESSAGE, uuid.uuid4(), self.ana.id, "Chat with @ana"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.me.user)

    def test_same_source_logged_twice_is_one_row(self):
        source = uuid.uuid4()
        for _ in range(2):
            services._log_absorbed(
                self.me, AbsorbedItem.SourceKind.FRIEND_MESSAGE, source, self.kiho.id, "Chat with @kiho"
            )
        self.assertEqual(AbsorbedItem.objects.filter(tenant=self.me, source_id=source).count(), 1)

    def test_list_groups_per_sender_with_kind_label_and_dates(self):
        rows = self.client.get("/api/v1/friends/absorbed/").json()
        kiho_rows = [r for r in rows if r["from_handle"] == "kiho"]
        self.assertEqual(len(kiho_rows), 4)
        self.assertEqual(len({r["group_key"] for r in kiho_rows}), 1)
        ana_key = next(r["group_key"] for r in rows if r["from_handle"] == "ana")
        self.assertNotEqual(ana_key, kiho_rows[0]["group_key"])
        for r in rows:
            self.assertEqual(r["kind_label"], "chat message")
            self.assertTrue(r["created_at"])
            self.assertNotIn(str(self.kiho.id), r["group_key"])
            self.assertNotIn(str(self.kiho.id).replace("-", ""), r["group_key"])

    def test_group_key_is_not_comparable_across_viewers(self):
        mine = services._absorbed_group_key(self.me.id, "friend_message", self.kiho.id, None)
        theirs = services._absorbed_group_key(self.ana.id, "friend_message", self.kiho.id, None)
        self.assertNotEqual(mine, theirs)

    def test_list_query_count_is_bounded(self):
        with self.assertNumQueries(2):
            services.list_absorbed(self.me)

    def test_purge_group_tombstones_only_that_group(self):
        kiho_key = next(
            r["group_key"] for r in self.client.get("/api/v1/friends/absorbed/").json() if r["from_handle"] == "kiho"
        )
        resp = self.client.post("/api/v1/friends/absorbed/purge-group/", {"group_key": kiho_key}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["purged"], 4)
        remaining = self.client.get("/api/v1/friends/absorbed/").json()
        self.assertEqual([r["from_handle"] for r in remaining], ["ana"])
        # Idempotent: nothing left in that group.
        again = self.client.post("/api/v1/friends/absorbed/purge-group/", {"group_key": kiho_key}, format="json")
        self.assertEqual(again.json()["purged"], 0)

    def test_purge_group_cannot_touch_another_viewers_items(self):
        other = _tenant("other")
        services._log_absorbed(
            other, AbsorbedItem.SourceKind.FRIEND_MESSAGE, uuid.uuid4(), self.kiho.id, "Chat with @kiho"
        )
        their_key = services._absorbed_group_key(other.id, "friend_message", self.kiho.id, None)
        resp = self.client.post("/api/v1/friends/absorbed/purge-group/", {"group_key": their_key}, format="json")
        self.assertEqual(resp.json()["purged"], 0)
        self.assertEqual(AbsorbedItem.objects.filter(tenant=other, purged_at__isnull=True).count(), 1)

    def test_purge_group_requires_a_key(self):
        resp = self.client.post("/api/v1/friends/absorbed/purge-group/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
