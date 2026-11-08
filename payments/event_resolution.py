# payments/event_resolution.py

import hashlib
import logging

from django.db import transaction as db_transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from google.cloud import firestore

from .models import (
    EventEntry,
    EventActivation,
    EventResolutionParticipant,
    NotificationEntry,
)

from .firebase_client import db
from .event_admin import control_center_authenticated


logger = logging.getLogger(__name__)


# ============================================================
# REWARD CONFIGURATION
# ============================================================

def get_event_gold_rewards(event):

    rewards = event.rewards or {}

    return {
        1: int(rewards.get("first_place_gold", 0)),
        2: int(rewards.get("second_place_gold", 0)),
        3: int(rewards.get("third_place_gold", 0)),
    }


# ============================================================
# RANKING
# ============================================================

def calculate_event_positions(results):

    """
    Competition ranking:
        1, 2, 2, 4

    Every participant with an equal score receives
    the same position.
    """

    ordered = sorted(
        results,
        key=lambda item: item["score"],
        reverse=True
    )

    previous_score = None
    current_position = 0

    for index, participant in enumerate(ordered):

        score = participant["score"]

        if previous_score is None or score != previous_score:
            current_position = index + 1

        participant["position"] = current_position

        previous_score = score

    return ordered


# ============================================================
# SUBMIT CALCULATED RESULTS
# ============================================================

@api_view(["POST"])
def submit_event_resolution(request):

    authenticated, response = control_center_authenticated(request)

    if not authenticated:
        return response

    event_id = request.data.get("event_id")
    version = request.data.get("version")
    results = request.data.get("results")

    if not event_id or version is None or results is None:
        return Response(
            {
                "error": "Missing event_id, version or results."
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    try:

        version = int(version)

        if version < 1:
            raise ValueError("Invalid activation version.")

        if not isinstance(results, list):
            raise ValueError("Results must be a list.")

        # ------------------------------------------
        # VALIDATE PARTICIPANTS
        # ------------------------------------------

        normalized_results = []
        seen_users = set()

        for item in results:

            if not isinstance(item, dict):
                raise ValueError("Invalid participant result.")

            user_id = str(item.get("userId", "")).strip()

            if not user_id:
                raise ValueError("A participant is missing userId.")

            if user_id in seen_users:
                raise ValueError(
                    f"Duplicate participant: {user_id}"
                )

            seen_users.add(user_id)

            score = int(item.get("score", 0))

            normalized_results.append({
                "user_id": user_id,
                "score": score
            })

        with db_transaction.atomic():

            # --------------------------------------
            # LOCK ACTIVATION AND EVENT
            # --------------------------------------

            try:
                activation = (
                    EventActivation.objects
                    .select_for_update()
                    .select_related("event")
                    .get(
                        event_id=event_id,
                        version=version
                    )
                )

            except EventActivation.DoesNotExist:
                return Response(
                    {"error": "Event activation not found."},
                    status=status.HTTP_404_NOT_FOUND
                )

            if activation.resolved_at is not None:
                return Response(
                    {
                        "success": True,
                        "already_resolved": True,
                        "message": "Activation is already resolved."
                    }
                )

            # --------------------------------------
            # THE CONTROL CENTER DECIDES WHEN A RUN ENDED
            # --------------------------------------
            #
            # The Control Center only submits after the event's
            # end time has passed, and this endpoint is already
            # authenticated with its key. Record the end here
            # instead of waiting for a sync to say so.

            if activation.ended_at is None:

                activation.ended_at = timezone.now()

                activation.save(
                    update_fields=["ended_at"]
                )

            # --------------------------------------
            # HANDLE REPEATED SUBMISSIONS
            # --------------------------------------

            if activation.results_submitted_at is not None:

                existing = {
                    participant.user_id: participant.score
                    for participant in (
                        EventResolutionParticipant.objects
                        .filter(activation=activation)
                    )
                }

                incoming = {
                    item["user_id"]: item["score"]
                    for item in normalized_results
                }

                if existing != incoming:
                    return Response(
                        {
                            "error": (
                                "Results have already been submitted "
                                "and cannot be changed."
                            )
                        },
                        status=status.HTTP_409_CONFLICT
                    )

                return Response({
                    "success": True,
                    "already_submitted": True,
                    "participant_count": len(existing)
                })

            # --------------------------------------
            # CALCULATE OFFICIAL POSITIONS
            # --------------------------------------

            ranked = calculate_event_positions([
                {
                    "user_id": item["user_id"],
                    "score": item["score"]
                }
                for item in normalized_results
            ])

            gold_rewards = get_event_gold_rewards(
                activation.event
            )

            participants = []

            for item in ranked:

                position = item["position"]

                participants.append(
                    EventResolutionParticipant(
                        activation=activation,
                        user_id=item["user_id"],
                        score=item["score"],
                        position=position,
                        gold_reward=gold_rewards.get(position, 0)
                    )
                )

            EventResolutionParticipant.objects.bulk_create(
                participants
            )

            activation.results_submitted_at = timezone.now()

            activation.save(
                update_fields=["results_submitted_at"]
            )

        return Response({
            "success": True,
            "participant_count": len(normalized_results),
            "message": "Event results saved successfully."
        })

    except (ValueError, TypeError) as error:

        return Response(
            {"error": str(error)},
            status=status.HTTP_400_BAD_REQUEST
        )

    except Exception:

        logger.exception(
            "Failed to submit event resolution."
        )

        return Response(
            {
                "error": "Unable to submit event resolution."
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================
# IDEMPOTENT FIRESTORE GOLD ISSUANCE
# ============================================================

class PlayerNotFound(ValueError):

    """
    The player document does not exist (for example a deleted
    account). There is nobody to pay, and retrying will never
    change that, so it must not block the rest of the event.
    """


def issue_event_gold(participant):

    """
    Adds Gold and creates an issuance record in one
    Firestore transaction.

    If the request is retried, the existing issuance
    record prevents Gold from being added again.
    """

    if participant.gold_reward <= 0:
        return {
            "success": True,
            "already_issued": True,
            "gold_added": 0
        }

    activation = participant.activation

    issuance_id = (
        f"{activation.event_id}_{activation.version}"
    )

    player_ref = (
        db.collection("playerData")
        .document(participant.user_id)
    )

    issuance_ref = (
        player_ref
        .collection("event_reward_issuances")
        .document(issuance_id)
    )

    firestore_transaction = db.transaction()

    @firestore.transactional
    def apply_reward(transaction):

        existing = issuance_ref.get(
            transaction=transaction
        )

        if existing.exists:

            return {
                "success": True,
                "already_issued": True,
                "gold_added": 0
            }

        player_snapshot = player_ref.get(
            transaction=transaction
        )

        if not player_snapshot.exists:
            raise PlayerNotFound(
                f"Player not found: {participant.user_id}"
            )

        player_data = player_snapshot.to_dict() or {}

        current_gold = int(player_data.get("gold", 0))

        new_gold = current_gold + participant.gold_reward

        now = int(timezone.now().timestamp())

        transaction.update(
            player_ref,
            {
                "gold": new_gold,
                "updatedAt": now
            }
        )

        transaction.create(
            issuance_ref,
            {
                "eventId": str(activation.event_id),
                "version": activation.version,
                "userId": participant.user_id,
                "goldAdded": participant.gold_reward,
                "createdAt": now
            }
        )

        return {
            "success": True,
            "already_issued": False,
            "gold_added": participant.gold_reward
        }

    return apply_reward(firestore_transaction)


# ============================================================
# RESULT NOTIFICATION
# ============================================================

def create_event_result_notification(participant):

    activation = participant.activation

    title = "Event Results"

    if participant.position == 1:
        description = (
            f"You finished 1st with {participant.score} "
            f"Legend points and earned "
            f"{participant.gold_reward} Gold!"
        )

    elif participant.position == 2:
        description = (
            f"You finished 2nd with {participant.score} "
            f"Legend points and earned "
            f"{participant.gold_reward} Gold!"
        )

    elif participant.position == 3:
        description = (
            f"You finished 3rd with {participant.score} "
            f"Legend points and earned "
            f"{participant.gold_reward} Gold!"
        )

    else:
        description = (
            f"You finished in position {participant.position} "
            f"with {participant.score} Legend points. "
            f"Better luck next time!"
        )

    # A stable hash helps identify the same result notification.
    hash_code = int(
        hashlib.sha256(
            (
                f"event-result:"
                f"{activation.event_id}:"
                f"{activation.version}:"
                f"{participant.user_id}"
            ).encode()
        ).hexdigest(),
        16
    ) % (10 ** 18)

    with db_transaction.atomic():

        locked = (
            EventResolutionParticipant.objects
            .select_for_update()
            .get(pk=participant.pk)
        )

        if locked.notification_sent:
            return

        NotificationEntry.objects.get_or_create(
            user_id=locked.user_id,
            hash_code=hash_code,
            defaults={
                "title": title,
                "description": description,
                "seen": False
            }
        )

        locked.notification_sent = True

        locked.save(
            update_fields=["notification_sent"]
        )


# ============================================================
# PROCESS PENDING PARTICIPANTS
# ============================================================

@api_view(["POST"])
def process_event_resolution(request):

    authenticated, response = control_center_authenticated(request)

    if not authenticated:
        return response

    event_id = request.data.get("event_id")
    version = request.data.get("version")

    if not event_id or version is None:
        return Response(
            {"error": "Missing event_id or version."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:

        version = int(version)

        batch_size = int(
            request.data.get("batch_size", 25)
        )

        batch_size = max(1, min(batch_size, 100))

        with db_transaction.atomic():

            try:
                activation = (
                    EventActivation.objects
                    .select_for_update()
                    .select_related("event")
                    .get(
                        event_id=event_id,
                        version=version
                    )
                )

            except EventActivation.DoesNotExist:
                return Response(
                    {"error": "Event activation not found."},
                    status=status.HTTP_404_NOT_FOUND
                )

            if activation.resolved_at is not None:
                return Response({
                    "success": True,
                    "complete": True,
                    "already_resolved": True,
                    "message": "Activation is already resolved."
                })

            if activation.results_submitted_at is None:
                return Response(
                    {
                        "error": (
                            "Results have not been submitted yet."
                        )
                    },
                    status=status.HTTP_409_CONFLICT
                )

        # ------------------------------------------
        # PROCESS REWARDS AND NOTIFICATIONS
        # ------------------------------------------

        pending = list(
            EventResolutionParticipant.objects
            .filter(
                activation=activation
            )
            .filter(
                reward_issued=False
            )
            .order_by("position", "user_id")[:batch_size]
        )

        processed = 0

        for participant in pending:

            # Gold is independently idempotent in Firestore.
            # Any other failure (network, Firestore outage) is raised
            # so the whole call can be retried safely.
            try:
                issue_event_gold(participant)

            except PlayerNotFound:

                # Nobody to pay; do not let one missing account
                # block every other player in the event.
                logger.warning(
                    "Skipping Gold for missing player %s "
                    "(event %s, version %s, %s Gold).",
                    participant.user_id,
                    activation.event_id,
                    activation.version,
                    participant.gold_reward
                )

            EventResolutionParticipant.objects.filter(
                pk=participant.pk
            ).update(
                reward_issued=True
            )

            participant.reward_issued = True

            # Notification is independently idempotent in Django.
            create_event_result_notification(participant)

            processed += 1

        # Also catch participants whose Gold was issued
        # but whose notification still needs to be created.
        notification_pending = list(
            EventResolutionParticipant.objects
            .filter(
                activation=activation,
                reward_issued=True,
                notification_sent=False
            )
            .order_by("position", "user_id")[:batch_size]
        )

        for participant in notification_pending:

            create_event_result_notification(participant)

        # ------------------------------------------
        # CHECK COMPLETION
        # ------------------------------------------

        unfinished = EventResolutionParticipant.objects.filter(
            activation=activation
        ).filter(
            reward_issued=False
        ).exists()

        notifications_pending = EventResolutionParticipant.objects.filter(
            activation=activation,
            notification_sent=False
        ).exists()

        if not unfinished and not notifications_pending:

            with db_transaction.atomic():

                activation = (
                    EventActivation.objects
                    .select_for_update()
                    .select_related("event")
                    .get(pk=activation.pk)
                )

                if activation.resolved_at is None:

                    activation.resolved_at = timezone.now()

                    activation.save(
                        update_fields=["resolved_at"]
                    )

                    event = EventEntry.objects.select_for_update().get(
                        event_id=activation.event_id
                    )

                    if event.current_version == activation.version:

                        event.status = "RESOLVED"
                        event.is_active = False
                        event.resolved_at = activation.resolved_at

                        from .event_admin import (
                            generate_event_hash,
                            bump_event_manifest
                        )

                        event.hash_code = generate_event_hash(event)

                        event.save()

                        bump_event_manifest()

            return Response({
                "success": True,
                "complete": True,
                "processed": processed,
                "message": "Event resolution completed."
            })

        remaining = EventResolutionParticipant.objects.filter(
            activation=activation
        ).filter(
            reward_issued=False
        ).count()

        notification_remaining = EventResolutionParticipant.objects.filter(
            activation=activation,
            notification_sent=False
        ).count()

        return Response({
            "success": True,
            "complete": False,
            "processed": processed,
            "remaining_rewards": remaining,
            "remaining_notifications": notification_remaining,
            "message": "Event resolution is still processing."
        })

    except Exception:

        logger.exception(
            "Failed to process event resolution."
        )

        return Response(
            {
                "error": (
                    "Event resolution processing failed. "
                    "It can safely be retried."
                )
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================
# FIND COMPLETED ACTIVATIONS
# ============================================================

@api_view(["GET"])
def get_pending_event_resolutions(request):

    authenticated, response = control_center_authenticated(request)

    if not authenticated:
        return response

    try:

        activations = (
            EventActivation.objects
            .filter(
                ended_at__isnull=False,
                resolved_at__isnull=True
            )
            .select_related("event")
            .order_by("ended_at")
        )

        pending = []

        for activation in activations:

            pending.append({
                "event_id": str(activation.event_id),
                "event_title": activation.event.title,
                "version": activation.version,
                "ended_at": activation.ended_at.isoformat(),
                "results_submitted": (
                    activation.results_submitted_at is not None
                )
            })

        return Response({
            "success": True,
            "count": len(pending),
            "activations": pending
        })

    except Exception:

        logger.exception(
            "Failed to retrieve pending event resolutions."
        )

        return Response(
            {
                "error": (
                    "Unable to retrieve pending event resolutions."
                )
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )