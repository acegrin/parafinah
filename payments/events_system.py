from payments.models import EventEntry, NewsManifestState, DataManifestStateEntry
from django.http import JsonResponse, Http404
from django.utils import timezone


def get_events_manifest(current_manifest_version):
    state, created = DataManifestStateEntry.objects.get_or_create(
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

    articles = EventEntry.objects.values(
        "event_id",
        "updated_at"
    )

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["event_id"]),
                "updated_at": int(a["updated_at"].timestamp())
            }
            for a in articles
        ]
    }

def get_events_article(event_id):
    try:
        article = EventEntry.objects.get(event_id=event_id)
    except EventEntry.DoesNotExist:
        raise Http404("Event Article not found")

    return JsonResponse(article.to_dict())