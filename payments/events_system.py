# event_system.py

import json

from datetime import datetime

from payments.models import DataManifestStateEntry, PublishedSnapshot
from django.http import JsonResponse, Http404
from django.utils import timezone


def _to_unix_timestamp(value):

    if value is None:
        return 0

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):

        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return 0

    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.utc)

    return int(value.timestamp())


def get_events_manifest(current_manifest_version):

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type="event",
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        return {
            "new_data_available": False,
        }

    try:
        snapshot = PublishedSnapshot.objects.get(data_type="event")

    except PublishedSnapshot.DoesNotExist:
        return {
            "new_data_available": False,
        }

    articles = [
        {
            "id": str(record["data"]["event_id"]),
            "updated_at": int(record["updated_at"]),
        }
        for record in snapshot.records
        if isinstance(record.get("data"), dict)
        and record["data"].get("event_id")
    ]

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": articles,
    }


def get_events_article(event_id):

    try:
        snapshot = PublishedSnapshot.objects.get(data_type="event")

    except PublishedSnapshot.DoesNotExist:
        raise Http404("Event snapshot not found")

    for record in snapshot.records:

        data = record.get("data")

        if not isinstance(data, dict):
            continue

        if str(data.get("event_id")) != str(event_id):
            continue

        article = {
            "id": str(data.get("event_id")),
            "current_version": int(data.get("current_version", 1)),
            "payload_structure": data.get("payload_structure", ""),
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "banner": data.get("banner", ""),
            "start_time": _to_unix_timestamp(data.get("start_time")),
            "end_time": _to_unix_timestamp(data.get("end_time")),
            "created_at": _to_unix_timestamp(data.get("created_at")),
            "updated_at": _to_unix_timestamp(data.get("updated_at")),
            "rewards": json.dumps(data.get("rewards", {})),
            "status": data.get("status", "ACTIVE"),
            "seen": False,
        }

        print(f"New: {article}")

        return JsonResponse(article)

    raise Http404("Event Article not found")