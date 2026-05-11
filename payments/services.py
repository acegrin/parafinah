from datetime import datetime, timezone
from django.db import transaction
from .models import LeaderboardEntry, LeaderboardPeriodResult, LeaderboardReward


def format_week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"

def get_period_keys():
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()

    return {
        "daily": now.strftime("%Y-%m-%d"),
        "weekly": f"{iso.year}-W{iso.week:02d}",
        "monthly": now.strftime("%Y-%m"),
    }

def recalc_ranks(period, period_key):
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

def update_leaderboards(user_id, player_name, lifetime_score):
    period_keys = get_period_keys()

    for period, period_key in period_keys.items():
        with transaction.atomic():
            entry, created = LeaderboardEntry.objects.get_or_create(
                user_id=user_id,
                period=period,
                period_key=period_key,
                defaults={
                    "player_name": player_name,
                    "baseline_score": lifetime_score,
                    "score": 0,
                    "rank": 0,
                }
            )

            if created:
                # First entry of the period → baseline set, score is 0
                recalc_ranks(period, period_key)
                continue

            # Compute delta for this period
            delta = lifetime_score - entry.baseline_score

            if delta < 0:
                # Safety guard (resets / cheating / sync issues)
                delta = 0

            if entry.score == delta:
                continue

            entry.score = delta
            entry.player_name = player_name
            entry.save(update_fields=["score", "player_name", "updated_at"])

            recalc_ranks(period, period_key)

WINNER_RANKS = [1, 2, 3]  # configurable later

def finalize_leaderboard(period, period_key):
    with transaction.atomic():
        # Guard: already finalized
        if LeaderboardPeriodResult.objects.filter(
            period=period,
            period_key=period_key
        ).exists():
            return False

        winners = (
            LeaderboardEntry.objects
            .filter(
                period=period,
                period_key=period_key,
                rank__in=WINNER_RANKS
            )
            .select_for_update()
        )

        results = []
        for entry in winners:
            results.append(
                LeaderboardPeriodResult(
                    period=period,
                    period_key=period_key,
                    user_id=entry.user_id,
                    player_name=entry.player_name,
                    rank=entry.rank,
                    score=entry.score,
                )
            )

        LeaderboardPeriodResult.objects.bulk_create(results)

        # Lock entries
        LeaderboardEntry.objects.filter(
            period=period,
            period_key=period_key
        ).update(is_finalized=True)

    return True

def apply_rewards(period, period_key):
    results = LeaderboardPeriodResult.objects.filter(
        period=period,
        period_key=period_key
    )

    for result in results:
        reward = LeaderboardReward.objects.get(
            period=period,
            rank=result.rank
        )
        grant_reward(result.user_id, reward)
