from django.contrib import admin
from django.db.models import F
from django.utils.translation import gettext_lazy as _

from .models import AdAttribution, MonthlyBudget, UsageRecord


@admin.register(UsageRecord)
class UsageRecordAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "tenant",
        "input_tokens",
        "output_tokens",
        "model_used",
        "cost_estimate",
        "created_at",
    )
    list_filter = ("event_type",)
    date_hierarchy = "created_at"


class OverBudgetFilter(admin.SimpleListFilter):
    """Filter MonthlyBudget rows by whether spent_dollars >= budget_dollars."""

    title = _("capped")
    parameter_name = "capped"

    def lookups(self, request, model_admin):
        return [
            ("yes", _("Yes")),
            ("no", _("No")),
        ]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(spent_dollars__gte=F("budget_dollars"))
        if self.value() == "no":
            return queryset.exclude(spent_dollars__gte=F("budget_dollars"))
        return queryset


@admin.register(MonthlyBudget)
class MonthlyBudgetAdmin(admin.ModelAdmin):
    list_display = ("month", "budget_dollars", "spent_dollars", "capped")
    list_filter = (OverBudgetFilter,)

    @admin.display(boolean=True, description="capped")
    def capped(self, obj):
        return obj.is_over_budget


@admin.register(AdAttribution)
class AdAttributionAdmin(admin.ModelAdmin):
    """Read-only audit view: attribution rows are written by the app and resolver."""

    list_display = (
        "tenant",
        "status",
        "campaign_id",
        "ad_group_id",
        "keyword_id",
        "attempts",
        "created_at",
        "resolved_at",
    )
    list_filter = ("status",)
    search_fields = ("tenant__user__email", "tenant__user__username")
    date_hierarchy = "created_at"
    readonly_fields = tuple(field.name for field in AdAttribution._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
