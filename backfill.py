"""Bound a rebuild using the host's own factory (module:callable returning AssocIndex)."""
import argparse
import asyncio
import importlib
import json
from assoc_plans import refresh_plan_states


async def run(factory, limit, buckets, plans):
    module, name = factory.split(":", 1)
    index = getattr(importlib.import_module(module), name)()
    if plans:
        return await refresh_plan_states(index, limit)
    return await index.rebuild(limit, bucket_ids=buckets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("factory")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--bucket", action="append", default=[])
    parser.add_argument("--plans", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if args.limit < 0 or (args.plans and args.bucket):
        parser.error("negative limit or bucket filter with --plans")
    result = ({"limit": args.limit, "mode": "plans" if args.plans else "cues", "buckets": args.bucket}
              if args.plan_only else asyncio.run(run(args.factory, args.limit, args.bucket, args.plans)))
    print(json.dumps(result))
    raise SystemExit(int(bool(result.get("failed"))))
