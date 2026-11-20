"""Opt-in compatibility check. The only positional argument is a local upstream.git."""
import argparse
import asyncio
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from adapters.ombre_brain import local_recall
from adapters.ombre_brain.demo import QUOTE, fixture_model, validation_request
from adapters.ombre_brain.tests.test_vault import snapshot


PIN = "115d831d979bf67efc7150f976e2cb34c333c935"


async def check(scratch):
    from bucket_manager import BucketManager
    from ombrebrain.storage.source_store import SourceStore

    root = scratch / "buckets"
    manager = BucketManager({"buckets_dir": str(root)})
    source = SourceStore(root).put("Synthetic source-only transcript.")
    body = QUOTE + " beside the toolbox."
    bid = await manager.create(body, source_tool="grow", domain=["workshop"],
                               source_refs=[{"ref": source, "ranges": [[1, 1]]}],
                               quotes=[{"text": "Synthetic metadata-only quote."}])
    await manager.create("Synthetic private letter.", bucket_type="letter", locked_by="human",
                         lock_type="permanent", source_tool="letter")
    before = snapshot(root)
    async with local_recall(root, scratch_parent=scratch, dehydrator=fixture_model()) as (index, recall):
        await index.warm()
        report = await index.rebuild(limit=10)
        assert (report["scanned"], report["indexed"], report["failed"]) == (1, 1, 0), report
        await index.warm()
        hit, = await index.search(["amber lantern"])
        old = validation_request([hit])
        row, = (await recall.validate(old))["candidates"]
        assert row["event_id"] == bid and row["source_window"] == body
        assert before == snapshot(root)
        assert await manager.update(bid, content=body + " A new observation.")
        assert not (await recall.validate(old))["candidates"]
        assert (await index.rebuild(limit=10))["indexed"] == 1
        await index.warm()
        changed, = await index.search(["amber lantern"])
        assert "new observation" in changed["source_window"]
        assert await manager.update(bid, dont_surface=True)
        assert not (await recall.validate(validation_request([changed])))["candidates"]
        assert await manager.update(bid, dont_surface=False)
        assert await manager.archive(bid)
        assert not index.moment_store.list_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("git_dir", type=Path)
    repo = parser.parse_args().git_dir.resolve(strict=True)
    # Keep the pinned sources and vault temporary and out of the distributable.
    with tempfile.TemporaryDirectory(prefix="ob-writer-", dir=Path(__file__).resolve().parent) as tmp:
        scratch = Path(tmp)
        archive = subprocess.check_output(["git", "--git-dir", str(repo), "archive", PIN, "VERSION", "src"])
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(scratch, filter="data")
        sys.path.insert(0, str(scratch / "src"))
        asyncio.run(check(scratch))
    print("PASS: pinned BucketManager create/update/archive, SourceStore.put, local recall validation")


if __name__ == "__main__":
    main()
