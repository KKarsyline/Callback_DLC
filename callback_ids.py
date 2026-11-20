import re


def valid_moment_id(value, bucket_id):
    if not isinstance(value, str) or not isinstance(bucket_id, str):
        return False
    ordinary = r"[A-Za-z0-9_-]{1,200}"
    if not re.fullmatch(ordinary, bucket_id):
        return False
    return bool(re.fullmatch(ordinary, value) or re.fullmatch(re.escape(bucket_id) + ":[0-9a-f]{16}", value))
