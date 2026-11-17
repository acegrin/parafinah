# payments/control_center_admin.py

import hmac
import json
import logging

from django.conf import settings
from rest_framework.decorators import (
    api_view,
    parser_classes,
)
from rest_framework.parsers import (
    FormParser,
    JSONParser,
    MultiPartParser,
)
from rest_framework.response import Response
from rest_framework import status

from payments.services.sync_service import (
    SyncService,
    SyncValidationError,
)


logger = logging.getLogger(__name__)


# Same values the Control Center sends as "data_type" when syncing.
SNAPSHOT_DATA_TYPES = {
    "event",
    "mission",
    "runner",
    "objective",
    "news",
}


def _is_authorized(request):
    """Checks the X-Control-Center-Key header in constant time."""

    expected_key = settings.CONTROL_CENTER_API_KEY
    provided_key = request.headers.get(
        "X-Control-Center-Key",
        ""
    )

    return bool(
        expected_key
        and provided_key
        and hmac.compare_digest(
            str(expected_key),
            str(provided_key)
        )
    )


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def control_center_sync(request):

    # --------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------

    if not _is_authorized(request):
        return Response(
            {"error": "Unauthorized"},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # --------------------------------------------------
    # PAYLOAD
    # --------------------------------------------------

    raw_payload = request.data.get("payload")

    if not raw_payload:
        return Response(
            {"error": "Missing payload"},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:

        if isinstance(raw_payload, str):
            payload = json.loads(raw_payload)
        else:
            payload = raw_payload

    except (TypeError, ValueError):

        return Response(
            {"error": "Invalid JSON payload"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not isinstance(payload, dict):
        return Response(
            {"error": "Payload must be a JSON object"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # --------------------------------------------------
    # PUBLISH
    # --------------------------------------------------

    try:

        service = SyncService()

        result = service.publish(
            payload=payload,
            uploaded_files=request.FILES
        )

        return Response(
            result,
            status=status.HTTP_200_OK
        )

    except SyncValidationError as exc:

        return Response(
            {
                "success": False,
                "error": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    except Exception:

        logger.exception(
            "Control Center synchronization failed."
        )

        return Response(
            {
                "success": False,
                "error": "Synchronization failed."
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["POST"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def control_center_snapshot(request):
    """
    Returns the snapshot that was last synced for one data type, exactly
    as the Control Center sent it:

        {
            "success": true,
            "data_type": "event",
            "records": [
                {"id": "...", "updated_at": 1763400000, "data": {...}}
            ]
        }
    """

    # --------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------

    if not _is_authorized(request):
        return Response(
            {"error": "Unauthorized"},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # --------------------------------------------------
    # REQUEST
    # --------------------------------------------------

    data_type = request.data.get("data_type")

    if not isinstance(data_type, str) or (
        data_type not in SNAPSHOT_DATA_TYPES
    ):
        return Response(
            {
                "success": False,
                "error": (
                    "data_type must be one of: "
                    + ", ".join(sorted(SNAPSHOT_DATA_TYPES))
                ),
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    # --------------------------------------------------
    # READ SNAPSHOT
    # --------------------------------------------------

    try:

        service = SyncService()

        records = service.get_snapshot(data_type)

        return Response(
            {
                "success": True,
                "data_type": data_type,
                "records": records,
            },
            status=status.HTTP_200_OK
        )

    except SyncValidationError as exc:

        return Response(
            {
                "success": False,
                "error": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    except Exception:

        logger.exception(
            "Control Center snapshot download failed."
        )

        return Response(
            {
                "success": False,
                "error": "Snapshot download failed."
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )