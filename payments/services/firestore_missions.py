from .firestore_client import get_firestore
from .mission_serializer import serialize_mission_for_firestore


def push_mission_to_firestore(mission):
    db = get_firestore()

    payload = serialize_mission_for_firestore(mission)

    db.collection("missionData") \
      .document(mission.mission_id) \
      .set(payload)
