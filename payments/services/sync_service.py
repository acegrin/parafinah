
# payments/services/sync_service.py

import logging
import os
import uuid

from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone
from django.utils.text import get_valid_filename

from core.storage.github import OverwriteStorage
from payments.models import (
    PublishedSnapshot,
    DataManifestStateEntry,
)


logger = logging.getLogger(__name__)


class SyncValidationError(Exception):
    pass


class SyncService:

    SYNC_CONFIG = {
        "event": {
            "image_fields": {
                "banner": "events/banners",
                "image": "events/icons",
            },
        },

        "mission": {
            "image_fields": {
                "banner": "missions/banners",
            },
        },

        "runner": {
            "image_fields": {
                "icon_url": "",
            },
        },

        "objective": {
            "image_fields": {
                "icon": "",
            },
        },

        "news": {
            "image_fields": {
                "banner": "news/banners",
            },
        }
    }

    def __init__(self):

        self.storage = OverwriteStorage()

    # --------------------------------------------------
    # PUBLIC ENTRY POINT
    # --------------------------------------------------

    def publish(self, payload, uploaded_files):

        data_type = payload.get("data_type")

        config = self.SYNC_CONFIG.get(data_type)

        if not config:
            raise SyncValidationError(
                f"Unsupported data_type: {data_type}"
            )

        records = self._validate_records(
            payload.get("records"),
            config
        )

        uploads = payload.get("uploads", [])

        if not isinstance(uploads, list):
            raise SyncValidationError(
                "uploads must be a list."
            )

        uploaded_names = []

        try:

            self._process_uploads(
                records=records,
                uploads=uploads,
                uploaded_files=uploaded_files,
                image_fields=config["image_fields"],
                uploaded_names=uploaded_names,
            )

            self._validate_image_urls(
                records,
                config["image_fields"]
            )

            with transaction.atomic():

                snapshot, _ = (
                    PublishedSnapshot.objects
                    .select_for_update()
                    .get_or_create(
                        data_type=data_type,
                        defaults={"records": []},
                    )
                )

                snapshot.records = records
                snapshot.save(
                    update_fields=["records", "updated_at"]
                )

                manifest, _ = (
                    DataManifestStateEntry.objects
                    .select_for_update()
                    .get_or_create(
                        data_type=data_type,
                        defaults={"version": 0},
                    )
                )

                manifest.bump()

                version = manifest.version

            return {
                "success": True,
                "data_type": data_type,
                "version": version,
                "records_published": len(records),
                "images_uploaded": len(uploaded_names),
            }

        except Exception:

            # GitHub is external to the database transaction.
            # Remove newly uploaded files if publishing fails.

            for name in uploaded_names:
                try:
                    self.storage.delete(name)
                except Exception:
                    logger.exception(
                        "Failed to clean up GitHub file: %s",
                        name
                    )

            raise

    # --------------------------------------------------
    # RECORD VALIDATION
    # --------------------------------------------------

    def _validate_records(self, records, config):

        if not isinstance(records, list):
            raise SyncValidationError(
                "records must be a list."
            )

        validated = []
        seen_ids = set()

        for index, record in enumerate(records):

            if not isinstance(record, dict):
                raise SyncValidationError(
                    f"Record {index} must be an object."
                )

            record_id = record.get("id")
            data = record.get("data")

            if not isinstance(record_id, str) or not record_id.strip():
                raise SyncValidationError(
                    f"Record {index} has an invalid id."
                )

            if record_id in seen_ids:
                raise SyncValidationError(
                    f"Duplicate record id: {record_id}"
                )

            seen_ids.add(record_id)

            if not isinstance(data, dict):
                raise SyncValidationError(
                    f"Record {record_id} has invalid data."
                )

            updated_at = record.get("updated_at")

            if updated_at is None:
                updated_at = int(timezone.now().timestamp())

            try:
                updated_at = int(updated_at)
            except (TypeError, ValueError):
                raise SyncValidationError(
                    f"Record {record_id} has invalid updated_at."
                )

            validated.append({
                "id": record_id,
                "updated_at": updated_at,
                "data": data,
            })

        return validated

    # --------------------------------------------------
    # IMAGE UPLOADS
    # --------------------------------------------------

    def _process_uploads(
        self,
        records,
        uploads,
        uploaded_files,
        image_fields,
        uploaded_names,
    ):

        records_by_id = {
            record["id"]: record
            for record in records
        }

        processed_fields = set()

        for upload in uploads:

            if not isinstance(upload, dict):
                raise SyncValidationError(
                    "Each upload must be an object."
                )

            file_key = upload.get("file_key")
            record_id = upload.get("record_id")
            field = upload.get("field")

            if field not in image_fields:
                raise SyncValidationError(
                    f"Image field '{field}' is not allowed."
                )

            record = records_by_id.get(record_id)

            if record is None:
                raise SyncValidationError(
                    f"Upload references unknown record: {record_id}"
                )

            upload_identity = (record_id, field)

            if upload_identity in processed_fields:
                raise SyncValidationError(
                    f"Duplicate image upload for {record_id}.{field}"
                )

            processed_fields.add(upload_identity)

            uploaded_file = uploaded_files.get(file_key)

            if uploaded_file is None:
                raise SyncValidationError(
                    f"Missing uploaded file: {file_key}"
                )

            original_filename = (
                upload.get("filename")
                or uploaded_file.name
            )

            filename = get_valid_filename(
                os.path.basename(original_filename)
            )

            if not filename:
                raise SyncValidationError(
                    "Invalid uploaded filename."
                )

            folder = image_fields[field]

            unique_filename = (
                f"{uuid.uuid4().hex}_{filename}"
            )

            storage_name = (
                f"{folder}/{unique_filename}"
            )

            saved_name = self.storage.save(
                storage_name,
                uploaded_file
            )

            uploaded_names.append(saved_name)

            public_url = self.storage.url(saved_name)

            record["data"][field] = public_url

    # --------------------------------------------------
    # IMAGE URL VALIDATION
    # --------------------------------------------------

    def _validate_image_urls(self, records, image_fields):

        for record in records:

            data = record["data"]

            for field in image_fields:

                value = data.get(field)

                if not value:
                    continue

                if not isinstance(value, str):
                    raise SyncValidationError(
                        f"{record['id']}.{field} must be a URL."
                    )

                parsed = urlparse(value)

                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise SyncValidationError(
                        f"{record['id']}.{field} contains a local "
                        "image path. Upload the image before publishing."
                    )
