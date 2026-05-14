import json
import os
import uuid
from random import choices
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.conf import settings
from django.core.files import File
from urllib.parse import urlparse
import hashlib

from core.storage.github import GitHubStorage

PAYLOAD_TYPE_CHOICES = [
        ("STANDARD", "Standard"),
        ("NEW_RUNNER", "New Runner"),
        ("RANK_COMPETITION", "Rank Competition")
    ]
class LeaderboardEntry(models.Model):
    PERIOD_DAILY = "daily"
    PERIOD_WEEKLY = "weekly"
    PERIOD_MONTHLY = "monthly"

    PERIOD_CHOICES = [
        (PERIOD_DAILY, "Daily"),
        (PERIOD_WEEKLY, "Weekly"),
        (PERIOD_MONTHLY, "Monthly"),
    ]

    user_id = models.CharField(max_length=64, db_index=True)
    player_name = models.CharField(max_length=64)

    score = models.IntegerField(default=0)
    baseline_score = models.IntegerField(default=0)
    rank = models.IntegerField(db_index=True)

    period = models.CharField(
        max_length=10,
        choices=PERIOD_CHOICES,
        db_index=True,
    )

    period_key = models.CharField(
        max_length=16,
        db_index=True,
        help_text="Example: 2025-12-16, 2025-W51, 2025-12"
    )

    updated_at = models.DateTimeField(auto_now=True)

    is_finalized = models.BooleanField(default=False)

    class Meta:
        unique_together = ("user_id", "period", "period_key")
        ordering = ["rank"]

    def __str__(self):
        return f"{self.period}:{self.period_key} #{self.rank} {self.player_name} ({self.score})"

class LeaderboardPeriodResult(models.Model):
    PERIOD_CHOICES = (
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
    )

    period = models.CharField(max_length=10, choices=PERIOD_CHOICES)
    period_key = models.CharField(max_length=16)

    user_id = models.CharField(max_length=128)
    player_name = models.CharField(max_length=64)

    rank = models.PositiveIntegerField()
    score = models.PositiveIntegerField()

    finalized_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("period", "period_key", "rank")
        indexes = [
            models.Index(fields=["period", "period_key"]),
            models.Index(fields=["user_id"]),
        ]

    def __str__(self):
        return f"{self.period} {self.period_key} #{self.rank}"

class LeaderboardReward(models.Model):
    PERIOD_CHOICES = [
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
    ]

    period = models.CharField(
        max_length=10,
        choices=PERIOD_CHOICES,
        db_index=True,
    )
    rank = models.PositiveIntegerField()

    reward_type = models.CharField(max_length=32)
    reward_payload = models.JSONField()

    class Meta:
        unique_together = ("period", "rank")

class LeaderboardPeriodControl(models.Model):
    PERIOD_CHOICES = [
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
    ]

    period = models.CharField(max_length=10, choices=PERIOD_CHOICES)
    period_key = models.CharField(max_length=16, db_index=True)

    is_locked = models.BooleanField(default=False)
    is_finalized = models.BooleanField(default=False)
    rewards_distributed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("period", "period_key")
        ordering = ["-period", "-period_key"]

    def __str__(self):
        return f"{self.period} {self.period_key}"

class ImageUploadPath:
    def __init__(self, folder_name):
        self.folder_name = folder_name

    def __call__(self, instance, filename):
        ext = os.path.splitext(filename)[1]
        code = uuid.uuid4().hex
        return f"{self.folder_name}/{code}{ext}"

    def deconstruct(self):
        """
        Required for Django migrations.
        Returns enough information to re-create this object.
        """
        return (
            "payments.models.ImageUploadPath",  # import path
            [self.folder_name],                 # positional args
            {}                                  # keyword args
        )

class OverwriteStorage(GitHubStorage):
    def get_available_name(self, name, max_length=None):
        if self.exists(name):
            self.delete(name)
        return name

    def deconstruct(self):
        """
        This tells Django how to serialize this storage for migrations.
        """
        name = self.__class__.__module__ + "." + self.__class__.__qualname__
        args = []
        kwargs = {}  # include custom kwargs if you have any
        return (name, args, kwargs)



def default_level_config():
    return LevelConfig.objects.order_by("level").values_list("id", flat=True).first()

class LevelConfig(models.Model):
    LEVEL_CHOICES = [(i, f"Level{i}") for i in range(1, 6)]

    level_id = models.UUIDField(default=uuid.uuid4, editable=True)

    name = models.CharField(max_length=32, blank=True)

    level = models.PositiveSmallIntegerField(
        choices=LEVEL_CHOICES,
        help_text="Player / mission level (1–5)",
        unique=True
    )

    value = models.PositiveSmallIntegerField()

    image = models.ImageField(
        upload_to="objectives/icons/",
        storage=OverwriteStorage(),
        help_text="Event icon / GIF",
        null=True,
        blank=True
    )

    # Card requirement range
    min_required_cards = models.PositiveSmallIntegerField()
    max_required_cards = models.PositiveSmallIntegerField()

    # Allowed enemy difficulty range
    min_enemy_level = models.PositiveSmallIntegerField()
    max_enemy_level = models.PositiveSmallIntegerField()

    # Optional tuning knobs (future-proof)
    min_card_level = models.PositiveSmallIntegerField(default=1)
    max_card_level = models.PositiveSmallIntegerField(default=5)

    # Variance control
    max_distinct_card_levels = models.PositiveSmallIntegerField(
        default=2,
        help_text="How many different card levels can appear in a mission"
    )

    updated_at = models.DateTimeField(auto_now=True)

    created_at = models.DateTimeField(auto_now=True)

    # ---- Helpers ----
    @property
    def image_url_contractor(self):
        return self.image.url if self.image else ""

    def to_dict(self):
        return {
            "id": self.level_id,
            "name": self.name,
            "title": f"Level{self.level}",
            "level": self.level,
            "value": self.value,
            "icon": self.image_url_contractor,
            "created_at": int(self.created_at.timestamp()),
            "updated_at": int(self.updated_at.timestamp())
        }

    class Meta:
        ordering = ["level"]

    def __str__(self):
        return f"Level {self.level}"

class ValidationEntry(models.Model):
    identifier = models.CharField(
        max_length=32,
        unique=True,
        db_index=True,
        help_text="Validation identifier, e.g. user_id/all_missions"
    )

    validation_code = models.CharField(max_length=128)

    class Meta:
        ordering = ["identifier"]

class EventEntry(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    payload_structure = models.CharField(
        max_length=20,
        choices=PAYLOAD_TYPE_CHOICES,
        default="STANDARD",
        null=True,
        blank=True
    )
    title = models.CharField(max_length=128)
    description = models.CharField(max_length=128)

    headline = models.CharField(max_length=128)
    brief = models.CharField(max_length=128, null=True, blank=True)
    content = models.TextField(null=True, blank=True)

    # ---- Time Window ----
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    # ---- Rewards ----
    rewards = models.JSONField(
        help_text="Reward definition (fixed or ranked)"
    )

    banner = models.ImageField(
        upload_to="events/banners/",
        storage=OverwriteStorage(),
        help_text="Event banner / GIF",
        null=True,
        blank=True
    )

    # ---- Presentation ----
    image = models.ImageField(
        upload_to="events/icons/",
        storage=OverwriteStorage(),
        help_text="Event icon / GIF",
        null=True,
        blank=True
    )

    # ---- State ----
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("RESOLVING", "Resolving"),
        ("RESOLVED", "Resolved"),
    ]

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="ACTIVE"
    )

    is_active = models.BooleanField(default=True)

    # ---- Integrity ----
    hash_code = models.BigIntegerField(db_index=True)

    # ---- Audit ----
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ---- Helpers ----
    @property
    def banner_url(self):
        return self.image.url if self.image else ""

    def to_dict(self):
        return {
            "id": str(self.event_id),
            "payload_structure": self.payload_structure,
            "title": self.title,
            "description": self.description,  # ✅ fixed
            "banner": self.banner_url,
            "start_time": int(self.start_time.timestamp()),
            "end_time": int(self.end_time.timestamp()),
            "created_at": int(self.created_at.timestamp()),
            "updated_at": int(self.updated_at.timestamp()),
            "rewards": json.dumps(self.rewards),
            "status": self.status,
            "seen": False  # ✅ explicit default
        }

    class Meta:
        ordering = ["created_at"]

class Mission(models.Model):
    # ---- Identity ----
    MISSION_TYPE_CHOICES = [
        ("DEFAULT", "Default"),
        ("STANDARD", "Standard")
    ]

    mission_type = models.CharField(
        max_length=16,
        choices=MISSION_TYPE_CHOICES,
        default="STANDARD"
    )

    mission_id = models.CharField(
        max_length=32,
        unique=True,
        db_index=True,
        help_text="Public mission identifier, e.g. XP0"
    )

    title = models.CharField(max_length=128)

    # ---- Presentation ----
    image = models.ImageField(
        upload_to="missions/banners/",
        storage=OverwriteStorage(),
        help_text="Mission icon / GIF"
    )

    banner = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text="Resolved banner URL (derived from image)"
    )

    # ---- Mission Axes ----

    level = models.ForeignKey(
        LevelConfig,
        on_delete=models.PROTECT
    )

    PROGRESS_MODE_CHOICES = [
        ("TARGETED", "Targeted"),       # Has a fixed completion target
        ("ACCUMULATIVE", "Accumulative") # Grows until expiry
    ]

    progress_mode = models.CharField(
        max_length=16,
        choices=PROGRESS_MODE_CHOICES
    )

    TIME_SCOPE_CHOICES = [
        ("PERMANENT", "Permanent"),
        ("DAILY", "Daily"),
        ("WEEKLY", "Weekly"),
        ("MONTHLY", "Monthly"),
    ]

    time_scope = models.CharField(
        max_length=16,
        choices=TIME_SCOPE_CHOICES,
        default="PERMANENT"
    )

    RESOLUTION_MODE_CHOICES = [
        ("THRESHOLD", "Threshold"),     # Complete when target reached
        ("COMPETITIVE", "Competitive")  # Resolve at expiry (leaderboard)
    ]

    resolution_mode = models.CharField(
        max_length=16,
        choices=RESOLUTION_MODE_CHOICES
    )

    # ---- Time Window ----
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    # ---- Progress Definition ----
    progress_definition = models.JSONField(
        help_text="""
        Defines how progress is earned.
        Examples:
        - { "type": "card", "vehicle_types": ["TANK", "DRONE"] }
        - { "type": "points", "per_kill": 10 }
        """
    )

    target_value = models.IntegerField(
        null=True,
        blank=True,
        help_text="Only used for TARGETED missions"
    )

    # ---- Rewards ----
    rewards = models.JSONField(
        help_text="Reward definition (fixed or ranked)"
    )

    # ---- State ----
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("RESOLVING", "Resolving"),
        ("RESOLVED", "Resolved"),
    ]

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="ACTIVE"
    )

    is_active = models.BooleanField(default=True)

    # ---- Integrity ----
    hash_code = models.BigIntegerField(db_index=True, null=True, blank=True)

    # ---- Audit ----
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ---- Helpers ----
    @property
    def banner_url(self):
        return self.image.url if self.image else ""

    def to_dict(self):
        return {
            "mission_id": self.mission_id,
            "level": f"Level{self.level.level}",
            "title": self.title,
            "progress_mode": self.progress_mode,
            "time_scope": self.time_scope,
            "resolution_mode": self.resolution_mode,
            "banner": self.banner_url,
            "start_time": int(self.start_time.timestamp()),
            "end_time": int(self.end_time.timestamp()),
            "progress_definition": self.progress_definition,
            "target_value": self.target_value,
            "rewards": self.rewards,
            "status": self.status,
            "hash_code": self.hash_code,
        }

    class Meta:
        ordering = ["created_at"]

class NotificationEntry(models.Model):
    article_uid = models.UUIDField(default=uuid.uuid4, null=True, blank=True)
    user_id = models.CharField(max_length=128)
    title = models.CharField(max_length=128)
    description = models.CharField(max_length=128)

    image = models.ImageField(
        upload_to="notifications/icons/",
        storage=OverwriteStorage(),
        null=True,
        blank=True
    )

    seen = models.BooleanField(default=False)
    hash_code = models.BigIntegerField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def banner_url(self):
        return self.image.url if self.image else ""

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "banner": self.banner_url,
            "seen": self.seen,
            "created_at": int(self.created_at.timestamp()),
        }

    @classmethod
    def create_from_image_url(cls, user_id, title, description, image_url):
        """
        image_url must point to MEDIA_URL on this server
        """

        image_file = None

        if image_url:
            parsed = urlparse(image_url)

            # Convert URL → local filesystem path
            relative_path = parsed.path.replace(settings.MEDIA_URL, "")
            absolute_path = os.path.join(settings.MEDIA_ROOT, relative_path)

            if not os.path.exists(absolute_path):
                raise FileNotFoundError(f"Image not found: {absolute_path}")

            image_file = File(open(absolute_path, "rb"))

        # Deterministic hash (prevents duplicates if you want)
        hash_code = int(
            hashlib.sha256(
                f"{user_id}:{title}:{description}".encode()
            ).hexdigest(),
            16
        ) % (10 ** 18)

        notification = cls(
            user_id=user_id,
            title=title,
            description=description,
            hash_code=hash_code,
        )

        if image_file:
            filename = os.path.basename(absolute_path)
            notification.image.save(filename, image_file, save=False)

        notification.save()
        return notification

    def delete_with_file(self, using=None, keep_parents=False):
        """
        Deletes the notification and its associated image file from storage.
        """

        if self.image:
            try:
                storage = self.image.storage
                if storage.exists(self.image.name):
                    storage.delete(self.image.name)
            except Exception:
                # Intentionally swallow errors to avoid partial deletes
                pass

        super().delete(using=using, keep_parents=keep_parents)

    class Meta:
        ordering = ["created_at"]

class NewsManifestState(models.Model):
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    def bump(self):
        self.version += 1
        self.save(update_fields=["version", "updated_at"])

class NewsEntry(models.Model):
    article_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    article_type = models.CharField(
        max_length=20,
        choices=PAYLOAD_TYPE_CHOICES,
        default="STANDARD"
    )
    headline = models.CharField(max_length=128)
    brief = models.CharField(max_length=128)
    content = models.CharField(max_length=512)

    context = models.JSONField(default=dict, blank=True)

    banner = models.ImageField(
        upload_to="news/banners/",
        storage=OverwriteStorage(),
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # IMPORTANT

    @property
    def banner_url(self):
        return self.banner.url if self.banner else ""

    def to_dict(self):
        return {
            "article_type": self.article_type,
            "id": self.article_uid,
            "headline": self.headline,
            "brief": self.brief,
            "content": self.content,
            "context": json.dumps(self.context),
            "banner": self.banner_url,
            "created_at": int(self.created_at.timestamp()),
        }

    def delete_with_file(self, using=None, keep_parents=False):
        """
        Deletes the notification and its associated image file from storage.
        """

        if self.banner:
            try:
                storage = self.banner.storage
                if storage.exists(self.banner.name):
                    storage.delete(self.banner.name)
            except Exception:
                # Intentionally swallow errors to avoid partial deletes
                pass

        super().delete(using=using, keep_parents=keep_parents)

    class Meta:
        ordering = ["created_at"]

class DataManifestStateEntry(models.Model):
    data_type = models.CharField(max_length=128)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)


    def bump(self):
        self.version += 1
        self.save(update_fields=["version", "updated_at"])

class RunnerEntry(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    name = models.CharField(max_length=128, unique=True)
    price = models.PositiveIntegerField(default=0)
    description = models.TextField()

    version = models.PositiveIntegerField(default=1)

    power = models.FloatField(default=1.0)
    speed = models.FloatField(default=1.0)
    stamina = models.FloatField(default=1.0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    is_active = models.BooleanField(default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "icon_url": f"https://acegrin.github.io/Parafinah-Assets/Android/{self.id}.png",
            "bundle_url": f"https://acegrin.github.io/Parafinah-Assets/Android/{self.id}",
            "price": self.price,
            "description": self.description,
            "power": self.power,
            "speed": self.speed,
            "stamina": self.stamina,
            "version": self.version,
            "created_at": int(self.created_at.timestamp()),
            "updated_at": int(self.updated_at.timestamp()),
            "is_active": self.is_active,
        }

    def bump(self):
        self.version += 1
        self.save(update_fields=["version", "updated_at"])

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["created_at", "updated_at"]

class PackageEntry(models.Model):
    package_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    PACKAGE_TYPE_CHOICES = [
        ("MISSION", "Mission"),
        ("FREE", "Free"),
        ("AD", "Ad"),
        ("PURCHASE", "Purchase"),
        ("BUNDLE", "Bundle")
    ]

    type = models.CharField(
        max_length=16,
        choices=PACKAGE_TYPE_CHOICES,
        default="PURCHASE"
    )

    title = models.CharField(max_length=128, unique=True)
    price = models.FloatField(default=0.0)
    quantity = models.PositiveIntegerField(default=0)
    description = models.TextField()

    rewards = models.JSONField(default=list)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def to_dict(self):
        return {
            "id": str(self.package_id),
            "title": self.title,
            "price": self.price,
            "quantity": self.quantity,
            "description": self.description,
            "type": self.type,
            "rewards": json.dumps(self.rewards),
            "created_at": int(self.created_at.timestamp()),
            "updated_at": int(self.updated_at.timestamp()),
            "is_active": self.is_active,
        }

    def __str__(self):
        return self.title

    class Meta:
        ordering = ["price", "title"]
