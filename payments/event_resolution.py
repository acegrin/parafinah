
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

from google.cloud.firestore import FieldFilter


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

    # Use the model's built-in helper method which handles creation,
    # default branding/image assets, and duplicate checks.
    try:
        NotificationEntry.create_from_image_url(
            user_id=user_id,
            title=title,
            description=description,
            image_url=(
                "https://parafinah.pythonanywhere.com/"
                "media/unordered/NotificationIconBlackNWhite.png"
            )
        )
        return True
    except Exception:
        logger.exception("Failed to create event result notification for user %s", user_id)
        return False

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



# ============================================================
# EVENT RESOLUTION CLEANUP
# ============================================================

def validate_cleanup_batch(data):

    event_id = data.get("event_id")
    version = data.get("version")
    batch_number = data.get("batch_number")
    total_batches = data.get("total_batches")
    user_ids = data.get("user_ids")

    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("Invalid event_id.")

    version = validate_integer(version, "version", minimum=1)
    batch_number = validate_integer(
        batch_number, "batch_number", minimum=1
    )
    total_batches = validate_integer(
        total_batches, "total_batches", minimum=1
    )

    if batch_number > total_batches:
        raise ValueError("batch_number exceeds total_batches.")

    if not isinstance(user_ids, list) or not user_ids:
        raise ValueError("user_ids must be a non-empty list.")

    if len(user_ids) > 100:
        raise ValueError("Maximum 100 players per cleanup batch.")

    normalized_ids = []

    for user_id in user_ids:

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("Invalid user ID in user_ids.")

        normalized_ids.append(user_id.strip())

    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError("Duplicate user IDs in cleanup batch.")

    return {
        "event_id": event_id.strip(),
        "version": version,
        "batch_number": batch_number,
        "total_batches": total_batches,
        "user_ids": normalized_ids
    }



def delete_event_economy_records(user_id, event_token):

    economy_ref = (
        db.collection("playerData")
        .document(user_id)
        .collection("economy_events")
    )

    documents = economy_ref.where(
        filter=FieldFilter(
            "eventIds",
            "array_contains",
            event_token
        )
    ).stream()

    deleted_count = 0
    updated_count = 0

    batch = db.batch()
    write_count = 0

    for document in documents:

        data = document.to_dict() or {}

        # Preserve event reward receipts.
        if (
            data.get("type") == "event_reward"
            or str(data.get("runId", "")).startswith("event_reward_")
            or str(data.get("context", "")).startswith("event_reward:")
        ):
            continue

        event_ids = data.get("eventIds", [])

        if not isinstance(event_ids, list):
            continue

        # Remove only the event currently being cleaned up.
        remaining_event_ids = [
            value for value in event_ids
            if value != event_token
        ]

        # Nothing remains: delete the economy record.
        if not remaining_event_ids:

            batch.delete(document.reference)
            deleted_count += 1

        else:

            # Other events still reference this record.
            batch.update(
                document.reference,
                {"eventIds": remaining_event_ids}
            )

            updated_count += 1

        write_count += 1

        # Firestore batch safety limit.
        if write_count >= 450:

            batch.commit()

            batch = db.batch()
            write_count = 0

    if write_count:

        batch.commit()

    return {
        "deleted": deleted_count,
        "updated": updated_count
    }


@api_view(["POST"])
def cleanup_event_resolution_batch(request):

    authenticated, response = control_center_authenticated(request)

    if not authenticated:
        return response

    try:

        cleanup = validate_cleanup_batch(request.data)

        event_id = cleanup["event_id"]
        version = cleanup["version"]
        batch_number = cleanup["batch_number"]
        total_batches = cleanup["total_batches"]
        user_ids = cleanup["user_ids"]

        event_token = f"{event_id}:{version}"

        total_deleted = 0
        players_processed = 0

        logger.info(
            "[EventCleanup] Starting cleanup: "
            "event=%s version=%s batch=%s/%s players=%s",
            event_id,
            version,
            batch_number,
            total_batches,
            len(user_ids)
        )

        for user_id in user_ids:

            deleted_count = delete_event_economy_records(
                user_id=user_id,
                event_token=event_token
            )

            total_deleted += deleted_count
            players_processed += 1

        logger.info(
            "[EventCleanup] Completed: "
            "event=%s version=%s batch=%s deleted=%s players=%s",
            event_id,
            version,
            batch_number,
            total_deleted,
            players_processed
        )

        return Response({
            "success": True,
            "cleanup_complete": True,
            "event_id": event_id,
            "version": version,
            "batch_number": batch_number,
            "total_batches": total_batches,
            "players_processed": players_processed,
            "records_deleted": total_deleted
        })

    except ValueError as error:

        return Response(
            {"success": False, "error": str(error)},
            status=status.HTTP_400_BAD_REQUEST
        )

    except Exception:

        logger.exception(
            "[EventCleanup] Failed to process cleanup batch."
        )

        return Response(
            {
                "success": False,
                "cleanup_complete": False,
                "error": "Unable to complete event cleanup batch."
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
