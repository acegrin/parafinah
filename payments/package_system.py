from payments.models import EventEntry, NewsManifestState, DataManifestStateEntry, PackageEntry
from django.http import JsonResponse, Http404
from django.utils import timezone


def get_package_manifest(current_manifest_version):
    state, created = DataManifestStateEntry.objects.get_or_create(
        data_type="package",
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        return {
            "new_data_available": False,
        }

    articles = PackageEntry.objects.values(
        "package_id",
        "updated_at"
    )

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["package_id"]),
                "updated_at": int(a["updated_at"].timestamp())
            }
            for a in articles
        ]
    }

def get_package_article(package_id):
    try:
        article = PackageEntry.objects.get(package_id=package_id)
    except PackageEntry.DoesNotExist:
        raise Http404("Event Article not found")

    return JsonResponse(article.to_dict())