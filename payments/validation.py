# payments/validation.py
import json, os, base64, logging, requests, time, uuid
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from google.cloud import firestore
from google.api_core.exceptions import GoogleAPIError
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import notification_system, news_system
from .firebase_client import db, firebase_auth
from rest_framework import status
from firebase_admin import firestore,credentials
from datetime import datetime
from cryptography.hazmat.primitives.asymmetric import padding, ec, rsa
from datetime import timezone, datetime
from dateutil.parser import parse

from .models import ValidationEntry, RunnerEntry, DataManifestStateEntry
from .mission_system import resolve_mission_data, resolve_event_data, resolve_objectives_data


def bump_validation_code(identifier: str):
    entry, _ = ValidationEntry.objects.get_or_create(
        identifier=identifier,
        defaults={"validation_code": uuid.uuid4().hex},
    )

    entry.validation_code = uuid.uuid4().hex
    entry.save(update_fields=["validation_code"])



@api_view(['POST'])
def validation_data_listener(request):
    """
    Unity calls this endpoint to validate a user's economy events
    and apply gold to the player's document in Firebase Firestore.
    """
    identifier = request.data.get("Identifier")

    if not identifier:
        return Response(
            {"error": "identifier is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        entry = ValidationEntry.objects.get(identifier=identifier)
    except ValidationEntry.DoesNotExist:
        return Response(
            {"error": "Identifier not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response(
        {
            "Identifier": entry.identifier,
            "ValidationCode": entry.validation_code,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
def get_validation_code(request):
    identifier = request.data.get("identifier")

    if not identifier:
        return Response(
            {"error": "identifier is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        entry = ValidationEntry.objects.get(identifier=identifier)
    except ValidationEntry.DoesNotExist:
        return Response(
            {"error": "Identifier not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response(
        {
            "identifier": entry.identifier,
            "validation_code": entry.validation_code,
        },
        status=status.HTTP_200_OK,
    )

@api_view(["POST"])
def fetch_data(request):
    identifier = request.data.get("identifier")

    server_data = []

    if identifier == "MISSION_DATA":
        server_data = resolve_mission_data()
    elif identifier == "EVENT_DATA":
        server_data = resolve_event_data()
    elif identifier == "OBJECTIVES_DATA":
        server_data = resolve_objectives_data()
    elif identifier == "NOTIFICATION_DATA":
        server_data = notification_system.resolve_notification_data(request.data.get("user_id"))
    elif identifier == "NEWS_DATA":
        server_data = news_system.resolve_news_data()
    return Response(
        {
            "data": server_data
        },
        status=status.HTTP_200_OK
    )

@api_view(["GET"])
def invalidate_runners(request):
    # identifier = request.data.get("identifier")
    for runner in RunnerEntry.objects.all():
        runner.bump()

    DataManifestStateEntry.objects.get(data_type="runner").bump()

    return Response({"message": f"Invalidated runners new version {DataManifestStateEntry.objects.get(data_type="runner").version}"})