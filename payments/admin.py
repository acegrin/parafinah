import json, uuid
from django.contrib import admin, messages
from django.urls import path
from django.shortcuts import render, redirect
from django import forms
from django.core.exceptions import ValidationError
from .models import RunnerEntry, LevelConfig, EventEntry, NotificationEntry, NewsEntry, NewsManifestState, \
    DataManifestStateEntry, PackageEntry, WorldEntry
from .admin_forms import MissionGeneratorForm
from .mission_system import build_mission_preview, save_mission_from_preview
from .validation import bump_validation_code
from django.core.exceptions import PermissionDenied

from .models import (
    LeaderboardEntry,
    LeaderboardPeriodResult,
    LeaderboardReward,
    LeaderboardPeriodControl,
    Mission,
    ValidationEntry,
)

from leaderboards.services import (
    finalize_period,
    distribute_rewards,
    recalc_ranks,
    get_period_keys,
)

# ---------- LeaderboardEntryAdmin ----------
@admin.register(LeaderboardEntry)
class LeaderboardEntryAdmin(admin.ModelAdmin):
    list_display = ("period", "period_key", "rank", "player_name", "score", "user_id", "updated_at", "is_finalized")
    list_filter = ("period", "period_key", "is_finalized")
    ordering = ("period", "period_key", "rank")

# ---------- LeaderboardPeriodResultAdmin ----------
@admin.register(LeaderboardPeriodResult)
class LeaderboardPeriodResultAdmin(admin.ModelAdmin):
    list_display = ("period", "period_key", "rank", "player_name", "score", "finalized_at")
    list_filter = ("period", "period_key")
    ordering = ("period", "period_key", "rank")

# ---------- LeaderboardRewardAdmin ----------
@admin.register(LeaderboardReward)
class LeaderboardRewardAdmin(admin.ModelAdmin):
    list_display = ("period", "rank", "reward_type", "reward_preview")
    list_filter = ("period", "reward_type")
    search_fields = ("period",)
    ordering = ("period", "rank")
    fieldsets = (
        ("Target", {"fields": ("period", "rank")}),
        ("Reward", {"fields": ("reward_type", "reward_payload")}),
    )

    def reward_preview(self, obj):
        if not obj.reward_payload:
            return "-"
        if obj.reward_type == "gold":
            return f"{obj.reward_payload.get('gold', 0)} gold"
        return str(obj.reward_payload)
    reward_preview.short_description = "Reward"

# ---------- LeaderboardPeriodControlAdmin ----------
PERIOD_CHOICES = [
    ("daily", "Daily"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
]

class LeaderboardControlForm(forms.Form):
    period = forms.ChoiceField(choices=PERIOD_CHOICES, label="Period Type")
    is_locked = forms.BooleanField(required=False, initial=False, label="Lock Period")
    is_finalized = forms.BooleanField(required=False, initial=False, label="Finalize Period")
    distribute_rewards_toggle = forms.BooleanField(required=False, initial=False, label="Distribute Rewards")

@admin.register(LeaderboardPeriodControl)
class LeaderboardPeriodControlAdmin(admin.ModelAdmin):

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "control-panel/",
                self.admin_site.admin_view(self.control_panel),
                name="leaderboard-control-panel",
            ),
        ]
        return custom_urls + urls

    def control_panel(self, request):
        if request.method == "POST":
            form = LeaderboardControlForm(request.POST)
            if form.is_valid():
                period = form.cleaned_data["period"]
                is_locked = form.cleaned_data["is_locked"]
                is_finalized = form.cleaned_data["is_finalized"]
                distribute_rewards_toggle = form.cleaned_data["distribute_rewards_toggle"]

                # Server-authoritative period key
                period_key = get_period_keys()[period]

                # Update or create control record
                LeaderboardPeriodControl.objects.update_or_create(
                    period=period,
                    period_key=period_key,
                    defaults={
                        "is_locked": is_locked,
                        "is_finalized": is_finalized,
                        "rewards_distributed": distribute_rewards_toggle,
                    },
                )

                # 🔴 THIS is the line that flips existing entries
                LeaderboardEntry.objects.filter(
                    period=period,
                    period_key=period_key,
                ).update(is_finalized=is_finalized)

                # Optional reward distribution
                if distribute_rewards_toggle:
                    entries = LeaderboardEntry.objects.filter(
                        period=period,
                        period_key=period_key,
                    )
                    distribute_rewards(period, period_key, entries)

                qs = LeaderboardEntry.objects.filter(
                    period=period,
                    period_key=period_key,
                )

                updated = qs.update(is_finalized=is_finalized)

                messages.success(
                    request,
                    f"{updated} entries updated for {period} ({period_key})",
                )


                messages.success(
                    request,
                    f"Settings applied for {period} ({period_key})",
                )
                return redirect(request.path)

        else:
            form = LeaderboardControlForm()

        context = dict(
            self.admin_site.each_context(request),
            form=form,
        )
        return render(
            request,
            "admin/leaderboard_control_panel.html",
            context,
        )


class LevelConfigAdminForm(forms.ModelForm):
    class Meta:
        model = LevelConfig
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()

        min_cards = cleaned.get("min_required_cards")
        max_cards = cleaned.get("max_required_cards")

        min_enemy = cleaned.get("min_enemy_level")
        max_enemy = cleaned.get("max_enemy_level")

        min_card_lvl = cleaned.get("min_card_level")
        max_card_lvl = cleaned.get("max_card_level")

        if min_cards > max_cards:
            raise ValidationError("Min required cards cannot exceed max required cards.")

        if min_enemy > max_enemy:
            raise ValidationError("Min enemy level cannot exceed max enemy level.")

        if min_card_lvl > max_card_lvl:
            raise ValidationError("Min card level cannot exceed max card level.")

        return cleaned

@admin.register(LevelConfig)
class LevelConfigAdmin(admin.ModelAdmin):
    form = LevelConfigAdminForm

    list_display = (
        "level",
        "value",
        "card_range",
        "enemy_range",
        "max_distinct_card_levels",
        "updated_at",
    )

    ordering = ("level",)

    readonly_fields = ("updated_at",)

    fieldsets = (
        (
            "Level Identity",
            {
                "fields": ("level_id","name", "image", "level","value",),
                "description": "Defines which mission level this configuration applies to (1–5).",
            },
        ),
        (
            "Card Requirements",
            {
                "fields": (
                    "min_required_cards",
                    "max_required_cards",
                    "min_card_level",
                    "max_card_level",
                    "max_distinct_card_levels",
                ),
                "description": "Controls how many cards are required and how varied they can be.",
            },
        ),
        (
            "Enemy Difficulty",
            {
                "fields": (
                    "min_enemy_level",
                    "max_enemy_level",
                ),
                "description": "Defines the difficulty range of enemies for this level.",
            },
        ),
        (
            "System",
            {
                "fields": ("updated_at",),
            },
        ),
    )

    def card_range(self, obj):
        return f"{obj.min_required_cards}–{obj.max_required_cards}"

    card_range.short_description = "Required Cards"

    def enemy_range(self, obj):
        return f"{obj.min_enemy_level}–{obj.max_enemy_level}"

    enemy_range.short_description = "Enemy Level Range"

@admin.register(RunnerEntry)
class RunnerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "price",
        "version",
        "is_active",
        "updated_at",
    )

    list_filter = (
        "is_active",
        "updated_at",
        "version",
    )

    search_fields = (
        "name",
        "description",
    )

    ordering = (
        "-updated_at",
    )

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )

@admin.register(WorldEntry)
class WorldEntryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "version",
        "is_active",
        "updated_at",
    )

    list_filter = (
        "is_active",
        "updated_at",
        "version",
    )

    search_fields = (
        "name",
        "description",
    )

    ordering = (
        "-updated_at",
    )

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )

# ---------- MissionAdmin ----------
@admin.register(Mission)
class MissionAdmin(admin.ModelAdmin):
    list_display = (
        "mission_id",
        "title",
        "progress_mode",
        "time_scope",
        "resolution_mode",
        "status",
        "is_active",
        "updated_at",
    )

    list_filter = (
        "is_active",
        "progress_mode",
        "time_scope",
        "resolution_mode",
        "status",
    )

    search_fields = ("mission_id", "title")

    readonly_fields = (
        "created_at",
        "updated_at",
        "resolved_at",
    )

    ordering = ("mission_id",)

    fieldsets = (
        (
            "Mission Identity",
            {
                "fields": (
                    "mission_type",
                    "mission_id",
                    "title",
                    "is_active",
                    "status",
                )
            },
        ),
        (
            "Mission Axes",
            {
                "fields": (
                    "level",
                    "progress_mode",
                    "time_scope",
                    "resolution_mode",
                )
            },
        ),
        (
            "Time Window",
            {
                "fields": (
                    "start_time",
                    "end_time",
                    "resolved_at",
                )
            },
        ),
        (
            "Progress Definition",
            {
                "fields": (
                    "progress_definition",
                    "target_value",
                ),
                "description": (
                    "Defines how progress is earned. "
                    "Target value is only required for TARGETED missions."
                ),
            },
        ),
        (
            "Rewards",
            {
                "fields": (
                    "rewards",
                )
            },
        ),
        (
            "Presentation",
            {
                "fields": (
                    "image",
                    "banner",
                )
            },
        ),
        (
            "Server Metadata",
            {
                "fields": (
                    "hash_code",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    # -----------------------------
    # Custom admin views
    # -----------------------------
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "generate/",
                self.admin_site.admin_view(self.generate_mission_view),
                name="mission-generate",
            ),
            path(
                "generate/confirm/",
                self.admin_site.admin_view(self.confirm_mission_view),
                name="mission-confirm",
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}

        if self.has_add_permission(request):
            extra_context["generate_mission_url"] = "generate/"

        return super().changelist_view(request, extra_context)

    # -----------------------------
    # Mission Generator (Preview)
    # -----------------------------
    def generate_mission_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        if request.method == "POST":
            form = MissionGeneratorForm(request.POST, request.FILES)
            if form.is_valid():
                uploaded_image = request.FILES.get("image")

                preview = build_mission_preview(
                    level=form.cleaned_data["level"],
                    title=form.cleaned_data["title"],
                    image=uploaded_image,
                )

                # Store minimal preview data (no files)
                request.session["mission_preview"] = {
                    "mission_id": preview["mission_id"],
                    "level_id": preview["level_id"],
                    "title": preview["title"],
                    "progress_mode": preview["progress_mode"],
                    "time_scope": preview["time_scope"],
                    "resolution_mode": preview["resolution_mode"],
                    "start_time": int(preview["start_time"].timestamp()),
                    "end_time": (
                        int(preview["end_time"].timestamp())
                        if preview.get("end_time")
                        else None
                    ),
                    "progress_definition": preview["progress_definition"],
                    "target_value": preview.get("target_value"),
                    "rewards": preview["rewards"],
                    "status": preview["status"],
                    "is_active": preview["is_active"],
                    "hash_code": preview["hash_code"],
                }

                context = {
                    **self.admin_site.each_context(request),
                    "preview": preview,
                    "title": "Preview Mission",
                }
                return render(request, "admin/mission_preview.html", context)
        else:
            form = MissionGeneratorForm()

        return render(
            request,
            "admin/generate_mission.html",
            {
                **self.admin_site.each_context(request),
                "form": form,
                "title": "Generate Mission",
            },
        )

    # -----------------------------
    # Mission Generator (Confirm)
    # -----------------------------
    def confirm_mission_view(self, request):
        if request.method != "POST":
            return redirect("..")

        preview_data = request.session.get("mission_preview")
        image = request.FILES.get("image")

        if not preview_data or not image:
            messages.error(
                request,
                "Mission preview expired or no image uploaded. Please generate again."
            )
            return redirect("..")

        preview_data["image"] = image

        mission = save_mission_from_preview(preview_data)

        del request.session["mission_preview"]

        bump_validation_code("MISSION_DATA")

        messages.success(
            request,
            f"Mission '{mission.title}' saved and pushed to Firestore."
        )

        return redirect("..")

# ---------- ValidationEntryAdmin ----------
@admin.register(ValidationEntry)
class ValidationEntryAdmin(admin.ModelAdmin):
    list_display = ("identifier", "validation_code")
    list_filter = ("identifier", "validation_code")
    ordering = ("identifier", "validation_code")

@admin.register(EventEntry)
class EventEntryAdmin(admin.ModelAdmin):
    list_display = ("title", "hash_code")
    list_filter = ("created_at", "hash_code")

@admin.register(NotificationEntry)
class NotificationEntryAdmin(admin.ModelAdmin):
    list_display = ("title", "hash_code")
    list_filter = ("created_at", "hash_code")

@admin.register(NewsEntry)
class NewsEntryAdmin(admin.ModelAdmin):
    list_display = ("headline", "brief", "content")
    list_filter = ("created_at", "article_type")

@admin.register(NewsManifestState)
class NewsManifestState(admin.ModelAdmin):
    list_display = ("version", "updated_at")
    list_filter = ("updated_at", "version")

@admin.register(DataManifestStateEntry)
class DataManifestStateAdmin(admin.ModelAdmin):
    list_display = ("version", "data_type", "updated_at")
    list_filter = ("updated_at", "version")

@admin.register(PackageEntry)
class PackageEntryAdmin(admin.ModelAdmin):
    list_display = ("title", "updated_at")
    list_filter = ("created_at", "updated_at")
