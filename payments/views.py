# payments/views.py
import json, os, base64, logging, requests, time
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from google.cloud import firestore
from google.api_core.exceptions import GoogleAPIError
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .events_system import get_events_manifest, get_events_article
from .firebase_client import db, firebase_auth
from rest_framework import status
from firebase_admin import firestore,credentials
from datetime import datetime
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding, ec, rsa
from decimal import Decimal, ROUND_HALF_UP
from datetime import timezone, datetime
import dateutil.parser  # pip install python-dateutil
from dateutil.parser import parse
from payments.models import LeaderboardEntry, LevelConfig, NotificationEntry, RunnerEntry, PackageEntry
from .mission_system import get_mission_article, get_mission_manifest
from .models import (
    Mission,
)
from .news_system import get_news_manifest, get_news_article
from .notification_system import get_notification_manifest, get_notification_article
from .objective_system import get_objectives_manifest, get_objectives_article
from .package_system import get_package_manifest, get_package_article
from .runner_system import get_runner_manifest, get_runner_article

firestore_client = firestore.client()

# Set up logging
logger = logging.getLogger("admob_ssv")
logger.setLevel(logging.DEBUG)

LOCAL_JWKS_FILE = os.path.join(os.path.dirname(__file__), "data", "verifier-keys.json")


def decode_base64_urlsafe(sig: str) -> bytes:
    # Add padding if missing
    sig += '=' * (-len(sig) % 4)
    return base64.urlsafe_b64decode(sig)

def compute_runner_configuration(level: int, base_config: dict) -> dict:
    return {
        "speed": to_2dp(leveled_value(level, base_config.get("speed", 0), 90)),
        "power": to_2dp(leveled_value(level, base_config.get("power", 0), 80)),
        "stamina": to_2dp(leveled_value(level, base_config.get("stamina", 0), 90))
    }

DEFAULT_RUNNER_ID = 'e2e1143b-1672-47c3-a529-6b26ebc6294c'

@api_view(["POST"])
def mark_notification_read(request):
    user_id = request.data.get("user_id")
    notification_id = request.data.get("notification_id")

    if not user_id or not notification_id:
        return Response({"error": "Invalid payload"}, status=400)

    notification = get_object_or_404(
        NotificationEntry,
        id=notification_id,
        user_id=user_id
    )

    if not notification.seen:
        notification.seen = True
        notification.save(update_fields=["seen"])

    return Response({"success": True})

@api_view(["POST"])
def initialize_player_data(request):
    user_id = request.data.get("userId")

    if not user_id:
        return Response({"error": "Missing userId"}, status=400)

    user_ref = db.collection("playerData").document(user_id)
    user_doc = user_ref.get()

    now_unix = int(datetime.now(timezone.utc).timestamp())

    if not user_doc.exists:
        # 🔹 Fetch runner definition from LOCAL DB (not Firebase)
        try:
            runner_entry = RunnerEntry.objects.get(id=DEFAULT_RUNNER_ID)
        except RunnerEntry.DoesNotExist:
            return Response(
                {"error": "Default runner definition missing"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        base_config = { "speed":runner_entry.speed, "stamina":runner_entry.stamina, "power":runner_entry.power }

        # 🔹 Level 1 config using SAME logic as upgrades
        level = 1
        computed_config = compute_runner_configuration(level, base_config)

        default_runner = {
            "name": DEFAULT_RUNNER_ID,
            "level": level,
            "configuration": computed_config
        }

        # 🔹 Create Firebase user document
        user_ref.set({
            "playerName": user_id[:10],
            "runnerName": DEFAULT_RUNNER_ID,
            "level": 0,
            "highScore": 0,
            "legendScore": 0,
            "gold": 0,
            "maxHealth": 3,
            "missions": [],
            "rewards": [],
            "runners": [default_runner],
            "createdAt": now_unix,
            "lastLogin": now_unix,
            "updatedAt": now_unix
        })

    else:
        # 🔹 Existing Firebase user → update timestamps only
        user_ref.update({
            "lastLogin": now_unix,
            "updatedAt": now_unix
        })

    return Response({"status": "ok"})

def sanitize_value(value):
    """
    Recursively sanitize Firestore data:
    - Nested timestamp maps -> unix seconds
    - Empty maps pretending to be timestamps -> unix seconds (now)
    """
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]

    if isinstance(value, dict):
        # Firestore timestamp map
        if "_seconds" in value and "_nanoseconds" in value:
            return int(value["_seconds"])

        # Empty map (corrupted timestamp)
        if len(value) == 0:
            return int(datetime.now(timezone.utc).timestamp())

        clean = {}
        for k, v in value.items():
            clean[k] = sanitize_value(v)
        return clean

    return value

@api_view(["POST"])
def economy_sync(request):
    user_id = request.data.get("userId")
    events = request.data.get("events")
    client_version = request.data.get("clientVersion")
    device_id = request.data.get("deviceId")

    if not user_id or not events:
        return Response(
            {"error": "Missing userId or events"},
            status=status.HTTP_400_BAD_REQUEST
        )

    user_ref = db.collection("playerData").document(user_id)
    fraud_ref = db.collection("fraud_logs").document(user_id)

    user_doc = user_ref.get()
    if not user_doc.exists:
        return Response({"error": "User not found"}, status=404)

    user_data = user_doc.to_dict()

    # ---- Cached user state ----
    current_gold = user_data.get("gold", 0)
    current_legend = user_data.get("legendScore", 0)
    registered_device_id = user_data.get("deviceId")
    player_name = user_data.get("playerName", "Unknown")

    missions = user_data.get("missions", [])
    rewards = user_data.get("rewards", [])

    # ---- Fast lookup maps ----
    mission_map = {m["missionId"]: m for m in missions}

    applied_gold = 0
    applied_legend = 0
    applied_cards = 0
    missions_dirty = False
    rewards_dirty = False

    suspicious_events = []

    batch = db.batch()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    MAX_TIME_DRIFT_MS = 24 * 60 * 60 * 1000

    def log_fraud(evt, category, reason, details=None, severity="medium"):
        suspicious_events.append({
            "eventId": evt.get("eventId"),
            "category": category,
            "reason": reason,
            "details": details or {},
            "severity": severity,
            "clientVersion": client_version,
            "deviceId": device_id,
            "detectedAtUtc": now_ts
        })

    for evt in events:
        event_id = evt.get("eventId")
        if not event_id:
            continue

        delta = evt.get("delta", 0)
        collected_type = evt.get("collectedType")
        timestamp_utc = evt.get("timestampUtc")
        context = evt.get("context") or ""
        source_id = evt.get("sourceId") or ""

        event_ref = user_ref.collection("economy_events").document(event_id)

        # ---- Duplicate detection ----
        if event_ref.get().exists:
            log_fraud(evt, "duplicate_event", "Event replay", severity="low")
            continue

        # ---- Validation ----
        if delta <= 0:
            log_fraud(evt, "invalid_delta", "Non-positive delta", {"delta": delta}, "high")
            continue

        if not timestamp_utc or abs(now_ms - timestamp_utc) > MAX_TIME_DRIFT_MS:
            log_fraud(evt, "invalid_timestamp", "Timestamp drift", severity="medium")
            continue

        if registered_device_id and device_id != registered_device_id:
            log_fraud(evt, "device_mismatch", "Device mismatch", severity="high")
            continue

        # ---- Economy handling ----
        if collected_type == "gold":
            level_info = LevelConfig.objects.get(level=context)
            applied_gold += level_info.value

        elif collected_type == "legend":
            applied_legend += delta

        elif collected_type == "card":
            mission_id = evt.get("sourceId")
            card_level = delta
            collected_at = timestamp_utc
            applied_cards += 1
            missions_dirty = True

            is_story = context.startswith("Story")

            if is_story:
                required_total = int(context.split("-")[1])
            else:
                try:
                    mission_def = Mission.objects.get(
                        mission_id=mission_id,
                        is_active=True
                    )
                except Mission.DoesNotExist:
                    log_fraud(evt, "invalid_mission", "Mission not found", severity="high")
                    continue

                required_total = mission_def.target_value

            mission = mission_map.get(mission_id)

            if not mission:
                mission = {
                    "missionId": mission_id,
                    "targetValue": required_total,
                    "collectedCards": [],
                    "completed": False,
                    "createdAt": now_ts
                }
                missions.append(mission)
                mission_map[mission_id] = mission

            if mission.get("completed"):
                pass
            else:
                cards = mission.setdefault("collectedCards", [])
                cards.append({
                    "level": f"Level{card_level}",
                    "collectedAt": collected_at
                })

                if len(cards) >= mission["targetValue"]:
                    mission["completed"] = True
                    mission["completedAt"] = now_ts
                    rewards.append({
                        "id": mission_id,
                        "claimed": False,
                        "createdAt": now_ts
                    })
                    rewards_dirty = True
            NotificationEntry.create_from_image_url(
                user_id=user_id,
                title="Card Collected",
                description=f"Card collected for {mission_id}",
                image_url="https://parafinah.pythonanywhere.com/media/unordered/NotificationIconBlackNWhite.png"
            )

        else:
            log_fraud(evt, "unknown_type", "Unknown collectedType", severity="medium")
            continue

        # ---- Persist event ----
        batch.set(event_ref, {
            "runId": evt.get("runId"),
            "type": evt.get("type"),
            "collectedType": collected_type,
            "delta": delta,
            "timestampUtc": timestamp_utc,
            "sourceId": evt.get("sourceId"),
            "context": context,
            "clientVersion": client_version,
            "deviceId": device_id,
            "serverTimestamp": now_ts
        })

    # ---- Final user update ----
    updates = {"updatedAt": now_ts}

    if applied_gold:
        updates["gold"] = current_gold + applied_gold

    if applied_legend:
        updates["legendScore"] = current_legend + applied_legend

    if missions_dirty:
        updates["missions"] = missions

    if rewards_dirty:
        updates["rewards"] = rewards

    if len(updates) > 1:
        batch.update(user_ref, updates)

    batch.commit()

    # ---- Fraud logging ----
    if suspicious_events:
        fraud_ref.set({
            "lastUpdatedUtc": now_ts,
            "events": firestore.ArrayUnion(suspicious_events)
        }, merge=True)

    # ---- Leaderboard ----
    if applied_legend:
        try:
            from leaderboards.services import update_leaderboards
            update_leaderboards(
                user_id=user_id,
                player_name=player_name,
                points_earned=applied_legend
            )
        except Exception:
            pass

    return Response({
        "message": "Economy events processed",
        "goldDeltaApplied": applied_gold,
        "legendDeltaApplied": applied_legend,
        "cardEventsApplied": applied_cards,
        "processedEvents": len(events),
        "suspiciousEvents": len(suspicious_events)
    })

@api_view(["POST"])
def player_sync(request):
    user_id = request.data.get("userId")
    data = request.data.get("data")

    if not user_id or not data:
        return Response(
            {"error": "Missing userId or data"},
            status=status.HTTP_400_BAD_REQUEST
        )

    user_ref = db.collection("playerData").document(user_id)
    user_doc = user_ref.get()

    # --- STRIP CLIENT TIMESTAMPS COMPLETELY ---
    for field in ("runners", "maxHealth","createdAt", "lastLogin", "schemaVersion", "Id", "UpdatedAt", "updatedAt", "lastUpdated", "rewards", "legendScore", "missions"):
        data.pop(field, None)

    # --- PROTECT GOLD ---
    if user_doc.exists:
        server_gold = user_doc.to_dict().get("gold")
        data.pop("gold", None)
        data["gold"] = server_gold

    # --- SERVER-CONTROLLED TIMESTAMPS ---
    data["updatedAt"] = int(datetime.now(timezone.utc).timestamp())
    data["lastLogin"] = int(datetime.now(timezone.utc).timestamp())

    if not user_doc.exists:
        data["createdAt"] = int(datetime.now(timezone.utc).timestamp())

    try:
        user_ref.set(data, merge=True)
    except Exception as e:
        return Response(
            {"error": "Failed to sync player data", "details": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


    return Response(
        {"message": "Player data synced", "user_id": user_id},
        status=status.HTTP_200_OK
    )

def load_google_public_key(key_id):
    """
    Load Google JWKS from local JSON file and return PEM public key for given key_id.
    """

    logger = logging.getLogger("admob_ssv")

    logger.info(f"Loading JWKS from local file: {LOCAL_JWKS_FILE}")

    try:
        with open(LOCAL_JWKS_FILE, "r") as f:
            jwks = json.load(f)
    except Exception as e:
        logger.exception(f"Failed to load JWKS file: {e}")
        return None

    keys_in_file = [str(k.get("keyId")) for k in jwks.get("keys", [])]
    logger.info(f"JWKS keys found: {keys_in_file}")

    for key in jwks.get("keys", []):
        if str(key.get("keyId")) == str(key_id):
            logger.info(f"Found key_id {key_id} in JWKS")
            return key.get("pem")

    logger.warning(f"Key_id {key_id} not found in JWKS")
    return None

def get_current_period_key(period: str) -> str:
    now = datetime.now(timezone.utc)

    if period == "daily":
        return now.strftime("%Y-%m-%d")

    if period == "weekly":
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    if period == "monthly":
        return now.strftime("%Y-%m")

    raise ValueError("Invalid period")

@api_view(['POST'])
def get_world_ranking(request):
    user_id = request.data.get("user_id")
    period = request.data.get("period", "daily")  # daily / weekly / monthly

    try:
        period_key = get_current_period_key(period)
    except ValueError:
        return Response({"error": "Invalid period"}, status=400)

    print(f"Period key: {period_key}, Period value: {period}")

    # --- Top 50 ---
    top_entries = (
        LeaderboardEntry.objects
        .filter(period=period, period_key=period_key)
        .order_by("rank")[:50]
    )

    all_ranks = []
    for entry in top_entries:
        all_ranks.append({
            "runner_name": "You" if entry.user_id == user_id else entry.player_name,
            "legend_score": entry.score,
            "rank": entry.rank
        })

    # --- Player position (if outside top 50) ---
    try:
        player_entry = LeaderboardEntry.objects.get(
            user_id=user_id,
            period=period,
            period_key=period_key
        )

        if player_entry.rank > 50:
            all_ranks.append({
                "runner_name": "You",
                "legend_score": player_entry.score,
                "rank": player_entry.rank
            })

    except LeaderboardEntry.DoesNotExist:
        pass

    return Response({
        "period": period,
        "period_key": period_key,
        "all_ranks": all_ranks
    })

@csrf_exempt
def admob_verify_reward(request):
    """
    Handles AdMob server-side verification (SSV) for rewarded ads.
    Logs the full operation and safely verifies signatures.
    """
    params = request.GET.dict()
    signature = params.pop("signature", None)
    key_id = params.pop("key_id", None)

    logger.debug(f"Incoming request params: {params}")
    logger.debug(f"signature: {signature}, key_id: {key_id}")

    # -------------------------------
    # 1️⃣ AdMob callback URL verification (test ping)
    # -------------------------------
    # if not signature or not key_id:
    if not signature or not key_id:
        logger.info("AdMob test ping received. Returning 200 OK for callback verification.")
        return HttpResponse(
            "Callback URL reachable — ready for SSV.",
            content_type="text/plain",
            status=200
        )

    # -------------------------------
    # 2️⃣ Real SSV: verify signature
    # -------------------------------
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    message_bytes = message.encode("utf-8")
    logger.debug(f"Message to verify: {message}")

    google_public_pem = load_google_public_key(key_id)
    if not google_public_pem:
        logger.error(f"Unknown key_id: {key_id}. Cannot verify signature.")
        return JsonResponse({"error": "Unknown key_id"}, status=400)

    try:
        public_key = serialization.load_pem_public_key(
            google_public_pem.encode("utf-8")
        )

        # Decode URL-safe Base64 signature with padding
        sig_bytes = decode_base64_urlsafe(signature)

        # Detect key type and verify accordingly
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                sig_bytes,
                message_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                sig_bytes,
                message_bytes,
                ec.ECDSA(hashes.SHA256())
            )
        else:
            raise ValueError("Unsupported key type for signature verification")

    except Exception as e:
        logger.exception(f"Signature verification failed: {e}")
        return JsonResponse({"error": "Invalid signature"}, status=403)

    # -------------------------------
    # 3️⃣ Signature valid → reward user
    # -------------------------------
    user_id = params.get("user_id")
    reward_amount = int(params.get("reward_amount", 0))
    reward_type = params.get("reward_item")  # AdMob uses reward_item, not reward_type
    logger.info(f"Rewarding user {user_id}: {reward_amount} of type {reward_type}")

    # TODO: Update your database safely here
    # Example:
    # from yourapp.models import PlayerProfile
    # player = PlayerProfile.objects.get(user_id=user_id)
    # player.coins += reward_amount
    # player.save()

    return JsonResponse({
        "success": True,
        "message": "Reward granted securely",
        "user_id": user_id,
        "reward_type": reward_type,
        "reward_amount": reward_amount
    })

@api_view(['GET'])
def get_server_date(request):
    now = datetime.utcnow().strftime("%Y-%m-%d")
    return Response({"date": now})

@api_view(['POST'])
def get_data_row(request):
    data_type = request.data.get('data_type')
    article_uid = request.data.get('article_uid')
    user_id = request.data.get('user_id')

    if not data_type:
        return Response(
            {"error": "Missing data_type"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not article_uid:
        return Response(
            {"error": "Missing article_uid"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if data_type == "news":
        return get_news_article(article_uid)
    if data_type == "notification":
        return get_notification_article(user_id, article_uid)
    if data_type == "mission":
        return get_mission_article(article_uid)
    if data_type == "runner":
        return get_runner_article(article_uid)
    if data_type == "event":
        return get_events_article(article_uid)
    if data_type == "package":
        return get_package_article(article_uid)
    if data_type == "objective":
        return get_objectives_article(article_uid)

    return Response(
        {"error": f"Unknown data_type '{data_type}'"},
        status=status.HTTP_400_BAD_REQUEST
    )

@api_view(['POST'])
def get_data_manifest(request):
    data_type = request.data.get('data_type')
    user_id = request.data.get('user_id')
    current_manifest_version = request.data.get('current_manifest_version')

    if not data_type:
        return Response(
            {"error": "data_type is required"},
            status=400
        )

    if data_type == "news":
        return Response(get_news_manifest(current_manifest_version))
    if data_type == "notification":
        return Response(get_notification_manifest(user_id, current_manifest_version))
    if data_type == "mission":
        return Response(get_mission_manifest(current_manifest_version))
    if data_type == "runner":
        return Response(get_runner_manifest(current_manifest_version))
    if data_type == "event":
        return Response(get_events_manifest(current_manifest_version))
    if data_type == "package":
        return  Response(get_package_manifest(current_manifest_version))
    if data_type == "objective":
        return Response(get_objectives_manifest(current_manifest_version))

    return Response(
        {"error": f"Unknown data_type '{data_type}'"},
        status=400
    )


@api_view(['POST'])
def collect_reward(request):
    user_id = request.data.get('user_id')
    reward_title = request.data.get('reward_title')
    reward_type = request.data.get('reward_type')
    mission_id = request.data.get('mission_id')

    if not user_id:
        return Response(
            {'error': 'Missing user_id'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # --------------------------------------------------
    # Firebase: Player data (UNCHANGED)
    # --------------------------------------------------
    user_ref = db.collection('playerData').document(user_id)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return Response(
            {'error': 'User not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    user_data = user_doc.to_dict()
    updated_fields = {}
    total_gold_added = 0

    # ==================================================
    # MISSION REWARDS (authoritative from Django)
    # ==================================================
    if reward_type == "MISSION":
        if not mission_id:
            return Response(
                {"error": "Missing mission_id for mission reward"},
                status=status.HTTP_400_BAD_REQUEST
            )

        player_rewards = user_data.get("rewards", [])
        reward_entry = next(
            (r for r in player_rewards if r.get("id") == mission_id),
            None
        )

        if not reward_entry:
            return Response(
                {"error": "Reward chest for this mission not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if reward_entry.get("claimed", False):
            for r in player_rewards:
                if r.get("id") == mission_id:
                    r["claimed"] = True
                    r["collectedAt"] = int(datetime.now(timezone.utc).timestamp())
                    break

            updated_fields["rewards"] = player_rewards
            updated_fields["updatedAt"] = int(datetime.now(timezone.utc).timestamp())

            user_ref.update(updated_fields)

            return Response(
                {"error": "COLLECTED"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            mission = Mission.objects.get(
                mission_id=mission_id,
                is_active=True
            )
        except Mission.DoesNotExist:
            return Response(
                {"error": "Mission not found or inactive"},
                status=status.HTTP_404_NOT_FOUND
            )

        rewards_map = mission.rewards or {}
        items = rewards_map.get("items", [])

        if not items:
            return Response(
                {"error": "Mission reward data missing"},
                status=status.HTTP_404_NOT_FOUND
            )

        reward_info = items[0]
        reward_kind = reward_info.get("kind")
        reward_quantity = int(reward_info.get("quantity", 0))

        if reward_kind == "gold":
            current_gold = int(user_data.get("gold", 0))
            updated_fields["gold"] = current_gold + reward_quantity
            total_gold_added = reward_quantity
        else:
            return Response(
                {"error": f"Unsupported mission reward type: {reward_kind}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        for r in player_rewards:
            if r.get("id") == mission_id:
                r["claimed"] = True
                r["collectedAt"] = int(datetime.now(timezone.utc).timestamp())
                break

        updated_fields["rewards"] = player_rewards
        updated_fields["updatedAt"] = int(datetime.now(timezone.utc).timestamp())

        user_ref.update(updated_fields)

        return Response(
            {
                "message": f'Mission reward for "{mission_id}" collected',
                "reward_info": reward_info,
                "gold_added": total_gold_added,
                "updated_fields": updated_fields
            },
            status=status.HTTP_200_OK
        )

    # ==================================================
    # STANDARD REWARD OFFERS (Django ONLY)
    # ==================================================
    if not reward_title:
        return Response(
            {'error': 'Missing reward_title'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        package = PackageEntry.objects.get(
            title=reward_title,
            is_active=True
        )
    except PackageEntry.DoesNotExist:
        return Response(
            {'error': f'Reward package "{reward_title}" not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    rewards = package.rewards or []
    package_type = package.type

    # --------------------------------------------------
    # DAILY / FREE reward (one per day)
    # --------------------------------------------------
    if package_type == "FREE":
        today_id = datetime.utcnow().strftime("%Y-%m-%d")
        player_rewards = user_data.get("rewards", [])

        already_claimed = any(
            r.get("id") == today_id for r in player_rewards
        )

        if already_claimed:
            return Response(
                {'error': 'Reward already collected today'},
                status=status.HTTP_400_BAD_REQUEST
            )

        for reward in rewards:
            if reward.get("type") == "gold":
                quantity = int(reward.get("quantity", 0))
                current_gold = int(user_data.get("gold", 0))
                updated_fields["gold"] = current_gold + quantity
                total_gold_added += quantity

        player_rewards.append({
            "id": today_id,
            "claimed": True,
            "createdAt": int(datetime.now(timezone.utc).timestamp())
        })

        updated_fields["rewards"] = player_rewards
        updated_fields["updatedAt"] = int(datetime.now(timezone.utc).timestamp())

        user_ref.update(updated_fields)

        return Response(
            {
                "message": f'Daily reward collected for {today_id}',
                "rewards_applied": rewards,
                "gold_added": total_gold_added,
                "updated_fields": updated_fields
            },
            status=status.HTTP_200_OK
        )

    # --------------------------------------------------
    # PURCHASE / AD / OTHER NON-DAILY REWARDS
    # --------------------------------------------------
    elif package_type in ["PURCHASE", "AD"]:
        for reward in rewards:
            if reward.get("type") == "gold":
                quantity = int(reward.get("quantity", 0))
                current_gold = int(user_data.get("gold", 0))
                updated_fields["gold"] = current_gold + quantity
                total_gold_added += quantity

        if updated_fields:
            updated_fields["updatedAt"] = int(datetime.now(timezone.utc).timestamp())
            user_ref.update(updated_fields)

        return Response(
            {
                "message": f'Reward "{reward_title}" collected',
                "reward_type": package_type,
                "rewards_applied": rewards,
                "gold_added": total_gold_added,
                "updated_fields": updated_fields
            },
            status=status.HTTP_200_OK
        )

    return Response(
        {'error': f'Unsupported reward type: {package_type}'},
        status=status.HTTP_400_BAD_REQUEST
    )


@api_view(['POST'])
def purchase_package(request):
    user_id = request.data.get('user_id')
    package_title = request.data.get('package_title')
    package_type = "gold"

    # ✅ Validate input
    if not user_id or not package_title:
        return Response({'error': 'Missing user_id or package_title'}, status=status.HTTP_400_BAD_REQUEST)

    # ✅ Firestore references
    user_ref = db.collection('playerData').document(user_id)

    user_doc = user_ref.get()
    package_doc = PackageEntry.objects.filter(title=package_title).first()

    # ✅ Validate existence
    if not user_doc.exists:
        return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

    if package_doc is None:
        return Response({'error': 'Package not found'}, status=status.HTTP_404_NOT_FOUND)

    user_data = user_doc.to_dict()
    package_data = package_doc

    # ✅ Extract user gold safely
    user_gold = float(user_data.get('gold', 0))

    # ✅ Extract package details
    quantity = package_data.quantity
    reward_type = package_data.type

    updated_fields = {}
    total_gold_added = 0

    # ----------------------------
    # ✅ Handle GOLD PACKAGE
    # ----------------------------
    if reward_type == "BUNDLE":
        new_gold = user_gold + quantity
        updated_fields['gold'] = new_gold
        total_gold_added = quantity

    else:
        return Response({'error': f'Unsupported package type: {reward_type}'},
                        status=status.HTTP_400_BAD_REQUEST)

    # ✅ Apply updates
    if updated_fields:
        user_ref.update(updated_fields)

    return Response({
        'message': f'Package \"{package_title}\" purchased successfully',
        'reward_type': reward_type,
        'updated_fields': updated_fields,
        'gold_added': total_gold_added
    }, status=status.HTTP_200_OK)

@api_view(['POST'])
def purchase_item(request):
    user_id = request.data.get('user_id')
    product_id = request.data.get('product_id')

    if not user_id or not product_id:
        return Response({'error': 'INVALID_DATA(MISSING_USER_ID/MISSING_PRODUCT_ID'}, status=status.HTTP_400_BAD_REQUEST)

    # Firestore references
    user_ref = db.collection('playerData').document(user_id)
    user_doc = user_ref.get()

    product_doc = RunnerEntry.objects.filter(id=product_id).first()

    if not user_doc.exists:
        return Response({'error': 'USER_NOT_FOUND'}, status=status.HTTP_404_NOT_FOUND)
    if product_doc is None:
        return Response({'error': 'PRODUCT_NOT_FOUND'}, status=status.HTTP_404_NOT_FOUND)

    user_data = user_doc.to_dict()
    product_data = product_doc

    user_gold = user_data.get('gold', 0)
    price = product_data.price

    # Check funds
    if user_gold < price:
        return Response({ 'error': 'INSUFFICIENT_FUNDS', 'product_id':product_id, "needed_gold": price - user_gold }, status=status.HTTP_400_BAD_REQUEST)

    # Subtract gold and update Firestore
    new_gold = user_gold - price
    user_ref.update({'gold': new_gold})

    # Update the runners list
    runners = user_data.get('runners', [])

    # Check if user already owns this runner
    already_owned = any(r.get('name') == product_id for r in runners)
    if already_owned:
        return Response({'error': 'Runner already owned'}, status=status.HTTP_400_BAD_REQUEST)

    new_config = {
            "speed": to_2dp(leveled_value(1, product_data.speed, 90)),
            "power": to_2dp(leveled_value(1, product_data.power, 80)),
            "stamina": to_2dp(leveled_value(1, product_data.stamina, 90))
        }

    # Append new runner entry
    new_runner = {
        'name': product_id,
        'level': 1,
        'configuration': new_config

    }
    runners.append(new_runner)

    # Update Firestore
    user_ref.update({'runners': runners})

    return Response({
        'message': 'Purchase successful',
        'new_gold': new_gold,
        'product_id': product_id,
        'runner': new_runner,
    }, status=status.HTTP_200_OK)

@api_view(['POST'])
def upgrade_runner(request):
    user_id = request.data.get("user_id")
    runner_id = request.data.get("runner_id")

    COST = 1000
    MAX_LEVEL = 5

    # Firestore references
    user_ref = db.collection('playerData').document(user_id)

    user_doc = user_ref.get()
    runner_doc = RunnerEntry.objects.filter(id=runner_id).first()

    # ✅ Validation
    if not user_doc.exists:
        return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

    if runner_doc is None:
        return Response({'error': 'Runner not found'}, status=status.HTTP_404_NOT_FOUND)

    user_data = user_doc.to_dict()
    runner_data = runner_doc

    runners = user_data.get('runners', [])
    gold = user_data.get('gold', 0)

    # ✅ Static base configuration (NEVER modified)
    base_config = runner_data

    # ✅ Find this runner in the player's owned list
    runner_entry = next((r for r in runners if r.get('name') == runner_id), None)

    if not runner_entry:
        return Response({'error': f'Runner {runner_id} not owned'}, status=status.HTTP_400_BAD_REQUEST)

    current_level = runner_entry.get('level', 1)

    # ✅ Max level check
    if current_level >= MAX_LEVEL:
        return Response({'error': 'Runner already at max level'}, status=status.HTTP_400_BAD_REQUEST)

    # ✅ Gold check
    if gold < COST:
        return Response({
            'error': 'INSUFFICIENT_FUNDS',
            'needed_gold': COST - gold
        }, status=status.HTTP_400_BAD_REQUEST)

    # ✅ Upgrade
    new_level = current_level + 1
    new_gold = gold - COST

    # ✅ Compute player-specific configuration
    new_config = {
        "speed": to_2dp(leveled_value(new_level, base_config.speed, 90)),
        "power": to_2dp(leveled_value(new_level, base_config.power, 80)),
        "stamina": to_2dp(leveled_value(new_level, base_config.stamina, 90))
    }

    # ✅ Update only PLAYER’S runner data
    runner_entry['level'] = new_level
    runner_entry['configuration'] = new_config

    # ✅ Save back to Firestore
    user_ref.update({
        'gold': new_gold,
        'runners': runners
    })

    runner = {
        'level': new_level,
        'configuration': new_config

    }

    return Response({
        'message': 'Runner upgraded successfully!',
        'new_gold': new_gold,
        'runner' : runner,
    }, status=status.HTTP_200_OK)

def to_2dp(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def leveled_value(current_level: int, max_value: float, lowest_percent: float) -> float:
    TOTAL_LEVELS = 5
    FINAL_PERCENT = 100.0

    # Clamp level between 1 and 5
    level = max(1, min(current_level, TOTAL_LEVELS))

    # How much percentage we need to gain from level 1 → level 5
    percent_range = FINAL_PERCENT - lowest_percent

    # How much we add each level
    step = percent_range / (TOTAL_LEVELS - 1)

    # The actual percentage for this level
    percentage = lowest_percent + (step * (level - 1))

    return max_value * (percentage / 100.0)
