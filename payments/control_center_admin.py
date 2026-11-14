
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
    MultiPartParser,
    FormParser,
)
from rest_framework.response import Response
from rest_framework import status

from payments.services.sync_service import (
    SyncService,
    SyncValidationError,
)


logger = logging.getLogger(__name__)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def     control_center_sync(request):

    # --------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------

    expected_key = settings.CONTROL_CENTER_API_KEY
    provided_key = request.headers.get(
        "X-Control-Center-Key",
        ""
    )

    if (
        not expected_key
        or not provided_key
        or not hmac.compare_digest(
            str(expected_key),
            str(provided_key)
        )
    ):
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
