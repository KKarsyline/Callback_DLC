def is_self_anchor_bucket(bucket):
    metadata = bucket.get("metadata") or {}
    if metadata.get("self_anchor"):
        return True
    vocabulary = {"自我", "self_anchor", "first_person_anchor", "first-person-anchor"}
    for field in ("tags", "bucket_tags", "domain", "profile_kind", "bucket_profile_kind",
                  "anchor_kind", "kind", "source"):
        values = metadata.get(field) or []
        if isinstance(values, str):
            values = values.split(",")
        if isinstance(values, (list, tuple, set)) and any(str(entry).strip().lower() in vocabulary for entry in values):
            return True
    return False
