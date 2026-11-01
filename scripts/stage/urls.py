"""Use the production views, with the job's requested root chat URL alias."""

from django.urls import path

from apps.router.chat_views import ChatMessageView
from config.urls import urlpatterns as application_urls

urlpatterns = [path("api/v1/chat/", ChatMessageView.as_view()), *application_urls]
