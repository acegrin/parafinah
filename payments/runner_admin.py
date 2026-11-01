
import hmac
import logging
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .models import RunnerEntry


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

def parse_runner_boolean(value, field_name):
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


def parse_runner_integer(value, field_name):
	try:
		result = int(value)
	except (ValueError, TypeError, OverflowError):
		raise ValueError(f"{field_name} must be an integer.")

	if result < 0:
		raise ValueError(f"{field_name} cannot be negative.")

	return result


def parse_runner_float(value, field_name):
	try:
		result = float(value)
	except (ValueError, TypeError, OverflowError):
		raise ValueError(f"{field_name} must be a number.")

	if not (-float("inf") < result < float("inf")):
		raise ValueError(f"{field_name} must be a finite number.")

	return result


def parse_runner_id(value):
	try:
		return uuid.UUID(str(value))
	except (ValueError, TypeError, AttributeError):
		raise ValueError("Invalid runner ID.")


# ============================================================
# VALIDATION AND FIELD UPDATES
# ============================================================

def clean_runner_data(request, runner, creating=False):
	data = request.data

	required_fields = (
		"name",
		"price",
		"description",
	)

	if creating:
		for field in required_fields:
			if field not in data or str(data.get(field, "")).strip() == "":
				raise ValueError(f"Missing required field: {field}")

	# Text fields
	if "name" in data:
		name = str(data.get("name", "")).strip()

		if not name:
			raise ValueError("name cannot be empty.")

		runner.name = name

	if "description" in data:
		runner.description = str(data.get("description", ""))

	# Integer fields
	if "price" in data:
		runner.price = parse_runner_integer(
			data.get("price"),
			"price"
		)

	# Numeric stat fields
	for field in ("power", "speed", "stamina"):
		if field in data:
			setattr(
				runner,
				field,
				parse_runner_float(data.get(field), field)
			)

	# Active flag
	if "is_active" in data:
		runner.is_active = parse_runner_boolean(
			data.get("is_active"),
			"is_active"
		)

	runner.full_clean()

	return runner


def runner_error_response(error):
	if isinstance(error, ValidationError):
		if hasattr(error, "message_dict"):
			return Response(
				{
					"error": "Runner validation failed.",
					"details": error.message_dict
				},
				status=status.HTTP_400_BAD_REQUEST
			)

		return Response(
			{
				"error": "Runner validation failed.",
				"details": error.messages
			},
			status=status.HTTP_400_BAD_REQUEST
		)

	return Response(
		{"error": str(error)},
		status=status.HTTP_400_BAD_REQUEST
	)


# ============================================================
# CREATE RUNNER
# ============================================================

@api_view(["POST"])
def create_runner(request):
	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	try:
		with transaction.atomic():
			runner = RunnerEntry()

			clean_runner_data(
				request,
				runner,
				creating=True
			)

			runner.save()

		return Response(
			{
				"success": True,
				"message": "Runner created successfully.",
				"runner": runner.to_dict()
			},
			status=status.HTTP_201_CREATED
		)

	except (ValueError, ValidationError) as error:
		return runner_error_response(error)

	except IntegrityError:
		return Response(
			{
				"error": (
					"A runner with this name already exists, "
					"or a database integrity constraint was violated."
				)
			},
			status=status.HTTP_400_BAD_REQUEST
		)

	except Exception:
		logger.exception("Failed to create runner.")

		return Response(
			{
				"error": (
					"An unexpected error occurred "
					"while creating the runner."
				)
			},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)


# ============================================================
# UPDATE RUNNER
# ============================================================

@api_view(["POST"])
def update_runner(request):
	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	runner_id = request.data.get("id")

	if not runner_id:
		return Response(
			{"error": "Missing runner id."},
			status=status.HTTP_400_BAD_REQUEST
		)

	try:
		runner_id = parse_runner_id(runner_id)

		with transaction.atomic():
			try:
				runner = RunnerEntry.objects.select_for_update().get(
					id=runner_id
				)

			except RunnerEntry.DoesNotExist:
				return Response(
					{"error": "Runner not found."},
					status=status.HTTP_404_NOT_FOUND
				)

			clean_runner_data(
				request,
				runner,
				creating=False
			)

			runner.save()

		return Response(
			{
				"success": True,
				"message": "Runner updated successfully.",
				"runner": runner.to_dict()
			},
			status=status.HTTP_200_OK
		)

	except (ValueError, ValidationError) as error:
		return runner_error_response(error)

	except IntegrityError:
		return Response(
			{
				"error": (
					"A runner with this name already exists, "
					"or a database integrity constraint was violated."
				)
			},
			status=status.HTTP_400_BAD_REQUEST
		)

	except Exception:
		logger.exception("Failed to update runner.")

		return Response(
			{
				"error": (
					"An unexpected error occurred "
					"while updating the runner."
				)
			},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)


# ============================================================
# DELETE RUNNER
# ============================================================

@api_view(["POST"])
def delete_runner(request):
	authenticated, response = control_center_authenticated(request)

	if not authenticated:
		return response

	runner_id = request.data.get("id")

	if not runner_id:
		return Response(
			{"error": "Missing runner id."},
			status=status.HTTP_400_BAD_REQUEST
		)

	try:
		runner_id = parse_runner_id(runner_id)

		with transaction.atomic():
			try:
				runner = RunnerEntry.objects.select_for_update().get(
					id=runner_id
				)

			except RunnerEntry.DoesNotExist:
				return Response(
					{"error": "Runner not found."},
					status=status.HTTP_404_NOT_FOUND
				)

			runner_name = runner.name
			runner.delete()

		return Response(
			{
				"success": True,
				"message": "Runner deleted successfully.",
				"runner_id": str(runner_id),
				"name": runner_name
			},
			status=status.HTTP_200_OK
		)

	except ValueError as error:
		return runner_error_response(error)

	except Exception:
		logger.exception("Failed to delete runner.")

		return Response(
			{
				"error": (
					"An unexpected error occurred "
					"while deleting the runner."
				)
			},
			status=status.HTTP_500_INTERNAL_SERVER_ERROR
		)
