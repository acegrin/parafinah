#event_system.py

from payments.models import EventEntry, DataManifestStateEntry, PublishedSnapshot
from django.http import JsonResponse, Http404
from django.utils import timezone


def get_events_manifest(current_manifest_version):

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type="event",
        defaults={
            "version": 0,
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
            "id": str(record["id"]),
            "updated_at": int(record["updated_at"]),
        }
        for record in snapshot.records
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

        if str(record.get("id")) == str(event_id):

            data = record.get("data")

            if not isinstance(data, dict):
                raise Http404("Invalid event record")

            return JsonResponse(data)

    raise Http404("Event Article not found")