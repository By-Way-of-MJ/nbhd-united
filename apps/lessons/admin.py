from django.contrib import admin

from apps.friends.access import backstop_service_context

from .models import Lesson, LessonConnection, StarJournalEntry, TutoringSession


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    @backstop_service_context()
    def get_deleted_objects(self, objs, request):
        return super().get_deleted_objects(objs, request)

    @backstop_service_context()
    def delete_model(self, request, obj):
        return super().delete_model(request, obj)

    @backstop_service_context()
    def delete_queryset(self, request, queryset):
        return super().delete_queryset(request, queryset)

    list_display = (
        "id",
        "tenant",
        "status",
        "source_type",
        "cluster_id",
        "cluster_label",
        "shared",
        "created_at",
    )
    list_filter = ("tenant", "status", "source_type", "shared")
    search_fields = ("text", "context", "source_ref", "tags")
    ordering = ("-created_at",)


@admin.register(LessonConnection)
class LessonConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "from_lesson",
        "to_lesson",
        "connection_type",
        "similarity",
        "created_at",
    )
    list_filter = ("connection_type",)
    search_fields = (
        "from_lesson__text",
        "to_lesson__text",
        "connection_type",
    )
    ordering = ("-created_at",)


@admin.register(TutoringSession)
class TutoringSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "star",
        "mastery_achieved",
        "new_star_stage",
        "skipped",
        "created_at",
    )
    list_filter = ("mastery_achieved", "new_star_stage", "skipped")
    search_fields = ("star__text", "messages")
    readonly_fields = ("id", "created_at")
    ordering = ("-created_at",)


@admin.register(StarJournalEntry)
class StarJournalEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
        "star",
        "entry_type",
        "created_at",
    )
    list_filter = ("entry_type", "tenant")
    search_fields = ("text", "star__text")
    readonly_fields = ("id", "created_at")
    ordering = ("-created_at",)
