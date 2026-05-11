from payments.models import NotificationEntry, DataManifestStateEntry
from rest_framework.decorators import api_view
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from django.utils import timezone
from django.http import JsonResponse, Http404

def resolve_notification_data(user_id):
    """
    API-safe export.
    """
    return [m.to_dict() for m in NotificationEntry.objects.filter(user_id=user_id, seen=False)]

@api_view(["POST"])
def delete_notification(request):
    user_id = request.data.get("user_id")
    notification_id = request.data.get("notification_id")

    notification = get_object_or_404(
        NotificationEntry,
        id=notification_id,
        user_id=user_id
    )

    notification.delete_with_file()
    return Response({"success": True})

def get_notification_manifest(user_id, current_manifest_version):
    state, created = DataManifestStateEntry.objects.get_or_create(
        data_type=f"{user_id}_notification",
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

    articles = NotificationEntry.objects.filter(user_id=user_id).values(
        "article_uid",
        "created_at"
    )

    return {
        "new_data_available": True,
        "version": state.version,
        "articles": [
            {
                "id": str(a["article_uid"]),
                "updated_at": int(a["created_at"].timestamp())
            }
            for a in articles
        ]
    }

def get_notification_article(user_id, article_uid):
    try:
        article = NotificationEntry.objects.get(user_id= user_id,article_uid=article_uid)
    except NotificationEntry.DoesNotExist:
        raise Http404("Article not sinapezeke")

    return JsonResponse(article.to_dict())
