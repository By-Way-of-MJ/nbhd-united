"""Owner activity feed routes. Mounted at ``/api/v1/activity/`` (see config/urls.py)."""

from django.urls import path

from apps.router.activity_views import ActivitySinceEventView, ActivitySinceView

urlpatterns = [
    path("since/", ActivitySinceView.as_view(), name="activity-since"),
    path("since/events", ActivitySinceEventView.as_view(), name="activity-since-events"),
]
