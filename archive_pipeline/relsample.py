"""Reproducible sample workdir for the offline relation job.

Archives one notice family plus two unrelated notices from the local source
``relsim`` into a complete identity-schema pipeline state
(``pipeline.sqlite3`` and content-addressed raw blobs under ``archive/``):

* N-5001 full edition v1 and v2 — two versions of the **same** archive
  identity observed at the same URL (so a mention bound to v1 must never
  silently pick up v2);
* N-5001 public edition — the **same printed notice number** as a second,
  distinct archive identity; it has a different body and its own mentions;
* N-5002 — an evaluation notice that is mentioned by an organization;
* N-5003 — a maintenance notice with no organization mention at all
  (the "negative relation with no candidate" case).

The builder only creates the sample workdir.  The relation job treats it as
read-only input and never writes an archived body.
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
SAMPLE_HTML_DIR = ROOT / "scenario" / "relations" / "notices"
SOURCE_ID = "relsim"
BASE_URL = "http://relsim.local/notices"

SAMPLES: list[dict] = [
    {"key": "N-5001", "url": f"{BASE_URL}/N-5001?view=full",
     "file": "N-5001-v1.html", "version": 1,
     "note": "完整版 v1：首批开放两个方向"},
    {"key": "N-5001", "url": f"{BASE_URL}/N-5001?view=full",
     "file": "N-5001-v2.html", "version": 2,
     "note": "完整版 v2（同一身份）：首批开放三个方向"},
    {"key": "N-5001", "url": f"{BASE_URL}/N-5001?edition=public",
     "file": "N-5001-public.html", "version": 1,
     "note": "同编号另一变体，正文不同"},
    {"key": "N-5002", "url": f"{BASE_URL}/N-5002",
     "file": "N-5002.html", "version": 1,
     "note": "联合数据评估通知"},
    {"key": "N-5003", "url": f"{BASE_URL}/N-5003",
     "file": "N-5003.html", "version": 1,
     "note": "设备维护通知，无机构提及"},
]


def build_sample_workdir(workdir: str | Path,
                         fetched_at: datetime | None = None) -> dict:
    """Create a fresh ``pipeline.sqlite3`` + ``archive/`` for the samples.

    An existing sample pipeline database is removed first so the builder is
    rerunnable; the builder owns the sample workdir.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "pipeline.sqlite3"
    if db_path.exists():
        db_path.unlink()
    store = Store(db_path)
    archive = RawArchive(workdir / "archive")
    moment = fetched_at or datetime(2026, 6, 2, tzinfo=timezone.utc)
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
    result = {"source_id": SOURCE_ID, "scan_id": scan_id, "versions": built}
    write_pipeline_manifest(workdir, result)
    return result


def version_of(built: dict, notice_key: str, file: str) -> dict:
    for version in built["versions"]:
        if version["notice_key"] == notice_key and version["file"] == file:
            return version
    raise KeyError((notice_key, file))


def aid_of(built: dict, notice_key: str, file: str) -> str:
    return version_of(built, notice_key, file)["archive_id"]


def write_pipeline_manifest(workdir: str | Path, built: dict) -> Path:
    path = Path(workdir) / "pipeline.json"
    payload = {
        "source_id": built["source_id"],
        "versions": [
            {"notice_key": version["notice_key"],
             "file": version["file"],
             "archive_id": version["archive_id"],
             "version_no": version["version_no"],
             "url": version["url"],
             "title": version["title"],
             "body": version["body"],
             "note": version["note"]}
            for version in built["versions"]],
    }
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False,
                                             indent=2), encoding="utf-8")
    return path
