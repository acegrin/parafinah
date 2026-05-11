from payments.models import NewsEntry, NewsManifestState, DataManifestStateEntry
from rest_framework.decorators import api_view
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from django.http import JsonResponse, Http404
from django.utils import timezone

def resolve_news_data():
    """
    API-safe export.
    """
    return [m.to_dict() for m in NewsEntry.objects.all()]

def get_news_manifest(current_manifest_version):
    state, created = DataManifestStateEntry.objects.get_or_create(
        data_type="news",
        defaults={
            "version": 1,
            "updated_at": timezone.now(),
        }
    )

    if state.version == current_manifest_version:
        return {
            "new_data_available": False,
        }

    articles = NewsEntry.objects.values(
        "article_uid",
        "updated_at"
    )

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["article_uid"]),
                "updated_at": int(a["updated_at"].timestamp())
            }
            for a in articles
        ]
    }

def get_news_article(article_uid):
    try:
        article = NewsEntry.objects.get(article_uid=article_uid)
    except NewsEntry.DoesNotExist:
        raise Http404("News Article not found")

    return JsonResponse(article.to_dict())

