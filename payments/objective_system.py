from payments.models import EventEntry, NewsManifestState, DataManifestStateEntry, LevelConfig
from django.http import JsonResponse, Http404
from django.utils import timezone


def get_objectives_manifest(current_manifest_version):
    state, created = DataManifestStateEntry.objects.get_or_create(
        data_type="objective",
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        return {
            "new_data_available": False,
        }

    articles = LevelConfig.objects.values(
        "level_id",
        "updated_at"
    )

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["level_id"]),
                "updated_at": int(a["updated_at"].timestamp())
            }
            for a in articles
        ]
    }

def get_objectives_article(objective_id):
    try:
        article = LevelConfig.objects.get(level_id=objective_id)
    except LevelConfig.DoesNotExist:
        raise Http404("Event Article not found")

    return JsonResponse(article.to_dict())