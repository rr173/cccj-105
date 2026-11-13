"""Build a reproducible sample workdir for the eligibility pre-screen job.

Archives one notice family from the local source ``eligsample`` into a
complete identity-schema pipeline state (``pipeline.sqlite3`` plus
content-addressed raw blobs under ``archive/``):

* E-9001 full edition v1 — the clause below binds this exact version;
* E-9001 full edition v2 — a later version observed at the **same URL**, so it
  is version 2 of the *same* archive identity whose team requirement changed
  from *at least 2* to *at least 3* doctorate holders;
* E-9001 public edition — the **same notice number** as a second, distinct
  archive identity, which lacks the team sentence entirely: a citation
  aimed at the wrong variant is detected;
* E-9002 — a second notice with its own corpus of notice sentences, used to
  build clauses with mismatched quotes for the negative tests.

The E-9001 body deliberately contains the phrase ``博士学位`` more than once
so citation tests can show that a quote found at *other* positions does not
make a shifted span acceptable.

The builder only *creates* the sample workdir; the job treats it as
read-only input.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .archive import RawArchive
from .engine import business_digest
from .htmlparse import ADAPTERS, parse_html
from .identity import derive_archive_id
from .store import Store
from .urlnorm import normalize_url

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_HTML_DIR = ROOT / "scenario" / "eligibility" / "eligsample"
SOURCE_ID = "eligsample"
BASE_URL = "http://eligsample.local/notices/2026"

SAMPLES: list[dict] = [
    {"key": "E-9001", "url": f"{BASE_URL}/E-9001?view=full",
     "file": "E-9001-full-v1.html", "version": 1,
     "note": "完整版 v1：团队至少 2 名博士且各满 3 年"},
    {"key": "E-9001", "url": f"{BASE_URL}/E-9001?view=full",
     "file": "E-9001-full-v2.html", "version": 2,
     "note": "完整版 v2（同一身份）：团队门槛改为至少 3 名博士"},
    {"key": "E-9001", "url": f"{BASE_URL}/E-9001?edition=public",
     "file": "E-9001-public.html", "version": 1,
     "note": "公众版：同编号另一变体，缺团队条件句"},
    {"key": "E-9002", "url": f"{BASE_URL}/E-9002",
     "file": "E-9002.html", "version": 1,
     "note": "另一通告，用于错位引文/串档负例"},
]


def build_sample_workdir(workdir: str | Path,
                         fetched_at: datetime | None = None) -> dict:
    """Create a fresh ``pipeline.sqlite3`` + ``archive/`` for the samples.

    An existing sample pipeline database is removed first so the builder is
    rerunnable; this builder owns the sample workdir.  The eligibility job
    itself never writes here.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "pipeline.sqlite3"
    if db_path.exists():
        db_path.unlink()
    store = Store(db_path)
    archive = RawArchive(workdir / "archive")
    moment = fetched_at or datetime(2026, 2, 10, tzinfo=timezone.utc)
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
        store.add_version(source_id=SOURCE_ID, archive_id=archive_id,
                          notice_key=sample["key"], url_key=url_key,
                          version_no=sample["version"], title=record.title,
                          issued_on=record.issued_on, body=record.body,
                          canonical_url=None, raw_digest=raw_digest,
                          content_digest=hashlib.sha256(
                              record.body.encode("utf-8")).hexdigest(),
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


def aid_for(built: dict, notice_key: str, file: str) -> str:
    for version in built["versions"]:
        if version["notice_key"] == notice_key and version["file"] == file:
            return version["archive_id"]
    raise KeyError((notice_key, file))


def version_for(built: dict, notice_key: str, file: str) -> dict:
    for version in built["versions"]:
        if version["notice_key"] == notice_key and version["file"] == file:
            return version
    raise KeyError((notice_key, file))


def span_of(body: str, quote: str, occurrence: int = 0) -> dict:
    """Locate the n-th occurrence of a quote and return a citation object."""
    cursor = -1
    for _ in range(occurrence + 1):
        cursor = body.find(quote, cursor + 1)
    if cursor < 0:
        raise KeyError(quote)
    return {"start": cursor, "end": cursor + len(quote), "quote": quote}
