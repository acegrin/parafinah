# news_system.py

import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse, Http404
from django.utils import timezone

from payments.models import (
    DataManifestStateEntry,
    NewsEntry,
    PublishedSnapshot,
)


DATA_TYPE = "news"


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def _get_snapshot():
    try:
        return PublishedSnapshot.objects.get(data_type=DATA_TYPE)

    except PublishedSnapshot.DoesNotExist:
        return None


def _as_int(value, default=0):
    try:
        return int(float(value))

    except (TypeError, ValueError):
        return default


def _article_version(data):
    """
    Articles published from the Control Center carry a version.
    Anything without one counts as version 1.
    """

    return max(
        1,
        _as_int(
            data.get("version") or data.get("current_version"),
            1
        )
    )


def _context_string(value):
    """
    The game client expects context as a JSON string, exactly like
    NewsEntry.to_dict() returned it. The Control Center snapshot stores
    it as a JSON object.
    """

    if isinstance(value, str):
        return value if value.strip() else "{}"

    if isinstance(value, dict):
        return json.dumps(value)

    return "{}"


def _snapshot_records(snapshot):
    """
    Yields (record, data) for every usable record in the snapshot.
    """

    for record in snapshot.records:

        if not isinstance(record, dict):
            continue

        data = record.get("data")

        if not isinstance(data, dict) or not data.get("id"):
            continue

        yield record, data


def _article_from_data(data):
    return {
        "article_type": data.get("article_type") or "STANDARD",
        "id": str(data.get("id")),
        "version": _article_version(data),
        "headline": data.get("headline", ""),
        "brief": data.get("brief", ""),
        "content": data.get("content", ""),
        "context": _context_string(data.get("context")),
        "banner": data.get("banner") or "",
        "created_at": _as_int(data.get("created_at")),
        "updated_at": _as_int(data.get("updated_at")),
    }


def _article_from_entry(entry):
    """
    Legacy path, used only until the first snapshot is published.
    NewsEntry has no version, so these articles are version 1.
    """

    article = entry.to_dict()

    article["id"] = str(article["id"])
    article["version"] = 1
    article["updated_at"] = int(entry.updated_at.timestamp())

    return article


# --------------------------------------------------
# MANIFEST
# --------------------------------------------------

def get_news_manifest(current_manifest_version):

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type=DATA_TYPE,
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        return {
            "new_data_available": False,
        }

    snapshot = _get_snapshot()

    if snapshot is None:

        # No snapshot published yet: keep serving the legacy table.
        articles = [
            {
                "id": str(item["article_uid"]),
                "updated_at": int(item["updated_at"].timestamp()),
                "version": 1,
            }
            for item in NewsEntry.objects.values(
                "article_uid",
                "updated_at"
            )
        ]

    else:

        articles = [
            {
                "id": str(data["id"]),
                "updated_at": _as_int(record.get("updated_at")),
                "version": _article_version(data),
            }
            for record, data in _snapshot_records(snapshot)
        ]

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": articles,
    }


# --------------------------------------------------
# SINGLE ARTICLE
# --------------------------------------------------

def get_news_article(article_uid):

    snapshot = _get_snapshot()

    if snapshot is None:

        try:
            entry = NewsEntry.objects.get(article_uid=article_uid)

        except (NewsEntry.DoesNotExist, ValueError, ValidationError):
            raise Http404("News Article not found")

        return JsonResponse(_article_from_entry(entry))

    for _record, data in _snapshot_records(snapshot):

        if str(data.get("id")) == str(article_uid):
            return JsonResponse(_article_from_data(data))

    raise Http404("News Article not found")


# --------------------------------------------------
# FULL EXPORT
# --------------------------------------------------

def resolve_news_data():
    """
    API-safe export from the published news snapshot. Falls back to
    the legacy NewsEntry table until the first snapshot is published,
    so the Control Center can pull the existing articles.
    """

    snapshot = _get_snapshot()

    if snapshot is None:
        return [
            _article_from_entry(entry)
            for entry in NewsEntry.objects.all()
        ]

    return [
        _article_from_data(data)
        for _record, data in _snapshot_records(snapshot)
    ]