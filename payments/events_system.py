
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

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": articles,
    }

def get_events_article(event_id):
    print(f"Get events called for ID: {event_id}")
    try:
        snapshot = PublishedSnapshot.objects.get(data_type="event")

    except PublishedSnapshot.DoesNotExist:
        raise Http404("Event snapshot not found")

    print("Snapshot loaded")
    for record in snapshot.records:

        data = record.get("data")

        if not isinstance(data, dict):
            continue
        print("Record Found")
        if str(data.get("event_id")) == str(event_id):
            print(f"Returned: {data}")
            return JsonResponse(data)

    raise Http404("Event Article not found")
