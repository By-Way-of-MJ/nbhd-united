"""Project requests run in one transaction (inherited from FriendsView)."""

from django.db import connection
from django.test import RequestFactory, TransactionTestCase
from rest_framework.response import Response

from apps.friends.project_views import ProjectView


class _Probe(ProjectView):
    authentication_classes = []
    permission_classes = []
    seen = None

    def get(self, request):
        type(self).seen = connection.in_atomic_block
        return Response({})


class ProjectRequestTransactionTests(TransactionTestCase):
    """TransactionTestCase: no outer test transaction, so this sees the real thing."""

    def test_a_project_request_runs_inside_one_transaction(self):
        self.assertFalse(connection.in_atomic_block)
        response = _Probe.as_view()(RequestFactory().get("/x/"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(_Probe.seen)
        self.assertFalse(connection.in_atomic_block)
