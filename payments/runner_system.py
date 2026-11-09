
from payments.models import DataManifestStateEntry, PublishedSnapshot
from django.http import JsonResponse, Http404
from django.utils import timezone


def get_runners_manifest(current_manifest_version):

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type="runner",
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
        snapshot = PublishedSnapshot.objects.get(data_type="runner")

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


def get_runners_article(runner_id):

    try:
        snapshot = PublishedSnapshot.objects.get(data_type="runner")

    except PublishedSnapshot.DoesNotExist:
        raise Http404("Runner snapshot not found")

    for record in snapshot.records:

        data = record.get("data")

        if not isinstance(data, dict):
            continue

        if str(data.get("id")) != str(runner_id):
            continue

        article = {
            "id": str(data.get("id")),
            "name": data.get("name"),
            "icon_url": data.get("icon_url", ""),
            "bundle_url": data.get("bundle_url", ""),
            "price": data.get("price", 0),
            "description": data.get("description", ""),
            "power": data.get("power", 1.0),
            "speed": data.get("speed", 1.0),
            "stamina": data.get("stamina", 1.0),
            "version": data.get("version", 1),
            "created_at": int(data.get("created_at", 0)),
            "updated_at": int(data.get("updated_at", 0)),
            "is_active": data.get("is_active", True),
        }

        return JsonResponse(article)

    raise Http404("Runner not found")


def resolve_runner_data():
    """
    API-safe export from the published runner snapshot.
    """

    try:
        snapshot = PublishedSnapshot.objects.get(data_type="runner")

    except PublishedSnapshot.DoesNotExist:
        return []

    return [
        record["data"]
        for record in snapshot.records
        if isinstance(record.get("data"), dict)
    ]
