from django.utils import timezone
from payments.models import DataManifestStateEntry, RunnerEntry, WorldEntry
from django.http import JsonResponse, Http404

def get_world_manifest(current_manifest_version):
    state, created = DataManifestStateEntry.objects.get_or_create(
        data_type="world",
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        return {
            "new_data_available": False,
        }


    # If the state was just created, you can optionally
    # perform any one-time initialization logic here
    if created:
        # Example: log, seed data, etc.
        pass

    articles = WorldEntry.objects.filter().values(
        "id",
        "updated_at",
    )

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["id"]),
                "updated_at": int(a["updated_at"].timestamp())
            }
            for a in articles
        ]
    }

def get_world_article(article_uid):
    try:
        article = WorldEntry.objects.get(id=article_uid)
    except WorldEntry.DoesNotExist:
        raise Http404("Article not found")

    return JsonResponse(article.to_dict())