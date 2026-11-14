"""Build a reproducible sample workdir for the XLIFF intake job.

Archives four notice versions from the local source ``xliffsim`` into a
complete identity-schema pipeline state (``pipeline.sqlite3`` plus
content-addressed raw blobs under ``archive/``):

* X-4001 full edition — the target notice of most review files;
* X-4001 public edition — the **same notice number** as a second, distinct
  archive identity (a misbound file must never borrow this variant's body,
  and a file bound here must not borrow the full edition's body);
* X-4002 v1 — the original supplement;
* X-4002 v2 — a later version observed at the **same URL**, so it is version
  2 of the same archive identity; an intake bound to v1 must never follow
  the newer version automatically.

The sample builder writes ``scenario/xliff/pipeline.json`` with the derived
``archive_id`` values so the static mapping fixtures can be checked against
the built state.  It only *creates* the sample workdir; the intake job treats
it as read-only input.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .archive import RawArchive
from .engine import business_digest
from .htmlparse import ADAPTERS, parse_html
from .identity import derive_archive_id
from .store import Store
from .urlnorm import normalize_url

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_HTML_DIR = ROOT / "scenario" / "xliff" / "notices"
SOURCE_ID = "xliffsim"
BASE_URL = "http://xliffsim.local/notices"

SAMPLES: list[dict] = [
    {"key": "X-4001", "url": f"{BASE_URL}/X-4001?view=full",
     "file": "X-4001-application.html", "version": 1,
     "note": "完整版：申报时间、项目代号、限额等六段正文"},
    {"key": "X-4001", "url": f"{BASE_URL}/X-4001?edition=public",
     "file": "X-4001-public.html", "version": 1,
     "note": "公众版：同编号另一变体，缺少第二、四段"},
    {"key": "X-4002", "url": f"{BASE_URL}/X-4002",
     "file": "X-4002-supplement-v1.html", "version": 1,
     "note": "补充通告，截止时间 2026-08-15"},
    {"key": "X-4002", "url": f"{BASE_URL}/X-4002",
     "file": "X-4002-supplement-v2.html", "version": 2,
     "note": "同一身份的后续版本，截止时间 2026-09-15"},
]


def build_sample_workdir(workdir: str | Path,
                         fetched_at: datetime | None = None) -> dict:
    """Create a fresh ``pipeline.sqlite3`` + ``archive/`` for the samples.

    An existing sample pipeline database is removed first so the builder is
    rerunnable.  The XLIFF intake job itself never writes here.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "pipeline.sqlite3"
    if db_path.exists():
        db_path.unlink()
    store = Store(db_path)
    archive = RawArchive(workdir / "archive")
    moment = fetched_at or datetime(2026, 5, 6, tzinfo=timezone.utc)
    stamp = moment.isoformat(timespec="seconds")
    scan_id = store.create_scan(SOURCE_ID, stamp)
    adapter = ADAPTERS["layout_a"]
    built: list[dict] = []
    for sample in SAMPLES:
        raw = (SAMPLE_HTML_DIR / sample["file"]).read_bytes()
        raw_digest = hashlib.sha256(raw).hexdigest()
        url_key = normalize_url(sample["url"], frozenset())
        archive_id = derive_archive_id(SOURCE_ID, url_key)
        archive.store(source_id=SOURCE_ID, digest=raw_digest, raw=raw,
                      url=sample["url"], final_url=sample["url"], status=200,
                      headers={"content-type": "text/html; charset=utf-8"},
                      scan_id=scan_id, kind="detail", fetched_at=moment)
        root, _notes = parse_html(raw.decode("utf-8"))
        record = adapter.parse_detail(root, sample["url"])
        store.ensure_identity(SOURCE_ID, archive_id,
                              notice_key=sample["key"], created_at=stamp)
        store.mark_identity_active(SOURCE_ID, archive_id, url_key,
                                   notice_key=sample["key"])
        store.add_version(
            source_id=SOURCE_ID, archive_id=archive_id,
            notice_key=sample["key"], url_key=url_key,
            version_no=sample["version"], title=record.title,
            issued_on=record.issued_on, body=record.body,
            canonical_url=None, raw_digest=raw_digest,
            content_digest=hashlib.sha256(record.body.encode()).hexdigest(),
            business_digest=business_digest(
                record.title, record.issued_on, record.body),
            scan_id=scan_id, created_at=stamp)
        built.append({"source_id": SOURCE_ID, "notice_key": sample["key"],
                      "archive_id": archive_id, "url": url_key,
                      "version_no": sample["version"],
                      "title": record.title, "issued_on": record.issued_on,
                      "body": record.body, "raw_digest": raw_digest,
                      "file": sample["file"], "note": sample["note"]})
    store.finish_scan(scan_id, finished_at=stamp, status="done", complete=True,
                      paging_loop=False, stop_reason="completed", page_count=1,
                      detail_count=len(built), requests=[], pages=[])
    store.close()
    return {"source_id": SOURCE_ID, "scan_id": scan_id, "versions": built}


def version_of(built: dict, notice_key: str, file: str) -> dict:
    for version in built["versions"]:
        if version["notice_key"] == notice_key and version["file"] == file:
            return version
    raise KeyError((notice_key, file))


def aid_of(built: dict, notice_key: str, file: str) -> str:
    return version_of(built, notice_key, file)["archive_id"]


def span_of(body: str, quote: str, occurrence: int = 0) -> dict:
    """Locate the n-th occurrence of a quote and return a half-open span."""
    cursor = -1
    for _ in range(occurrence + 1):
        cursor = body.find(quote, cursor + 1)
    if cursor < 0:
        raise KeyError(quote)
    return {"start": cursor, "end": cursor + len(quote)}


def write_pipeline_manifest(workdir: str | Path, built: dict) -> Path:
    """Write derived ids/bodies where fixture authors and tests can read them."""
    path = Path(workdir) / "pipeline.json"
    payload = {
        "source_id": built["source_id"],
        "versions": [
            {"notice_key": version["notice_key"],
             "file": version["file"],
             "archive_id": version["archive_id"],
             "version_no": version["version_no"],
             "url": version["url"],
             "body": version["body"]}
            for version in built["versions"]
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path
