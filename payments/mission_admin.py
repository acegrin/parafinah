import hashlib
import hmac
import json
import logging

from dateutil.parser import parse as parse_datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .models import Mission, LevelConfig, DataManifestStateEntry


logger = logging.getLogger(__name__)


# ============================================================
# AUTHENTICATION
# ============================================================

def control_center_authenticated(request):
	expected_key = settings.CONTROL_CENTER_API_KEY

	if not expected_key:
		return False, Response(
			{"error": "Control Center API key is not configured on the server."},
			status=status.HTTP_503_SERVICE_UNAVAILABLE
		)

	supplied_key = request.headers.get("X-Control-Center-Key", "")

	if not supplied_key or not hmac.compare_digest(
		supplied_key, expected_key
	):
		return False, Response(
			{"error": "Unauthorized"},
			status=status.HTTP_401_UNAUTHORIZED
		)

	return True, None


# ============================================================
# PARSING HELPERS
# ============================================================

def parse_mission_datetime(value, field_name, required=False):
	if value is None or str(value).strip() == "":
		if required:
			raise ValueError(f"{field_name} cannot be empty.")
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
		raise ValueError(f"Invalid date/time value for {field_name}.")


def parse_mission_boolean(value, field_name):
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

	raise ValueError(f"Invalid boolean value for {field_name}.")


def parse_mission_json(value, field_name):
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except json.JSONDecodeError:
			raise ValueError(f"{field_name} must contain valid JSON.")

	if not isinstance(value, (dict, list)):
		raise ValueError(
			f"{field_name} must be a JSON object or array."
		)

	return value


def parse_mission_target(value):
	if value is None or str(value).strip() == "":
		return None

	try:
		return int(value)
	except (ValueError, TypeError, OverflowError):
		raise ValueError("target_value must be an integer or empty.")


def resolve_mission_level(value):
	if value is None or str(value).strip() == "":
		raise ValueError("A mission level is required.")

	value = str(value).strip()
	normalized = value.lower().replace(" ", "")

	if normalized.startswith("level"):
		normalized = normalized[5:]

	try:
		level_number = int(normalized)
	except (ValueError, TypeError):
		raise ValueError("Invalid level. Use Level1 through Level5.")

	try:
		return LevelConfig.objects.get(level=level_number)
	except LevelConfig.DoesNotExist:
		raise ValueError(
			f"Level{level_number} does not exist in LevelConfig."
		)


# ============================================================
# HASH AND MANIFEST
# ============================================================

def generate_mission_hash(mission):
	hash_data = {
		"mission_id": mission.mission_id,
		"mission_type": mission.mission_type,
		"title": mission.title,
		"level": mission.level.level,
		"progress_mode": mission.progress_mode,
		"time_scope": mission.time_scope,
		"resolution_mode": mission.resolution_mode,
		"start_time": (
			mission.start_time.isoformat()
			if mission.start_time else None
		),
		"end_time": (
			mission.end_time.isoformat()
			if mission.end_time else None
		),
		"resolved_at": (
			mission.resolved_at.isoformat()
			if mission.resolved_at else None
		),
		"progress_definition": mission.progress_definition,
		"target_value": mission.target_value,
		"rewards": mission.rewards,
		"status": mission.status,
		"is_active": mission.is_active,
		"image": mission.image.name if mission.image else "",
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


def bump_mission_manifest():
	manifest, _ = DataManifestStateEntry.objects.get_or_create(
		data_type="mission",
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
			"Could not delete mission image: %s", filename
		)


# ============================================================
# VALIDATION AND FIELD UPDATES
# ============================================================

def clean_mission_data(request, mission, creating=False):
	data = request.data

	required_fields = (
		"mission_id",
		"title",
		"level",
		"progress_mode",
		"time_scope",
		"resolution_mode",
		"start_time",
		"progress_definition",
		"rewards",
	)

	if creating:
		for field in required_fields:
			if field not in data or str(data.get(field, "")).strip() == "":
				raise ValueError(f"Missing required field: {field}")

	# Text / identifier fields
	for field in ("mission_id", "title"):
		if field in data:
			value = str(data.get(field, "")).strip()

			if not value:
				raise ValueError(f"{field} cannot be empty.")

			setattr(mission, field, value)

	# Choice fields
	choice_fields = (
		"mission_type",
		"progress_mode",
		"time_scope",
		"resolution_mode",
		"status",
	)

	for field in choice_fields:
		if field in data:
			value = str(data.get(field, "")).strip()

			valid_values = [
				choice[0]
				for choice in mission._meta.get_field(field).choices
			]

			if value not in valid_values:
				raise ValueError(
					f"Invalid {field}. Valid values: {valid_values}"
				)

			setattr(mission, field, value)

	# LevelConfig foreign key
	if "level" in data:
		mission.level = resolve_mission_level(data.get("level"))

	# Dates
	if "start_time" in data:
		mission.start_time = parse_mission_datetime(
			data.get("start_time"),
			"start_time",
			required=True
		)

	if "end_time" in data:
		mission.end_time = parse_mission_datetime(
			data.get("end_time"),
			"end_time"
		)

	if "resolved_at" in data:
		mission.resolved_at = parse_mission_datetime(
			data.get("resolved_at"),
			"resolved_at"
		)

	# JSON fields
	for field in ("progress_definition", "rewards"):
		if field in data:
			setattr(
				mission,
				field,
				parse_mission_json(data.get(field), field)
			)

	# Optional integer target
	if "target_value" in data:
		mission.target_value = parse_mission_target(
			data.get("target_value")
		)

	# Active flag
	if "is_active" in data:
		mission.is_active = parse_mission_boolean(
			data.get("is_active"),
			"is_active"
		)

	# Image upload. Keep existing image when editing without a new upload.
	uploaded_image = request.FILES.get("image")

	if uploaded_image:
		mission.image = uploaded_image
	elif creating and not mission.image:
		raise ValueError("A mission image is required.")

	mission.hash_code = generate_mission_hash(mission)
	mission.full_clean()

	return mission


def mission_error_response(error):
	if isinstance(error, ValidationError):
		if hasattr(error, "message_dict"):
			return Response(
				{
					"error": "Mission validation failed.",
					"details": error.message_dict
				},
				status=status.HTTP_400_BAD_REQUEST
			)

		return Response(
			{
				"error": "Mission validation failed.",
				"details": error.messages
			},
			status=status.HTTP_400_BAD_REQUEST
		)

	return Response(
		{"error": str(error)},
		status=status.HTTP_400_BAD_REQUEST
	)


# ============================================================
# CREATE MISSION
# ============================================================

@api_view(["POST"])
def create_mission(request):
	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	try:
		with transaction.atomic():
			mission = Mission()

			clean_mission_data(
				request,
				mission,
				creating=True
			)

			mission.save()
			bump_mission_manifest()

		return Response(
			{
				"success": True,
				"message": "Mission created successfully.",
				"mission_id": mission.mission_id,
				"hash_code": mission.hash_code
			},
			status=status.HTTP_201_CREATED
		)

	except (ValueError, ValidationError) as error:
		return mission_error_response(error)

	except IntegrityError:
		return Response(
			{
				"error": (
					"A mission with this mission_id already exists, "
					"or a database integrity constraint was violated."
				)
			},
			status=status.HTTP_400_BAD_REQUEST
		)

	except Exception:
		logger.exception("Failed to create mission.")

		return Response(
			{"error": "An unexpected error occurred while creating the mission."},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)


# ============================================================
# UPDATE MISSION
# ============================================================

@api_view(["POST"])
def update_mission(request):
	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	mission_id = request.data.get("mission_id")

	if not mission_id:
		return Response(
			{"error": "Missing mission_id."},
			status=status.HTTP_400_BAD_REQUEST
		)

	old_image = None
	image_storage = None

	try:
		with transaction.atomic():
			try:
				mission = Mission.objects.select_for_update().get(
					mission_id=mission_id
				)

			except Mission.DoesNotExist:
				return Response(
					{"error": "Mission not found."},
					status=status.HTTP_404_NOT_FOUND
				)

			if mission.image and mission.image.name:
				old_image = mission.image.name
				image_storage = mission.image.storage

			clean_mission_data(
				request,
				mission,
				creating=False
			)

			mission.save()
			new_image_name = mission.image.name if mission.image else None
			bump_mission_manifest()

		# Remove the old image only after the database transaction succeeds.
		if old_image and old_image != new_image_name:
			delete_stored_file(image_storage, old_image)

		return Response(
			{
				"success": True,
				"message": "Mission updated successfully.",
				"mission_id": mission.mission_id,
				"hash_code": mission.hash_code
			},
			status=status.HTTP_200_OK
		)

	except (ValueError, ValidationError) as error:
		return mission_error_response(error)

	except IntegrityError:
		return Response(
			{"error": "A database integrity constraint was violated."},
			status=status.HTTP_400_BAD_REQUEST
		)

	except Exception:
		logger.exception("Failed to update mission.")

		return Response(
			{"error": "An unexpected error occurred while updating the mission."},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)


# ============================================================
# DELETE MISSION
# ============================================================

@api_view(["POST"])
def delete_mission(request):
	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	mission_id = request.data.get("mission_id")

	if not mission_id:
		return Response(
			{"error": "Missing mission_id."},
			status=status.HTTP_400_BAD_REQUEST
		)

	image_storage = None
	image_name = None

	try:
		with transaction.atomic():
			try:
				mission = Mission.objects.select_for_update().get(
					mission_id=mission_id
				)

			except Mission.DoesNotExist:
				return Response(
					{"error": "Mission not found."},
					status=status.HTTP_404_NOT_FOUND
				)

			if mission.image and mission.image.name:
				image_storage = mission.image.storage
				image_name = mission.image.name

			mission.delete()
			bump_mission_manifest()

		if image_name:
			delete_stored_file(image_storage, image_name)

		return Response(
			{
				"success": True,
				"message": "Mission deleted successfully.",
				"mission_id": str(mission_id)
			},
			status=status.HTTP_200_OK
		)

	except Exception:
		logger.exception("Failed to delete mission.")

		return Response(
			{"error": "An unexpected error occurred while deleting the mission."},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)
