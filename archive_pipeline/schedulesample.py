"""Build a reproducible sample workdir for the schedule-organizing job.

The sample archives notices from two local sources (``schedsim`` and
``schedsim2``) as a complete identity-schema pipeline state:
``pipeline.sqlite3`` plus content-addressed raw blobs under ``archive/``.
It covers every situation the organizer must handle:

* S-1001 — one notice scheduling **two rounds** of the same project;
* S-1002 — a **cross-year** window (open in 2026, deadline/review in 2027);
* S-1003 v1/v2 — two versions of one supplement that **only extends the
  material deadline**; the old date survives in the ledger but leaves the
  current query;
* S-1004 — a **whole-round withdrawal** (round 2) while round 1 continues;
* S-1005 — dates written **without a year**, filled from the issue date;
* S-1006 — a single review node **cancelled without killing the round**,
  plus S-1008 a correction that changes that review date first;
* S-1007 full/public — the **same notice number twice in one source**
  (two archive identities); S-1014 then cites that bare number and must be
  held as ``target_ambiguous`` rather than picking one edition;
* S-1009 → schedsim2/X-2001 — an **external, explicitly sourced** reference
  proving cross-source numbering is resolved by (source, number);
* S-1010 — vague wording with no concrete date (stays pending);
* S-1011 — Chinese-numeral dates;
* S-1012 — a node phrase absent from the default vocabulary (pending until
  the vocabulary is extended).

The builder only *creates* the sample workdir; the schedule job treats it as
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
SAMPLE_HTML_DIR = ROOT / "scenario" / "schedule"
ADAPTER = ADAPTERS["layout_a"]

# source, notice_key, url, html file (relative to scenario/schedule),
# version_no — later entries with the same URL form a version chain.
SAMPLES: list[dict] = [
    {"source": "schedsim", "key": "S-1001",
     "url": "http://schedsim.local/notices/S-1001",
     "file": "schedsim/S-1001-application.html", "version": 1},
    {"source": "schedsim", "key": "S-1002",
     "url": "http://schedsim.local/notices/S-1002",
     "file": "schedsim/S-1002-crossyear.html", "version": 1},
    {"source": "schedsim", "key": "S-1003",
     "url": "http://schedsim.local/notices/S-1003",
     "file": "schedsim/S-1003-supplement-v1.html", "version": 1},
    {"source": "schedsim", "key": "S-1003",
     "url": "http://schedsim.local/notices/S-1003",
     "file": "schedsim/S-1003-supplement-v2.html", "version": 2},
    {"source": "schedsim", "key": "S-1004",
     "url": "http://schedsim.local/notices/S-1004",
     "file": "schedsim/S-1004-withdraw-round.html", "version": 1},
    {"source": "schedsim", "key": "S-1005",
     "url": "http://schedsim.local/notices/S-1005",
     "file": "schedsim/S-1005-noyear.html", "version": 1},
    {"source": "schedsim", "key": "S-1006",
     "url": "http://schedsim.local/notices/S-1006",
     "file": "schedsim/S-1006-application.html", "version": 1},
    {"source": "schedsim", "key": "S-1007",
     "url": "http://schedsim.local/notices/S-1007?view=full",
     "file": "schedsim/S-1007-full.html", "version": 1},
    {"source": "schedsim", "key": "S-1007",
     "url": "http://schedsim.local/notices/S-1007?edition=public",
     "file": "schedsim/S-1007-public.html", "version": 1},
    {"source": "schedsim", "key": "S-1008",
     "url": "http://schedsim.local/notices/S-1008",
     "file": "schedsim/S-1008-correction.html", "version": 1},
    {"source": "schedsim", "key": "S-1009",
     "url": "http://schedsim.local/notices/S-1009",
     "file": "schedsim/S-1009-crosssource.html", "version": 1},
    {"source": "schedsim", "key": "S-1010",
     "url": "http://schedsim.local/notices/S-1010",
     "file": "schedsim/S-1010-vague.html", "version": 1},
    {"source": "schedsim", "key": "S-1011",
     "url": "http://schedsim.local/notices/S-1011",
     "file": "schedsim/S-1011-cnnumeral.html", "version": 1},
    {"source": "schedsim", "key": "S-1012",
     "url": "http://schedsim.local/notices/S-1012",
     "file": "schedsim/S-1012-newrole.html", "version": 1},
    {"source": "schedsim", "key": "S-1013",
     "url": "http://schedsim.local/notices/S-1013",
     "file": "schedsim/S-1013-node-cancel.html", "version": 1},
    {"source": "schedsim", "key": "S-1014",
     "url": "http://schedsim.local/notices/S-1014",
     "file": "schedsim/S-1014-ambiguous.html", "version": 1},
    {"source": "schedsim2", "key": "X-2001",
     "url": "http://schedsim2.local/notices/X-2001",
     "file": "schedsim2/X-2001-application.html", "version": 1},
]


def build_sample_workdir(workdir: str | Path,
                         fetched_at: datetime | None = None) -> dict:
    """Create ``pipeline.sqlite3`` + ``archive/`` for the sample notices."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    store = Store(workdir / "pipeline.sqlite3")
    archive = RawArchive(workdir / "archive")
    moment = fetched_at or datetime(2026, 11, 10, tzinfo=timezone.utc)
    stamp = moment.isoformat(timespec="seconds")
    built: list[dict] = []
    scans: dict[str, int] = {}

    for sample in SAMPLES:
        source_id = sample["source"]
        if source_id not in scans:
            scans[source_id] = store.create_scan(source_id, stamp)
        scan_id = scans[source_id]
        html_path = SAMPLE_HTML_DIR / sample["file"]
        raw = html_path.read_bytes()
        raw_digest = hashlib.sha256(raw).hexdigest()
        url_key = normalize_url(sample["url"], frozenset())
        archive_id = derive_archive_id(source_id, url_key)
        archive.store(source_id=source_id, digest=raw_digest, raw=raw,
                      url=sample["url"], final_url=sample["url"], status=200,
                      headers={"content-type": "text/html; charset=utf-8"},
                      scan_id=scan_id, kind="detail", fetched_at=moment)
        root, _notes = parse_html(raw.decode("utf-8"))
        record = ADAPTER.parse_detail(root, sample["url"])
        store.ensure_identity(source_id, archive_id,
                              notice_key=sample["key"], created_at=stamp)
        store.mark_identity_active(source_id, archive_id, url_key,
                                   notice_key=sample["key"])
        store.add_version(
            source_id=source_id, archive_id=archive_id,
            notice_key=sample["key"], url_key=url_key,
            version_no=sample["version"], title=record.title,
            issued_on=record.issued_on, body=record.body,
            canonical_url=None, raw_digest=raw_digest,
            content_digest=hashlib.sha256(record.body.encode()).hexdigest(),
            business_digest=business_digest(
                record.title, record.issued_on, record.body),
            scan_id=scan_id, created_at=stamp)
        built.append({"source_id": source_id, "notice_key": sample["key"],
                      "archive_id": archive_id, "url": url_key,
                      "version_no": sample["version"],
                      "issued_on": record.issued_on,
                      "raw_digest": raw_digest, "file": sample["file"]})

    for source_id, scan_id in scans.items():
        count = sum(1 for b in built if b["source_id"] == source_id)
        store.finish_scan(scan_id, finished_at=stamp, status="done",
                          complete=True, paging_loop=False,
                          stop_reason="completed", page_count=1,
                          detail_count=count, requests=[], pages=[])
    store.close()
    return {"sources": sorted(scans), "versions": built}


def aid_for(built: dict, source_id: str, notice_key: str,
            url_suffix: str | None = None) -> str:
    """Look up the archive_id of one built sample version."""
    for version in built["versions"]:
        if (version["source_id"] == source_id
                and version["notice_key"] == notice_key
                and (url_suffix is None or url_suffix in version["url"])):
            return version["archive_id"]
    raise KeyError((source_id, notice_key, url_suffix))
