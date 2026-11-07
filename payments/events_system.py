
# event_system.py

from payments.models import DataManifestStateEntry, PublishedSnapshot
from django.http import JsonResponse, Http404
from django.utils import timezone


def get_events_manifest(current_manifest_version):

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type="event",
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        print("No new data available")
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

    old = {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["data"]["event_id"]),
                "updated_at": 0
            }
            for a in snapshot.records
            if isinstance(a.get("data"), dict)
            and a["data"].get("event_id")
        ]
    }

    new = {
        "new_data_available": True,
        "version": state.version,
        "articles": articles,
    }

    print(f"old Articles: {old}")
    print(f"new Articles: {new}")

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

        if str(data.get("event_id")) == str(event_id):

            return JsonResponse(data)

    raise Http404("Event Article not found")
