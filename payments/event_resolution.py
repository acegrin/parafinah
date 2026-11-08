
import hashlib
import logging

from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from google.cloud import firestore

from .models import NotificationEntry
from .firebase_client import db
from .event_admin import control_center_authenticated


logger = logging.getLogger(__name__)


# ============================================================
# EXCEPTIONS
# ============================================================

class PlayerNotFound(Exception):
    pass


# ============================================================
# FIRESTORE GOLD REWARD
# ============================================================

def issue_event_gold(user_id, event_id, version, gold_reward):

    if gold_reward <= 0:
        return {
            "already_issued": False,
            "gold_added": 0
        }

    transaction_id = (
        f"event_reward_{event_id}_{version}"
    )

    player_ref = (
        db.collection("playerData")
        .document(user_id)
    )

    economy_ref = (
        player_ref
        .collection("economy_events")
        .document(transaction_id)
    )

    firestore_transaction = db.transaction()

    @firestore.transactional
    def apply_reward(transaction):

        # Check whether this reward was already issued.
        existing = economy_ref.get(
            transaction=transaction
        )

        if existing.exists:
            return {
                "already_issued": True,
                "gold_added": 0
            }

        # Retrieve the player's current balance.
        player_snapshot = player_ref.get(
            transaction=transaction
        )

        if not player_snapshot.exists:
            raise PlayerNotFound(
                f"Player not found: {user_id}"
            )

        player_data = player_snapshot.to_dict() or {}

        current_gold = int(
            player_data.get("gold", 0)
        )

        new_gold = current_gold + gold_reward

        now = int(timezone.now().timestamp())
        timestamp_ms = now * 1000

        # Economy record.
        economy_record = {
            "runId": transaction_id,
            "type": "event_reward",
            "collectedType": "gold",
            "delta": gold_reward,
            "timestampUtc": timestamp_ms,
            "sourceId": event_id,
            "context": f"event_reward:{version}",

            # Event rewards must not count as player
            # activity when calculating event scores.
            "eventIds": [],

            "clientVersion": "control_center",
            "deviceId": "control_center",
            "serverTimestamp": now
        }

        # Update the balance and create the receipt
        # atomically.
        transaction.update(
            player_ref,
            {
                "gold": new_gold,
                "updatedAt": now
            }
        )

        transaction.create(
            economy_ref,
            economy_record
        )

        return {
            "already_issued": False,
            "gold_added": gold_reward
        }

    return apply_reward(firestore_transaction)


# ============================================================
# RESULT NOTIFICATION
# ============================================================

def create_event_result_notification(
    user_id,
    event_id,
    version,
    score,
    position,
    gold_reward
):

    title = "Event Results"

    if position in [1, 2, 3]:

        suffix = {
            1: "1st",
            2: "2nd",
            3: "3rd"
        }[position]

        description = (
            f"You finished {suffix} with {score} "
            f"Legend points and earned "
            f"{gold_reward} Gold!"
        )

    else:

        description = (
            f"You finished in position {position} "
            f"with {score} Legend points. "
            f"Better luck next time!"
        )

    # Deterministic notification ID.
    hash_code = int(
        hashlib.sha256(
            (
                f"event-result:"
                f"{event_id}:"
                f"{version}:"
                f"{user_id}"
            ).encode()
        ).hexdigest(),
        16
    ) % (10 ** 18)

    # Use get_or_create with matching defaults
    notification, created = (
        NotificationEntry.objects.get_or_create(
            user_id=user_id,
            hash_code=hash_code,
            defaults={
                "title": title,
                "description": description,
                "seen": False,
                # Include image_url if your NotificationEntry model requires it
                "image_url": (
                    "https://parafinah.pythonanywhere.com/"
                    "media/unordered/NotificationIconBlackNWhite.png"
                )
            }
        )
    )

    return created

# ============================================================
# VALIDATION HELPERS
# ============================================================

def validate_integer(value, field_name, minimum=None):

    if isinstance(value, bool):
        raise ValueError(f"Invalid {field_name}.")

    try:
        converted = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field_name}.")

    # Reject fractional numeric values.
    if isinstance(value, float) and value != converted:
        raise ValueError(f"Invalid {field_name}.")

    if minimum is not None and converted < minimum:
        raise ValueError(
            f"{field_name} must be at least {minimum}."
        )

    return converted


def validate_batch(request_data):

    event_id = str(
        request_data.get("event_id", "")
    ).strip()

    if not event_id:
        raise ValueError("Missing event_id.")

    version = validate_integer(
        request_data.get("version"),
        "version",
        minimum=1
    )

    batch_number = validate_integer(
        request_data.get("batch_number"),
        "batch_number",
        minimum=1
    )

    total_batches = validate_integer(
        request_data.get("total_batches"),
        "total_batches",
        minimum=1
    )

    total_participants = validate_integer(
        request_data.get("total_participants"),
        "total_participants",
        minimum=1
    )

    if batch_number > total_batches:
        raise ValueError("Invalid batch_number.")

    participants = request_data.get("participants")

    if not isinstance(participants, list) or not participants:
        raise ValueError("Batch must contain participants.")

    if len(participants) > 100:
        raise ValueError("Batch exceeds maximum size.")

    normalized = []
    seen_users = set()

    for item in participants:

        if not isinstance(item, dict):
            raise ValueError("Invalid participant data.")

        user_id = str(
            item.get("userId", "")
        ).strip()

        if not user_id:
            raise ValueError(
                "A participant is missing userId."
            )

        if user_id in seen_users:
            raise ValueError(
                f"Duplicate participant: {user_id}"
            )

        seen_users.add(user_id)

        score = validate_integer(
            item.get("score"),
            "score",
            minimum=0
        )

        position = validate_integer(
            item.get("position"),
            "position",
            minimum=1
        )

        gold_reward = validate_integer(
            item.get("gold_reward"),
            "gold_reward",
            minimum=0
        )

        normalized.append({
            "user_id": user_id,
            "score": score,
            "position": position,
            "gold_reward": gold_reward
        })

    if len(normalized) > total_participants:
        raise ValueError(
            "Batch contains more participants than declared."
        )

    return {
        "event_id": event_id,
        "version": version,
        "batch_number": batch_number,
        "total_batches": total_batches,
        "total_participants": total_participants,
        "participants": normalized
    }


# ============================================================
# PROCESS PREPARED BATCH
# ============================================================

@api_view(["POST"])
def process_batch_resolution(request):

    authenticated, response = control_center_authenticated(
        request
    )

    if not authenticated:
        return response

    try:

        # ----------------------------------------------------
        # VALIDATE REQUEST
        # ----------------------------------------------------

        batch = validate_batch(request.data)

        event_id = batch["event_id"]
        version = batch["version"]
        batch_number = batch["batch_number"]
        participants = batch["participants"]

        # ----------------------------------------------------
        # PROCESS PARTICIPANTS
        # ----------------------------------------------------

        paid = 0
        already_paid = 0
        no_reward = 0
        notifications_created = 0
        notifications_existing = 0

        for participant in participants:

            user_id = participant["user_id"]
            score = participant["score"]
            position = participant["position"]
            gold_reward = participant["gold_reward"]

            # ----------------------------------------------
            # ISSUE GOLD
            # ----------------------------------------------

            result = issue_event_gold(
                user_id=user_id,
                event_id=event_id,
                version=version,
                gold_reward=gold_reward
            )

            if gold_reward <= 0:
                no_reward += 1

            elif result["already_issued"]:
                already_paid += 1

            else:
                paid += 1

            # ----------------------------------------------
            # CREATE NOTIFICATION
            # ----------------------------------------------

            notification_created = (
                create_event_result_notification(
                    user_id=user_id,
                    event_id=event_id,
                    version=version,
                    score=score,
                    position=position,
                    gold_reward=gold_reward
                )
            )

            if notification_created:
                notifications_created += 1
            else:
                notifications_existing += 1

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        logger.info(
            "Event batch processed successfully. "
            "Event=%s Version=%s Batch=%s Participants=%s",
            event_id,
            version,
            batch_number,
            len(participants)
        )

        return Response(
            {
                "success": True,
                "batch_complete": True,
                "event_id": event_id,
                "version": version,
                "batch_number": batch_number,
                "participant_count": len(participants),
                "paid": paid,
                "already_paid": already_paid,
                "no_reward": no_reward,
                "notifications_created": notifications_created,
                "notifications_existing": notifications_existing,
                "message": (
                    f"Batch {batch_number} processed successfully."
                )
            },
            status=status.HTTP_200_OK
        )

    except (ValueError, TypeError) as error:

        logger.warning(
            "Invalid event resolution batch: %s",
            str(error)
        )

        return Response(
            {
                "success": False,
                "batch_complete": False,
                "error": str(error),
                "message": "The batch contains invalid data."
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    except PlayerNotFound as error:

        logger.error(
            "Event resolution failed: %s",
            str(error)
        )

        return Response(
            {
                "success": False,
                "batch_complete": False,
                "error": str(error),
                "message": (
                    "A player could not be found. "
                    "The batch was not fully processed and can be retried."
                )
            },
            status=status.HTTP_404_NOT_FOUND
        )

    except Exception:

        logger.exception(
            "Failed to process event resolution batch."
        )

        return Response(
            {
                "success": False,
                "batch_complete": False,
                "message": (
                    "Batch processing failed. "
                    "The same batch can safely be retried."
                )
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
