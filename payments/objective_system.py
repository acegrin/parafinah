from payments.models import DataManifestStateEntry, PublishedSnapshot
from django.http import JsonResponse, Http404
from django.utils import timezone


def get_objectives_manifest(current_manifest_version):
    state, _ = DataManifestStateEntry.objects.get_or_create(
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

    try:
        snapshot = PublishedSnapshot.objects.get(data_type="objective")
    except PublishedSnapshot.DoesNotExist:
        return {
            "new_data_available": False,
        }

    articles = [
        {
            "id": str(record["data"]["id"]),
            "updated_at": int(record["updated_at"]),
        }
        for record in snapshot.records
        if isinstance(record.get("data"), dict)
        and record["data"].get("id")
    ]

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": articles,
    }


def get_objectives_article(objective_id):
    try:
        snapshot = PublishedSnapshot.objects.get(data_type="objective")
    except PublishedSnapshot.DoesNotExist:
        raise Http404("Objective snapshot not found")

    for record in snapshot.records:
        data = record.get("data")

        if not isinstance(data, dict):
            continue

        if str(data.get("id")) != str(objective_id):
            continue

        # Reconstructs the exact dictionary format previously provided by LevelConfig.to_dict()
        level_num = data.get("level", 1)
        article = {
            "id": str(data.get("id")),
            "name": data.get("name", ""),
            "title": f"Level{level_num}",
            "level": level_num,
            "value": data.get("value", 0),
            "icon": data.get("icon", ""),
            "created_at": int(data.get("created_at", 0)),
            "updated_at": int(data.get("updated_at", 0)),
        }

        return JsonResponse(article)

    raise Http404("Event Article not found")


def resolve_objective_data():
    """
    API-safe export from the published objective snapshot.
    """
    try:
        snapshot = PublishedSnapshot.objects.get(data_type="objective")
    except PublishedSnapshot.DoesNotExist:
        return []

    return [
        record["data"]
        for record in snapshot.records
        if isinstance(record.get("data"), dict)
    ]