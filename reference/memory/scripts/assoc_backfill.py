"""Bounded command-line backfill for associative memory cues.

Imports the application server lazily so importing this script has no service
side effects. Usage: python scripts/assoc_backfill.py --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import json


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Atelier associative cue index")
    parser.add_argument("--limit", type=int, default=100, help="maximum moments to process")
    parser.add_argument("--bucket",action="append",default=[])
    parser.add_argument("--plan-only", action="store_true", help="show the requested bounded run")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.plan_only:
        print(json.dumps({"plan_only": True, "limit": args.limit}, ensure_ascii=False))
        return 0

    # Keep application imports inside main; use the server's existing config and clients.
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        import server
        from assoc_index import AssocIndex
    except Exception as exc:
        parser.error(f"could not initialize Atelier server components: {exc}")
    index = AssocIndex(
        server.config, server.memory_moment_store, server.bucket_mgr,
        server.raw_event_store, server.embedding_engine, server.dehydrator,
    )
    result = asyncio.run(index.rebuild(args.limit,bucket_ids=args.bucket))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not result.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
