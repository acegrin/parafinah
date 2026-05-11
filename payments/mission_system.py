import uuid
import json
import hashlib
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone
from django.http import JsonResponse, Http404

from payments.models import Mission, LevelConfig, EventEntry, DataManifestStateEntry
from payments.services.firestore_missions import push_mission_to_firestore


# -------------------------------------------------
# Hashing
# -------------------------------------------------

def generate_hash(payload: dict) -> int:
    """
    Deterministic hash of mission-defining data.
    Ordering must be stable.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (10**18)


# -------------------------------------------------
# Mission specification (pure logic)
# -------------------------------------------------

import random

def generate_mission_spec(level: LevelConfig) -> dict:
    config = LevelConfig.objects.get(level=level.level)

    # Total cards required
    total_cards = random.randint(
        config.min_required_cards,
        config.max_required_cards
    )

    # Determine how many distinct card levels are involved
    available_levels = (
        config.max_card_level - config.min_card_level + 1
    )

    max_distinct = min(
        config.max_distinct_card_levels,
        available_levels
    )

    distinct_levels = random.randint(1, max_distinct)

    card_levels = random.sample(
        range(config.min_card_level, config.max_card_level + 1),
        distinct_levels
    )

    # Distribute required cards across levels
    remaining = total_cards
    requirements = {}

    for i, lvl in enumerate(card_levels):
        if i == len(card_levels) - 1:
            count = remaining
        else:
            count = random.randint(
                1,
                remaining - (len(card_levels) - i - 1)
            )

        remaining -= count
        requirements[str(lvl)] = count

    progress_definition = {
        "style": random.choice(["defeat", "rescue"]),
        "id": random.choice(["Anko", "Big Billy", "Peter Pata"]),
    }

    rewards = {
        "type": "fixed",
        "items": [
            {
                "kind": "gold",
                "quantity": 20 * level.level,
            }
        ],
    }

    return {
        "progress_mode": "TARGETED",
        "time_scope": "DAILY",
        "resolution_mode": "THRESHOLD",
        "progress_definition": progress_definition,
        "target_value": total_cards,
        "rewards": rewards,
    }



# -------------------------------------------------
# Preview builder (admin-safe)
# -------------------------------------------------

def build_mission_preview(*, level: LevelConfig, title: str, image):
    """
    Builds a preview that exactly mirrors Mission fields.
    """
    spec = generate_mission_spec(level)

    mission_id = f"MS_{uuid.uuid4().hex[:6].upper()}"

    start_time = timezone.now()
    end_time = start_time + timedelta(days=1)

    canonical_payload = {
        "mission_id": mission_id,
        "level_id": level.id,
        "title": title,
        "progress_mode": spec["progress_mode"],
        "time_scope": spec["time_scope"],
        "resolution_mode": spec["resolution_mode"],
        "start_time": int(start_time.timestamp()),
        "end_time": int(end_time.timestamp()),
        "progress_definition": spec["progress_definition"],
        "target_value": spec["target_value"],
        "rewards": spec["rewards"],
    }

    hash_code = generate_hash(canonical_payload)

    return {
        "mission_id": mission_id,
        "level_id": level.id,
        "title": title,
        "image": image,

        "progress_mode": spec["progress_mode"],
        "time_scope": spec["time_scope"],
        "resolution_mode": spec["resolution_mode"],

        "start_time": start_time,
        "end_time": end_time,

        "progress_definition": spec["progress_definition"],
        "target_value": spec["target_value"],
        "rewards": spec["rewards"],

        "status": "ACTIVE",
        "is_active": True,
        "hash_code": hash_code,
    }


# -------------------------------------------------
# Persistence
# -------------------------------------------------
def _ensure_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return timezone.make_aware(datetime.fromtimestamp(value))
    if isinstance(value, str):
        return timezone.make_aware(datetime.fromisoformat(value))
    raise ValueError(f"Invalid datetime value: {value!r}")

def save_mission_from_preview(preview: dict) -> Mission:
    mission = Mission.objects.create(
        mission_id=preview["mission_id"],
        level=LevelConfig.objects.get(id=preview["level_id"]),
        title=preview["title"],
        image=preview["image"],

        progress_mode=preview["progress_mode"],
        time_scope=preview["time_scope"],
        resolution_mode=preview["resolution_mode"],

        start_time=_ensure_datetime(preview["start_time"]),
        end_time=_ensure_datetime(preview["end_time"]),

        progress_definition=preview["progress_definition"],
        target_value=preview["target_value"],
        rewards=preview["rewards"],

        status=preview["status"],
        is_active=preview["is_active"],
        hash_code=preview["hash_code"],
    )

    mission.banner = f"{settings.SITE_URL}{mission.image.url}"
    mission.save(update_fields=["banner"])

    # push_mission_to_firestore(mission)
    return mission



# -------------------------------------------------
# Public export
# -------------------------------------------------

def resolve_mission_data():
    """
    API-safe export.
    """
    return [m.to_dict() for m in Mission.objects.filter(is_active=True)]

def resolve_event_data():
    """
        API-safe export.
        """
    return [m.to_dict() for m in EventEntry.objects.filter(is_active=True)]

def resolve_objectives_data():
    """
            API-safe export.
            """
    return [m.to_dict() for m in LevelConfig.objects.all()]

def get_mission_manifest(current_manifest_version):
    state, created = DataManifestStateEntry.objects.get_or_create(
        data_type="mission",
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        return {
            "new_data_available": False,
        }


    # If the state was just created, you can optionally
    # perform any one-time initialization logic here
    if created:
        # Example: log, seed data, etc.
        pass

    articles = Mission.objects.values(
        "mission_id",
        "created_at"
    ).filter(mission_type="STANDARD")

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["mission_id"]),
                "updated_at": int(a["created_at"].timestamp())
            }
            for a in articles
        ]
    }

def get_mission_article(article_uid):
    try:
        article = Mission.objects.get(mission_id=article_uid)
    except Mission.DoesNotExist:
        raise Http404("Article not found")

    return JsonResponse(article.to_dict())