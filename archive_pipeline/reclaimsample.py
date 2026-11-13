"""Reproducible synthetic work directory for reclamation demos and tests.

The scenario is built into a fresh directory and contains:

* an unreferenced blob under **two date directories** with the same digest;
* one response referenced only by a scan item;
* one response referenced by an archived version;
* an old unreferenced orphan (past grace) and a fresh unreferenced orphan
  (inside grace);
* an incomplete group (blob without metadata);
* a group with a corrupt gzip container;
* a digest-mismatched group;
* metadata ownership conflicts (wrong source, wrong byte count);
* a symlink and an unrecognised path;
* a gzip response that is unreferenced and valid, to prove gzip is judged by
  decompressed bytes.

Everything is written with explicit mtimes so "old" and "new" are real
filesystem facts.  No scanner is involved; the database facts are inserted
directly so the demo is hermetic.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .engine import business_digest
from .store import Store

SOURCE = "sim_a"
OLD = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
OLD_STAMP = "20260110"
NEW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
NEW_STAMP = "20261113"


def _set_mtime(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def _write(directory: Path, digest: str, payload: bytes, metadata: dict,
           moment: datetime, suffix: str = ".bin") -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    blob = directory / f"{digest}{suffix}"
    blob.write_bytes(payload)
    meta = directory / f"{digest}.json"
    meta.write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    _set_mtime(blob, moment)
    _set_mtime(meta, moment)
    return blob, meta


def _meta(digest: str, raw: bytes, url: str, source_id: str = SOURCE,
          moment: datetime = OLD, storage: str = "identity",
          status: int = 200, **overrides) -> dict:
    payload = {
        "digest_algorithm": "sha256",
        "digest": digest,
        "bytes": len(raw),
        "source_id": source_id,
        "url": url,
        "final_url": url,
        "status": status,
        "content_type": "text/html; charset=utf-8",
        "etag": "",
        "last_modified": "",
        "scan_id": 1,
        "kind": "detail",
        "fetched_at": moment.isoformat(),
        "storage": storage,
    }
    payload.update(overrides)
    return payload


def build_scenario(workdir: str | Path) -> dict:
    """Create the work directory + archive and return the digest catalogue."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    archive_root = workdir / "archive"
    store = Store(workdir / "pipeline.sqlite3")
    scan_id = store.create_scan(SOURCE, "2026-01-10T12:00:00+00:00")
    stamp = "2026-01-10T12:00:00+00:00"
    old_dir = archive_root / SOURCE / OLD_STAMP
    new_dir = archive_root / SOURCE / NEW_STAMP
    catalogue: dict[str, dict] = {}

    def body(name: str) -> bytes:
        return (f"<html><body><article class='notice' data-key='{name}'>"
                f"<h1>{name}</h1><div class='body'><p>body for {name}</p>"
                f"</div></article></body></html>").encode()

    # 1. Version-referenced response (protected even though its copy is old).
    version_raw = body("A-1001")
    version_digest = hashlib.sha256(version_raw).hexdigest()
    url = "http://127.0.0.1:8811/notices/same-url/A-1001"
    _write(old_dir, version_digest, version_raw,
           _meta(version_digest, version_raw, url), OLD)
    store.ensure_identity(SOURCE, "arc_version", notice_key="A-1001",
                          created_at=stamp)
    store.add_version(source_id=SOURCE, archive_id="arc_version",
                      notice_key="A-1001", url_key=url, version_no=1,
                      title="A-1001", issued_on="2026-01-01",
                      body="body for A-1001", canonical_url=None,
                      raw_digest=version_digest,
                      content_digest=hashlib.sha256(b"body for A-1001").hexdigest(),
                      business_digest=business_digest("A-1001", "2026-01-01",
                                                      "body for A-1001"),
                      scan_id=scan_id, created_at=stamp)
    catalogue["version_referenced"] = {
        "digest": version_digest, "path": f"{SOURCE}/{OLD_STAMP}/{version_digest}.bin"}

    # 2. Response referenced only by a scan item (no version).
    scan_raw = body("A-2002")
    scan_digest = hashlib.sha256(scan_raw).hexdigest()
    scan_url = "http://127.0.0.1:8811/notices/draft/A-2002"
    _write(old_dir, scan_digest, scan_raw,
           _meta(scan_digest, scan_raw, scan_url), OLD)
    store.add_scan_item(scan_id=scan_id, source_id=SOURCE, url_key=scan_url,
                        kind="detail", outcome="ok", http_status=200,
                        raw_digest=scan_digest, archive_id="arc_scan_only",
                        document_key="A-2002", detail={"note": "scan-only"})
    catalogue["scan_referenced"] = {
        "digest": scan_digest, "path": f"{SOURCE}/{OLD_STAMP}/{scan_digest}.bin"}

    # 3. Same digest is referenced -> all copies protected (cross-date copies).
    twin_raw = body("A-3003")
    twin_digest = hashlib.sha256(twin_raw).hexdigest()
    twin_url = "http://127.0.0.1:8811/notices/twin/A-3003"
    _write(old_dir, twin_digest, twin_raw,
           _meta(twin_digest, twin_raw, twin_url), OLD)
    # A later copy in a different date directory whose metadata looks "newer".
    _write(new_dir, twin_digest, twin_raw,
           _meta(twin_digest, twin_raw, twin_url, moment=NEW), NEW)
    store.add_version(source_id=SOURCE, archive_id="arc_twin",
                      notice_key="A-3003", url_key=twin_url, version_no=1,
                      title="A-3003", issued_on="2026-01-02",
                      body="body for A-3003", canonical_url=None,
                      raw_digest=twin_digest,
                      content_digest=hashlib.sha256(b"body for A-3003").hexdigest(),
                      business_digest=business_digest("A-3003", "2026-01-02",
                                                      "body for A-3003"),
                      scan_id=scan_id, created_at=stamp)
    catalogue["cross_date_protected"] = {
        "digest": twin_digest,
        "paths": [f"{SOURCE}/{OLD_STAMP}/{twin_digest}.bin",
                  f"{SOURCE}/{NEW_STAMP}/{twin_digest}.bin"]}

    # 4. Old unreferenced orphan -- the canonical isolate candidate.
    orphan_raw = body("ORPHAN-OLD")
    orphan_digest = hashlib.sha256(orphan_raw).hexdigest()
    orphan_url = "http://127.0.0.1:8811/notices/orphan/ORPHAN-OLD"
    _write(old_dir, orphan_digest, orphan_raw,
           _meta(orphan_digest, orphan_raw, orphan_url), OLD)
    catalogue["old_orphan"] = {
        "digest": orphan_digest, "path": f"{SOURCE}/{OLD_STAMP}/{orphan_digest}.bin"}

    # 5. Fresh unreferenced orphan -- inside grace, must never be isolated.
    fresh_raw = body("ORPHAN-FRESH")
    fresh_digest = hashlib.sha256(fresh_raw).hexdigest()
    fresh_url = "http://127.0.0.1:8811/notices/orphan/ORPHAN-FRESH"
    _write(new_dir, fresh_digest, fresh_raw,
           _meta(fresh_digest, fresh_raw, fresh_url, moment=NEW), NEW)
    catalogue["fresh_orphan"] = {
        "digest": fresh_digest, "path": f"{SOURCE}/{NEW_STAMP}/{fresh_digest}.bin"}

    # 6. Gzip orphan: digest is over the *decompressed* bytes.
    gz_raw = body("ORPHAN-GZ")
    gz_digest = hashlib.sha256(gz_raw).hexdigest()
    gz_url = "http://127.0.0.1:8811/notices/orphan/ORPHAN-GZ"
    _write(old_dir, gz_digest, gzip.compress(gz_raw, mtime=0),
           _meta(gz_digest, gz_raw, gz_url, storage="gzip"), OLD,
           suffix=".bin.gz")
    catalogue["gzip_orphan"] = {
        "digest": gz_digest, "path": f"{SOURCE}/{OLD_STAMP}/{gz_digest}.bin.gz"}

    # 7. Incomplete group: blob with no metadata.
    incomplete_raw = body("INCOMPLETE")
    incomplete_digest = hashlib.sha256(incomplete_raw).hexdigest()
    (old_dir / f"{incomplete_digest}.bin").write_bytes(incomplete_raw)
    _set_mtime(old_dir / f"{incomplete_digest}.bin", OLD)
    catalogue["incomplete_group"] = {
        "digest": incomplete_digest,
        "path": f"{SOURCE}/{OLD_STAMP}/{incomplete_digest}.bin"}

    # 8. Corrupt gzip container claiming a digest.
    corrupt_raw = body("CORRUPT-GZ")
    corrupt_digest = hashlib.sha256(corrupt_raw).hexdigest()
    _write(old_dir, corrupt_digest, b"\x1f\x8b\x08\x00not-a-gzip-stream",
           _meta(corrupt_digest, corrupt_raw,
                 "http://127.0.0.1:8811/notices/orphan/CORRUPT-GZ",
                 storage="gzip"), OLD, suffix=".bin.gz")
    catalogue["corrupt_gzip"] = {
        "digest": corrupt_digest,
        "path": f"{SOURCE}/{OLD_STAMP}/{corrupt_digest}.bin.gz"}

    # 9. Digest mismatch: file content does not hash to its name.
    mismatch_raw = body("MISMATCH")
    mismatch_digest = hashlib.sha256(mismatch_raw).hexdigest()
    _write(old_dir, mismatch_digest, b"different bytes",
           _meta(mismatch_digest, mismatch_raw,
                 "http://127.0.0.1:8811/notices/orphan/MISMATCH"), OLD)
    catalogue["digest_mismatch"] = {
        "digest": mismatch_digest,
        "path": f"{SOURCE}/{OLD_STAMP}/{mismatch_digest}.bin"}

    # 10. Metadata ownership conflicts (wrong source; wrong size).
    owner_raw = body("OWNER")
    owner_digest = hashlib.sha256(owner_raw).hexdigest()
    _write(old_dir, owner_digest, owner_raw,
           _meta(owner_digest, owner_raw,
                 "http://127.0.0.1:8811/notices/orphan/OWNER",
                 source_id="sim_b"), OLD)
    catalogue["metadata_source_conflict"] = {
        "digest": owner_digest, "path": f"{SOURCE}/{OLD_STAMP}/{owner_digest}.bin"}
    size_raw = body("SIZE")
    size_digest = hashlib.sha256(size_raw).hexdigest()
    _write(old_dir, size_digest, size_raw,
           _meta(size_digest, size_raw,
                 "http://127.0.0.1:8811/notices/orphan/SIZE", bytes=999999),
           OLD)
    catalogue["metadata_size_conflict"] = {
        "digest": size_digest, "path": f"{SOURCE}/{OLD_STAMP}/{size_digest}.bin"}

    # 11. Symlink and unrecognised path.
    symlink_target = old_dir / f"{version_digest}.bin"
    symlink_path = old_dir / "deadbeef.bin"
    symlink_path.symlink_to(symlink_target)
    catalogue["symlink"] = {"path": f"{SOURCE}/{OLD_STAMP}/deadbeef.bin"}
    foreign_path = new_dir / "notes.txt"
    foreign_path.write_text("not an archived response", encoding="utf-8")
    _set_mtime(foreign_path, NEW)
    catalogue["foreign_path"] = {"path": f"{SOURCE}/{NEW_STAMP}/notes.txt"}

    store.finish_scan(scan_id, finished_at=stamp, status="complete",
                      complete=True, paging_loop=False, stop_reason="completed",
                      page_count=1, detail_count=1, requests=[], pages=[])
    store.close()
    return {
        "workdir": str(workdir),
        "archive_root": str(archive_root),
        "scan_id": scan_id,
        "old": OLD, "new": NEW,
        "entries": catalogue,
    }
