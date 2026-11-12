"""Content-addressed raw response archive.

Each response is written to ``<archive>/<source>/<yyyymmdd>/<sha256>.bin`` where
the name is the SHA-256 of the exact bytes received.  The file is immutable: if
the digest already exists nothing is rewritten.  Metadata JSON is stored beside
it under the same digest so a report can explain which URL/status/headers/scan
produced the bytes.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

MAX_STORE_BYTES = 5_000_000  # bytes above this are gzipped on disk


def _stamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%d")


class RawArchive:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def store(
        self,
        *,
        source_id: str,
        digest: str,
        raw: bytes,
        url: str,
        final_url: str,
        status: int,
        headers: dict[str, str],
        scan_id: int,
        kind: str,
        fetched_at: datetime | None = None,
    ) -> dict:
        moment = fetched_at or datetime.now(timezone.utc)
        directory = self.root / source_id / _stamp(moment)
        directory.mkdir(parents=True, exist_ok=True)
        blob_path = directory / f"{digest}.bin"
        compressed = False
        if not blob_path.exists():
            if len(raw) > MAX_STORE_BYTES:
                blob_path = directory / f"{digest}.bin.gz"
                compressed = True
                payload = gzip.compress(raw, mtime=0)
            else:
                payload = raw
            tmp = blob_path.with_suffix(blob_path.suffix + ".tmp")
            with open(tmp, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, blob_path)
        meta_path = directory / f"{digest}.json"
        if not meta_path.exists():
            meta = {
                "digest_algorithm": "sha256",
                "digest": digest,
                "bytes": len(raw),
                "source_id": source_id,
                "url": url,
                "final_url": final_url,
                "status": status,
                "content_type": headers.get("content-type", ""),
                "etag": headers.get("etag", ""),
                "last_modified": headers.get("last-modified", ""),
                "scan_id": scan_id,
                "kind": kind,
                "fetched_at": moment.isoformat(),
                "storage": "gzip" if compressed else "identity",
            }
            tmp = meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, meta_path)
        return {
            "digest": digest,
            "path": str(blob_path),
            "meta_path": str(meta_path),
            "bytes": len(raw),
            "compressed": compressed,
        }

    def load(self, digest: str) -> bytes:
        matches = list(self.root.rglob(f"{digest}.bin")) + list(
            self.root.rglob(f"{digest}.bin.gz")
        )
        if not matches:
            raise FileNotFoundError(f"no archived blob for digest {digest}")
        path = matches[0]
        raw = path.read_bytes()
        return gzip.decompress(raw) if path.suffix == ".gz" else raw

    def metadata(self, digest: str) -> dict | None:
        matches = list(self.root.rglob(f"{digest}.json"))
        if not matches:
            return None
        return json.loads(matches[0].read_text(encoding="utf-8"))
