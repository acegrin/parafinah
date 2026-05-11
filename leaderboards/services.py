import logging
from datetime import datetime, timezone
from django.db import transaction
from payments.models import LeaderboardEntry, LeaderboardPeriodResult, LeaderboardReward
from firebase_admin import firestore
from payments.firebase_client import db
from payments.notifications.types import NotificationType
from payments.notifications.firebase import send_notification

WINNER_RANKS = [1, 2, 3]

logger = logging.getLogger("leaderboard")
logger.setLevel(logging.DEBUG)


def get_period_keys():
    """
    Returns period keys for daily, weekly, monthly based on server time.
    Each key changes automatically when the period rolls over.
    """
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return {
        "daily": now.strftime("%Y-%m-%d"),
        "weekly": f"{year}-W{week:02d}",
        "monthly": now.strftime("%Y-%m"),
    }


def recalc_ranks(period, period_key):
    """
    Dense ranking for a single period slice.
    """
    entries = (
        LeaderboardEntry.objects
        .filter(period=period, period_key=period_key)
        .order_by("-score", "updated_at")
    )

    current_rank = 0
    last_score = None
    index = 0
    updates = []

    for entry in entries:
        index += 1
        if entry.score != last_score:
            current_rank = index
            last_score = entry.score
        if entry.rank != current_rank:
            entry.rank = current_rank
            updates.append(entry)

    if updates:
        LeaderboardEntry.objects.bulk_update(updates, ["rank"])


def update_leaderboards(user_id, player_name, points_earned):
    """
    Add points to the leaderboard for each period (daily, weekly, monthly).
    - Points reset when the period changes automatically.
    - No subtraction or deltas; just add points for the current period.
    """

    period_keys = get_period_keys()

    for period, period_key in period_keys.items():
        with transaction.atomic():
            entry, created = LeaderboardEntry.objects.get_or_create(
                user_id=user_id,
                period=period,
                period_key=period_key,
                defaults={"player_name": player_name, "score": 0, "rank": 0},
            )

            # Simply add points for this period
            entry.score += points_earned
            entry.player_name = player_name
            entry.save(update_fields=["score", "player_name", "updated_at"])

            recalc_ranks(period, period_key)


def finalize_period(period, period_key):
    """
    Finalizes a leaderboard period:
    - Persists results
    - Locks entries
    - Distributes rewards
    """
    with transaction.atomic():
        entries = (
            LeaderboardEntry.objects
            .select_for_update()
            .filter(period=period, period_key=period_key, is_finalized=False)
        )

        if not entries.exists():
            return

        # 1. Persist final results
        results = [
            LeaderboardPeriodResult(
                period=entry.period,
                period_key=entry.period_key,
                user_id=entry.user_id,
                player_name=entry.player_name,
                rank=entry.rank,
                score=entry.score,
            )
            for entry in entries
        ]
        LeaderboardPeriodResult.objects.bulk_create(results)

        # 2. Distribute rewards
        distribute_rewards(period, period_key, entries)

        # 3. Lock entries
        entries.update(is_finalized=True)


def distribute_rewards(period, period_key, entries):
    rewards = {r.rank: r for r in LeaderboardReward.objects.filter(period=period)}

    for entry in entries:
        reward = rewards.get(entry.rank)
        if not reward:
            continue
        try:
            if reward.reward_type == "gold":
                amount = reward.reward_payload.get("gold", 0)
                if amount > 0:
                    grant_gold(entry.user_id, amount)
            notify_leaderboard_reward(entry, reward)
        except Exception:
            logger.exception(
                "Failed to distribute leaderboard reward",
                extra={"user_id": entry.user_id, "period": period, "rank": entry.rank},
            )


def grant_gold(user_id, amount):
    user_ref = db.collection("playerData").document(user_id)
    user_ref.update({"gold": firestore.Increment(amount)})


def notify_leaderboard_reward(entry, reward):
    dedupe_key = f"{entry.period}:{entry.period_key}:{entry.user_id}"
    send_notification(
        user_id=str(entry.user_id),
        notification_type=NotificationType.LEADERBOARD_REWARD,
        payload={
            "period": entry.period,
            "period_key": entry.period_key,
            "rank": entry.rank,
            "reward": {"type": reward.reward_type, "data": reward.reward_payload},
        },
        source="leaderboard",
        dedupe_key=dedupe_key,
    )
