
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .firebase_client import db, firebase_auth
from .event_admin import control_center_authenticated


logger = logging.getLogger(__name__)


# --------------------------------------------------
# RECURSIVE FIRESTORE DELETION
# --------------------------------------------------

def delete_document_tree(document_ref):

    # Delete all nested collections and their documents first.
    for collection_ref in document_ref.collections():

        for child_document in collection_ref.stream():

            delete_document_tree(
                child_document.reference
            )

    # Delete the document itself.
    document_ref.delete()


# --------------------------------------------------
# DELETE PLAYER
# --------------------------------------------------

@api_view(["POST"])
def delete_player(request):

    authenticated, error_response = control_center_authenticated(request)

    if not authenticated:
        return error_response

    player_id = request.data.get("player_id")

    if not player_id:
        return Response(
            {"error": "Player ID is required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    deleted = []

    try:

        # --------------------------------------------------
        # 1. DELETE PLAYER DATA AND SUBCOLLECTIONS
        # --------------------------------------------------

        player_ref = db.collection("playerData").document(player_id)

        if player_ref.get().exists:

            delete_document_tree(player_ref)
            deleted.append("playerData")

        # --------------------------------------------------
        # 2. DELETE FRAUD LOGS
        # --------------------------------------------------

        fraud_ref = db.collection("fraud_logs").document(player_id)

        if fraud_ref.get().exists:

            fraud_ref.delete()
            deleted.append("fraud_logs")

        # --------------------------------------------------
        # 3. DELETE FIREBASE AUTH ACCOUNT
        # --------------------------------------------------

        try:

            firebase_auth.delete_user(player_id)

            deleted.append("authentication")

        except firebase_auth.UserNotFoundError:

            # Already deleted; this makes retries safe.
            deleted.append("authentication (already absent)")

        return Response({
            "success": True,
            "message": "Player deleted successfully.",
            "player_id": player_id,
            "deleted": deleted
        })

    except Exception:

        logger.exception(
            "Failed to delete player %s",
            player_id
        )

        return Response(
            {
                "success": False,
                "error": "Player deletion failed. Check server logs before retrying."
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
