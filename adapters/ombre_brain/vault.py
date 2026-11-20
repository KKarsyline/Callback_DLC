"""A conservative local projection. Never import the OB server or open its databases."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from threading import Lock

import yaml


_MAX_BYTES = 1024 * 1024
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FLAGS = set("resolved pinned protected anchor dont_surface digested".split())
_FIELDS = _FLAGS | set("""
    id type name title tags domain created last_active activation_count importance
    valence arousal model_valence first_of_kind source_tool imported grow_batch_id
    why_remembered meaning media quotes provenance source_refs source_links relation_links last_merged_by
""".split())
_PRIVATE = {"self", "i", "you", "them", "feel", "letter", "plan", "自我",
            "self_anchor", "first_person_anchor", "first-person-anchor"}


class _Header(yaml.SafeLoader):
    """Reject aliases, merges and duplicate keys in policy-bearing metadata."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("unsupported YAML alias")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("ambiguous YAML keys")
        return super().construct_mapping(node, deep=deep)


def _bindings_visible(meta):
    """A detached component vetoes this bucket only, including mixed buckets."""
    projections = {}
    for field in ("source_links", "source_refs"):
        if field not in meta:
            continue
        items = meta[field]
        if not isinstance(items, list) or len(items) > 32:
            return False
        bindings = set()
        for item in items:
            fields = {"ref", "ranges", "status"} if field == "source_links" else {"ref", "ranges"}
            if not isinstance(item, dict) or item.keys() - fields:
                return False
            if field == "source_links" and item.get("status") != "active":
                return False
            ref = item.get("ref")
            if not isinstance(ref, str) or not re.fullmatch(r"src_[a-f0-9]{64}", ref):
                return False
            ranges = item.get("ranges", [])
            if not isinstance(ranges, list) or len(ranges) > 128:
                return False
            for pair in ranges:
                if (not isinstance(pair, list) or len(pair) != 2
                        or any(type(v) is not int for v in pair) or not 1 <= pair[0] <= pair[1]):
                    return False
            binding = (ref, tuple(map(tuple, ranges)))
            if binding in bindings:
                return False
            bindings.add(binding)
        projections[field] = bindings
    return len(projections) < 2 or projections["source_links"] == projections["source_refs"]


def _visible(meta, kind):
    if meta.keys() - _FIELDS or meta.get("type") != kind:
        return False
    if meta.get("source_tool", "direct") not in ("direct", "hold", "grow"):
        return False
    if any(meta.get(key, False) is not False for key in _FLAGS):
        return False
    for key in ("tags", "domain"):
        labels = meta.get(key, [])
        if isinstance(labels, str):
            labels = labels.split(",")
        if not isinstance(labels, list):
            return False
        for label in labels:
            if not isinstance(label, str):
                return False
            value = label.strip().casefold()
            if value.startswith("__") or value in _PRIVATE:
                return False
    return _bindings_visible(meta)


def _valid_id(value):
    return (isinstance(value, str) and value not in (".", "..")
            and re.fullmatch(r"[\w.-]{1,160}", value) is not None)


class PolicyUnavailable(RuntimeError):
    """The host could not supply a complete, understood authorization policy."""


def _valid_policy(policy):
    if not isinstance(policy, dict) or policy.keys() - {"dynamic_ids", "archives", "updates"}:
        return False
    ids = policy.get("dynamic_ids")
    if not isinstance(ids, list) or not all(_valid_id(bid) for bid in ids):
        return False
    archives = policy.get("archives", {})
    if not isinstance(archives, dict):
        return False
    for path, grant in archives.items():
        if (not isinstance(path, str) or "\x00" in path or not path.endswith(".md")
                or any(part in ("", ".", "..") for part in path.split("/"))
                or not isinstance(grant, dict) or grant.keys() != {"id", "original_type", "sha256"}
                or not _valid_id(grant["id"]) or grant["original_type"] != "dynamic"
                or not isinstance(grant["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", grant["sha256"])):
            return False
    updates = policy.get("updates", [])
    return isinstance(updates, list) and all(
        isinstance(pair, list) and len(pair) == 2 and all(_valid_id(bid) for bid in pair)
        and pair[0] != pair[1] for pair in updates
    )


def _read_bucket(directory, name, kind):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_BYTES:
            return None
        data = stream.read(_MAX_BYTES + 1)
        after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not os.path.samestat(info, after) or info.st_ctime_ns != after.st_ctime_ns
                or len(data) != info.st_size):
            return None
    if len(data) > _MAX_BYTES:
        return None
    text = data.decode("utf-8").replace("\r\n", "\n")
    header = re.match(r"\A---\n(.*?)^---(?:\n|\Z)", text, re.S | re.M)
    if not header:
        return None
    meta = yaml.load(header[1], Loader=_Header)
    if not isinstance(meta, dict):
        return None
    bid = meta.get("id")
    if not _valid_id(bid):
        return None
    body = text[header.end():].strip()
    if not body or not _visible(meta, kind):
        return bid, None
    created = meta.get("created")
    if isinstance(created, (datetime, date)):
        created = created.isoformat()
    if created is not None:
        if not isinstance(created, str):
            return bid, None
        try:
            datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return bid, None
    # Only expose the bucket body. A source_ref.path would let core reopen the
    # Markdown with surrounding lines, potentially including private frontmatter.
    # Core's raw-event port accepts integers. This local surrogate identifies
    # a bucket binding, never a shared src_<hash> file or a transcript event.
    source_key = int.from_bytes(hashlib.sha256(bid.encode()).digest()[:8], "big") >> 1
    return bid, {"id": bid, "content": body, "metadata": {
        "type": kind, "created": created or "",
        "source_raw_event_ids": [source_key],
        "ob_revision": hashlib.sha256(data).hexdigest(),
    }}


class OmbreVault:
    """One trusted local buckets_dir, for a single audience and core state."""

    def __init__(self, buckets_dir, *, read_policy=None):
        root = Path(buckets_dir).absolute()
        if root.is_symlink() or not (root / "dynamic").is_dir() or (root / "dynamic").is_symlink():
            raise ValueError("expected a vault with a regular dynamic directory")
        self.base_dir = str(root.resolve(strict=True))
        self.moments = _Moments(self)
        self._observed_sources = set()
        self._sources_lock = Lock()
        self.read_policy = read_policy

    def _scan(self, tree, grants=None):
        rows = {}
        # Re-open from the configured root for every list and lookup. Keep no
        # positive cache that could survive suppression, moves, or deletion.
        root = os.open(self.base_dir, _DIR)
        try:
            try:
                folder = os.open(tree, _DIR, dir_fd=root)
            except FileNotFoundError:
                return {}
            try:
                for location, dirs, names, directory in os.fwalk(".", dir_fd=folder, follow_symlinks=False):
                    dirs[:] = [d for d in dirs if location == "." and not d.startswith((".", "_"))
                               and d.casefold() not in _PRIVATE | {"archive", "letters", "plans", "permanent"}]
                    for name in names:
                        if name.startswith((".", "_")) or not name.endswith(".md"):
                            continue
                        grant = None
                        if grants is not None:
                            grant = grants.get(str(Path(location) / name))
                            if grant is None:
                                continue
                        try:
                            row = _read_bucket(directory, name, "archived" if grant else "dynamic")
                        except (OSError, ValueError, yaml.YAMLError, OverflowError, RecursionError):
                            continue
                        if row:
                            bid, bucket = row
                            if grant and (not bucket or grant.get("id") != bid or
                                          grant.get("sha256") != bucket["metadata"]["ob_revision"]):
                                bucket = None
                            rows[bid] = None if bid in rows else bucket
            finally:
                os.close(folder)
        finally:
            os.close(root)
        return rows

    def _view(self):
        policy = None
        if self.read_policy is not None:
            try:
                policy = deepcopy(self.read_policy())
            except Exception as exc:
                raise PolicyUnavailable("could not read authorization policy") from exc
            if not _valid_policy(policy):
                raise PolicyUnavailable("invalid authorization policy")
        rows = self._scan("dynamic")
        if policy is not None:
            allowed = set(policy["dynamic_ids"])
            rows = {bid: b if bid in allowed else None for bid, b in rows.items()}
            if policy.get("archives"):
                for bid, archived in self._scan("archive", policy["archives"]).items():
                    rows[bid] = None if bid in rows else archived
        visible = {bid: rows[bid] for bid in sorted(rows) if rows[bid] is not None}
        edges = []
        for pair in (policy or {}).get("updates", []):
            if all(bid in visible for bid in pair):
                edges.append({"source": pair[0], "target": pair[1], "relation_type": "updates"})
                # A removed edge must invalidate previously validated outcome evidence.
                for bid in pair:
                    visible[bid]["metadata"].setdefault("authored_updates", []).append(pair)
        return visible, edges

    def list_edges(self):
        return self._view()[1]

    async def list_edges_async(self):
        return await asyncio.to_thread(self.list_edges)

    def _buckets(self):
        visible, _ = self._view()
        keys = self._source_keys(visible)
        with self._sources_lock:
            self._observed_sources.update(keys)
        return visible

    @staticmethod
    def _source_keys(rows):
        return {b["metadata"]["source_raw_event_ids"][0] for b in rows.values()}

    def superseded_ids(self):
        current = self._source_keys(self._buckets())
        with self._sources_lock:
            return self._observed_sources.difference(current)

    async def superseded_ids_async(self):
        return await asyncio.to_thread(self.superseded_ids)

    async def list_all(self, include_archive=False):
        buckets = await asyncio.to_thread(self._buckets)
        return [b for b in buckets.values() if include_archive or b["metadata"]["type"] != "archived"]

    def _get(self, bucket_id):
        try:
            return self._buckets().get(bucket_id)
        except PolicyUnavailable:
            return None

    async def get(self, bucket_id):
        return await asyncio.to_thread(self._get, bucket_id)


class _Moments:
    def __init__(self, vault):
        self.vault = vault

    @staticmethod
    def _body(bucket):
        bid = bucket["id"]
        return {"moment_id": bid + ":body", "bucket_id": bid, "section": "body",
                "source_id": bid, "text": bucket["content"], "metadata": {}}

    def get(self, moment_id):
        bid, sep, section = moment_id.rpartition(":")
        if sep and section == "body":
            bucket = self.vault._get(bid)
            if bucket:
                return self._body(bucket)
        return None

    def list_all(self, limit=None):
        buckets = list(self.vault._buckets().values())
        return [self._body(b) for b in buckets[:limit]]

    async def get_async(self, moment_id):
        return await asyncio.to_thread(self.get, moment_id)

    async def list_all_async(self, limit=None):
        return await asyncio.to_thread(self.list_all, limit)


@asynccontextmanager
async def host_recall(vault, *, state_dir, model, embeddings):
    """Open state dedicated by the host to this collection and audience."""
    from service import open_service

    state = Path(state_dir).resolve()
    root = Path(vault.base_dir)
    if state == root or root in state.parents or state in root.parents:
        raise ValueError("use a dedicated state directory separate from the vault")
    if state.exists():
        for entry in state.iterdir():
            if entry.is_symlink() or not entry.is_file() or entry.stat().st_nlink != 1:
                raise ValueError("expected a dedicated state directory with regular files")
    async with open_service(state_dir=state, moments=vault.moments, buckets=vault, raw_events=vault,
                            embeddings=embeddings, model=model, list_edges=vault.list_edges,
                            list_edges_async=vault.list_edges_async) as service:
        yield service


@asynccontextmanager
async def local_recall(buckets_dir, *, scratch_parent=None, dehydrator=None, embedding_engine=None):
    """Use new private, disposable SQLite state outside the vault on each run."""
    vault = OmbreVault(buckets_dir)
    parent = Path(scratch_parent or tempfile.gettempdir()).resolve(strict=True)
    if Path(vault.base_dir) in (parent, *parent.parents):
        raise ValueError("scratch_parent must be outside the vault")
    with tempfile.TemporaryDirectory(dir=parent, prefix="ob-recall-") as state:
        async with host_recall(vault, state_dir=state, model=dehydrator, embeddings=embedding_engine) as service:
            yield service
