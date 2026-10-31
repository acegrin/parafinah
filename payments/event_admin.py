import hashlib
import hmac
import json
import logging
import os

from dateutil.parser import parse as parse_datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .models import EventEntry, DataManifestStateEntry


logger = logging.getLogger(__name__)


# ============================================================
# AUTHENTICATION
# ============================================================

def control_center_authenticated(request):
	expected_key = os.environ.get("CONTROL_CENTER_API_KEY")

	if not expected_key:
		return False, Response(
			{"error": "Control Center API key is not configured on the server."},
			status=status.HTTP_503_SERVICE_UNAVAILABLE
		)

	supplied_key = request.headers.get("X-Control-Center-Key", "")

	if not supplied_key or not hmac.compare_digest(
		supplied_key,
		expected_key
	):
		return False, Response(
			{"error": "Unauthorized"},
			status=status.HTTP_401_UNAUTHORIZED
		)

	return True, None


# ============================================================
# HELPERS
# ============================================================

def parse_event_datetime(value):
	if not value:
		return None

	try:
		result = parse_datetime(str(value))

		if timezone.is_naive(result):
			result = timezone.make_aware(
				result,
				timezone.get_current_timezone()
			)

		return result

	except (ValueError, TypeError, OverflowError):
		raise ValueError(
			f"Invalid date/time value: {value}"
		)


def parse_event_boolean(value, field_name):
	if isinstance(value, bool):
		return value

	if isinstance(value, int) and value in (0, 1):
		return bool(value)

	if isinstance(value, str):
		value = value.strip().lower()

		if value in ("true", "1", "yes", "on"):
			return True

		if value in ("false", "0", "no", "off"):
			return False

	raise ValueError(
		f"Invalid boolean value for {field_name}"
	)


def parse_event_rewards(value):
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except json.JSONDecodeError:
			raise ValueError("Rewards must contain valid JSON.")

	if not isinstance(value, (dict, list)):
		raise ValueError("Rewards must be a JSON object or array.")

	return value


def generate_event_hash(event):
	"""
	Generate a positive 63-bit hash from the event's content.

	The existing Unity event response does not expose this value.
	"""

	hash_data = {
		"id": str(event.event_id),
		"payload_structure": event.payload_structure,
		"title": event.title,
		"description": event.description,
		"headline": event.headline,
		"brief": event.brief,
		"content": event.content,
		"start_time": event.start_time.isoformat(),
		"end_time": (
			event.end_time.isoformat()
			if event.end_time else None
		),
		"rewards": event.rewards,
		"status": event.status,
		"is_active": event.is_active,
	}

	serialized = json.dumps(
		hash_data,
		sort_keys=True,
		separators=(",", ":"),
		default=str
	)

	digest = hashlib.sha256(
		serialized.encode("utf-8")
	).digest()

	return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def bump_event_manifest():
	manifest, _ = DataManifestStateEntry.objects.get_or_create(
		data_type="event",
		defaults={
			"version": 1,
			"updated_at": timezone.now()
		}
	)

	manifest.bump()


def delete_stored_file(storage, filename):
	if not filename:
		return

	try:
		if storage.exists(filename):
			storage.delete(filename)

	except Exception:
		logger.exception(
			"Could not delete event image from storage: %s",
			filename
		)


def clean_event_data(request, event, creating=False):
	data = request.data

	# --------------------------------------------------------
	# Required text fields
	# --------------------------------------------------------

	required_fields = (
		"title",
		"description",
		"headline",
		"start_time",
		"rewards",
	)

	for field in required_fields:
		if creating and field not in data:
			raise ValueError(
				f"Missing required field: {field}"
			)

	text_fields = (
		"title",
		"description",
		"headline",
		"brief",
		"content",
	)

	for field in text_fields:
		if field in data:
			value = data.get(field)

			if value is None:
				value = ""

			setattr(event, field, str(value))

	# --------------------------------------------------------
	# Payload structure
	# --------------------------------------------------------

	if "payload_structure" in data:
		payload = data.get("payload_structure")

		valid_payloads = [
			choice[0]
			for choice in EventEntry._meta.get_field(
				"payload_structure"
			).choices
		]

		if payload not in valid_payloads:
			raise ValueError(
				f"Invalid payload_structure. Valid values: {valid_payloads}"
			)

		event.payload_structure = payload

	# --------------------------------------------------------
	# Dates
	# --------------------------------------------------------

	if "start_time" in data:
		event.start_time = parse_event_datetime(
			data.get("start_time")
		)

		if event.start_time is None:
			raise ValueError("start_time cannot be empty.")

	if "end_time" in data:
		event.end_time = parse_event_datetime(
			data.get("end_time")
		)

	# --------------------------------------------------------
	# Rewards
	# --------------------------------------------------------

	if "rewards" in data:
		event.rewards = parse_event_rewards(
			data.get("rewards")
		)

	# --------------------------------------------------------
	# Status
	# --------------------------------------------------------

	if "status" in data:
		valid_statuses = [
			choice[0]
			for choice in EventEntry.STATUS_CHOICES
		]

		event_status = data.get("status")

		if event_status not in valid_statuses:
			raise ValueError(
				f"Invalid status. Valid values: {valid_statuses}"
			)

		event.status = event_status

	# --------------------------------------------------------
	# Active state
	# --------------------------------------------------------

	if "is_active" in data:
		event.is_active = parse_event_boolean(
			data.get("is_active"),
			"is_active"
		)

	# --------------------------------------------------------
	# Images
	# --------------------------------------------------------

	for field_name in ("banner", "image"):

		uploaded_file = request.FILES.get(field_name)

		remove_flag = f"remove_{field_name}"

		remove_file = False

		if remove_flag in data:
			remove_file = parse_event_boolean(
				data.get(remove_flag),
				remove_flag
			)

		if uploaded_file:
			setattr(event, field_name, uploaded_file)

		elif remove_file:
			setattr(event, field_name, None)

	# --------------------------------------------------------
	# Hash
	# --------------------------------------------------------

	event.hash_code = generate_event_hash(event)

	# Validate model fields and choices.
	event.full_clean()

	return event


def event_error_response(error):
	if isinstance(error, ValidationError):
		if hasattr(error, "message_dict"):
			return Response(
				{
					"error": "Event validation failed.",
					"details": error.message_dict
				},
				status=status.HTTP_400_BAD_REQUEST
			)

		return Response(
			{
				"error": "Event validation failed.",
				"details": error.messages
			},
			status=status.HTTP_400_BAD_REQUEST
		)

	return Response(
		{"error": str(error)},
		status=status.HTTP_400_BAD_REQUEST
	)


# ============================================================
# CREATE EVENT
# ============================================================

@api_view(["POST"])
def create_event(request):

	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	try:
		with transaction.atomic():

			event = EventEntry()

			clean_event_data(
				request,
				event,
				creating=True
			)

			event.save()

			bump_event_manifest()

		return Response(
			{
				"success": True,
				"message": "Event created successfully.",
				"event_id": str(event.event_id),
				"hash_code": event.hash_code
			},
			status=status.HTTP_201_CREATED
		)

	except (ValueError, ValidationError) as error:
		return event_error_response(error)

	except Exception:
		logger.exception("Failed to create event.")

		return Response(
			{"error": "An unexpected error occurred while creating the event."},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)


# ============================================================
# UPDATE EVENT
# ============================================================

@api_view(["POST"])
def update_event(request):
	old_files = []
	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	event_id = request.data.get("event_id")

	if not event_id:
		return Response(
			{"error": "Missing event_id."},
			status=status.HTTP_400_BAD_REQUEST
		)

	old_files = []

	try:
		with transaction.atomic():

			try:
				event = EventEntry.objects.select_for_update().get(
					event_id=event_id
				)

			except EventEntry.DoesNotExist:
				return Response(
					{"error": "Event not found."},
					status=status.HTTP_404_NOT_FOUND
				)

			# Capture old files before replacing or removing them.
			for field_name in ("banner", "image"):
				field_file = getattr(event, field_name)

				if field_file and field_file.name:
					old_files.append(
						(
							field_name,
							field_file.storage,
							field_file.name
						)
					)

			clean_event_data(
				request,
				event,
				creating=False
			)

			event.save()

			# Determine which old files are no longer in use.
			files_to_delete = []

			for storage, filename in old_files:
				still_used = (
					event.banner.name == filename
					if storage == event.banner.storage
					else event.image.name == filename
				)

				if not still_used:
					files_to_delete.append(
						(storage, filename)
					)

			bump_event_manifest()

		# Delete replaced files only after the DB transaction succeeds.
		for storage, filename in files_to_delete:
			delete_stored_file(storage, filename)

		return Response(
			{
				"success": True,
				"message": "Event updated successfully.",
				"event_id": str(event.event_id),
				"hash_code": event.hash_code
			},
			status=status.HTTP_200_OK
		)

	except (ValueError, ValidationError) as error:
		return event_error_response(error)

	except Exception:
		logger.exception("Failed to update event.")

		return Response(
			{"error": "An unexpected error occurred while updating the event."},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)


# ============================================================
# DELETE EVENT
# ============================================================

@api_view(["POST"])
def delete_event(request):

	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	event_id = request.data.get("event_id")

	if not event_id:
		return Response(
			{"error": "Missing event_id."},
			status=status.HTTP_400_BAD_REQUEST
		)

	files_to_delete = []

	try:
		with transaction.atomic():

			try:
				event = EventEntry.objects.select_for_update().get(
					event_id=event_id
				)

			except EventEntry.DoesNotExist:
				return Response(
					{"error": "Event not found."},
					status=status.HTTP_404_NOT_FOUND
				)

			for field_name in ("banner", "image"):
				field_file = getattr(event, field_name)

				if field_file and field_file.name:
					files_to_delete.append(
						(
							field_file.storage,
							field_file.name
						)
					)

			event.delete()

			bump_event_manifest()

		# Delete images after successful database deletion.
		for storage, filename in files_to_delete:
			delete_stored_file(storage, filename)

		return Response(
			{
				"success": True,
				"message": "Event deleted successfully.",
				"event_id": str(event_id)
			},
			status=status.HTTP_200_OK
		)

	except Exception:
		logger.exception("Failed to delete event.")

		return Response(
			{"error": "An unexpected error occurred while deleting the event."},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)
