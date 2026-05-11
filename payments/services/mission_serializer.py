def serialize_mission_for_firestore(mission):
    return {
        "mission_id": mission.mission_id,
        "level": mission.level.level,
        "title": mission.title,
        "progress_mode": mission.progress_mode,
        "time_scope": mission.time_scope,
        "resolution_mode": mission.resolution_mode,
        "banner": mission.banner_url,
        "start_time": int(mission.start_time.timestamp()),
        "end_time": int(mission.end_time.timestamp()),
        "progress_definition": mission.progress_definition,
        "target_value": mission.target_value,
        "rewards": mission.rewards,
        "status": mission.status,
        "hash_code": mission.hash_code,
    }
