import time
import uuid
from firebase_admin import firestore

db = firestore.client()

def send_notification(
    *,
    user_id: str,
    notification_type: str,
    payload: dict,
    source: str,
    dedupe_key: str | None = None,
):
    """
    Server-authoritative notification sender.
    Firestore-backed.
    """

    ref = (
        db.collection("player_notifications")
          .document(user_id)
          .collection("items")
    )

    if dedupe_key:
        existing = (
            ref.where("dedupe_key", "==", dedupe_key)
               .limit(1)
               .get()
        )
        if existing:
            return

    notification_id = str(uuid.uuid4())

    ref.document(notification_id).set({
        "type": notification_type,
        "payload": payload,
        "source": source,
        "dedupe_key": dedupe_key,
        "created_at": firestore.SERVER_TIMESTAMP,
        "read": False,
    })
