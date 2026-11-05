
# player_admin.py

import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .firebase_client import db
from .event_admin import control_center_authenticated


logger = logging.getLogger(__name__)


# ============================================================
# LIST PLAYERS
# ============================================================

@api_view(["GET"])
def list_players(request):

    authenticated, response = control_center_authenticated(request)

    if not authenticated:
        return response

    try:

        players = []

        documents = db.collection("playerData").stream()

        for document in documents:

            data = document.to_dict() or {}

            players.append({
                "playerId": document.id,
                "playerName": data.get("playerName", "Unknown"),
                "level": data.get("level", 0),
                "highScore": data.get("highScore", 0),
                "legendScore": data.get("legendScore", 0),
                "gold": data.get("gold", 0),
                "createdAt": data.get("createdAt"),
                "lastLogin": data.get("lastLogin"),
                "updatedAt": data.get("updatedAt")
            })

        players.sort(
            key=lambda player: player.get("lastLogin") or 0,
            reverse=True
        )

        return Response({
            "success": True,
            "count": len(players),
            "players": players
        })

    except Exception:

        logger.exception("Failed to retrieve players.")

        return Response(
            {"error": "Unable to retrieve players."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ============================================================
# PLAYER DETAIL
# ============================================================

@api_view(["POST"])
def get_player_detail(request):

    authenticated, response = control_center_authenticated(request)

    if not authenticated:
        return response

    player_id = request.data.get("player_id")

    if not player_id:
        return Response(
            {"error": "Missing player_id."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:

        player_ref = db.collection("playerData").document(
            str(player_id)
        )

        player_doc = player_ref.get()

        if not player_doc.exists:
            return Response(
                {"error": "Player not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        player_data = player_doc.to_dict() or {}

        return Response({
            "success": True,
            "playerId": player_doc.id,
            "player": player_data
        })

    except Exception:

        logger.exception(
            "Failed to retrieve player %s.",
            player_id
        )

        return Response(
            {"error": "Unable to retrieve player details."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
