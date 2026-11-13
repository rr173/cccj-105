"""Build a reproducible sample workdir for the funding-table job.

The sample archives eleven notice pages (raw HTML kept under
``scenario/funding/``) as a miniature but complete pipeline state: an
identity-schema ``pipeline.sqlite3`` plus content-addressed raw blobs under
``archive/``.  Every case the job handles is present:

* F-3001 full edition — rowspan institutions, colspan year groups, an
  ignored 备注 column, a column-level 万元 unit overriding the table-level
  元, full-width digits, thousands separators, and a reconciling totals row;
* F-3001 public edition — same notice number, different archive identity,
  single header row, table-level 万元 unit, no totals row;
* F-3002 — overlapping merged regions (misaligned merge);
* F-3003 — one amount cell spanning two year columns;
* F-3004 — totals row disagreeing with the details;
* F-3005 — version whose raw blob is missing from the archive;
* F-3006 — archived blob whose content no longer matches its digest;
* F-3007 — header aliases missing from the default config (bookable after a
  config fix);
* F-3008 — dash / 待定 amounts (missing values, never zero);
* F-3009 — a table nested inside a cell;
* F-3010 — unknown currency (美元);
* F-3011 — no unit declared anywhere.

The builder only *creates* the sample workdir; the funding job treats it as
read-only input exactly like a real archived workdir.
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
SAMPLE_HTML_DIR = ROOT / "scenario" / "funding"
SOURCE_ID = "fundsim"
BASE_URL = "http://fundsim.local/notices/2026"

SAMPLES: list[dict] = [
    {"key": "F-3001", "file": "F-3001-full.html",
     "url": f"{BASE_URL}/F-3001?view=full",
     "note": "完整版：跨行机构、跨列年度、局部金额单位（万元）、合计核对"},
    {"key": "F-3001", "file": "F-3001-public.html",
     "url": f"{BASE_URL}/F-3001?edition=public",
     "note": "公众版：同编号不同身份，单层列头，表级单位万元"},
    {"key": "F-3002", "file": "F-3002-overlap.html", "url": f"{BASE_URL}/F-3002",
     "note": "错位合并：合并区域重叠"},
    {"key": "F-3003", "file": "F-3003-crossyear.html", "url": f"{BASE_URL}/F-3003",
     "note": "一个金额格横跨两个年度列"},
    {"key": "F-3004", "file": "F-3004-mismatch.html", "url": f"{BASE_URL}/F-3004",
     "note": "合计与明细差额"},
    {"key": "F-3005", "file": None, "url": f"{BASE_URL}/F-3005",
     "note": "原始留档缺失"},
    {"key": "F-3006", "file": "F-3006-valid.html", "url": f"{BASE_URL}/F-3006",
     "corrupt": True, "note": "留档内容与摘要不符"},
    {"key": "F-3007", "file": "F-3007-alias.html", "url": f"{BASE_URL}/F-3007",
     "note": "默认配置缺少标题别名，改配置后可重处理"},
    {"key": "F-3008", "file": "F-3008-dash.html", "url": f"{BASE_URL}/F-3008",
     "note": "破折号/待定金额"},
    {"key": "F-3009", "file": "F-3009-nested.html", "url": f"{BASE_URL}/F-3009",
     "note": "嵌套表格"},
    {"key": "F-3010", "file": "F-3010-foreign.html", "url": f"{BASE_URL}/F-3010",
     "note": "未知币种（美元）"},
    {"key": "F-3011", "file": "F-3011-nounit.html", "url": f"{BASE_URL}/F-3011",
     "note": "缺单位"},
]


def build_sample_workdir(workdir: str | Path, source_id: str = SOURCE_ID,
                         fetched_at: datetime | None = None) -> dict:
    """Create ``pipeline.sqlite3`` + ``archive/`` for the sample notices."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    store = Store(workdir / "pipeline.sqlite3")
    archive = RawArchive(workdir / "archive")
    moment = fetched_at or datetime(2026, 3, 2, tzinfo=timezone.utc)
    stamp = moment.isoformat(timespec="seconds")
    scan_id = store.create_scan(source_id, stamp)
    adapter = ADAPTERS["layout_a"]
    built: list[dict] = []
    for sample in SAMPLES:
        url_key = normalize_url(sample["url"], frozenset())
        archive_id = derive_archive_id(source_id, url_key)
        if sample["file"] is None:
            # The version row exists but its raw blob was never archived.
            raw = f"archived bytes for {sample['key']} were never stored".encode()
            raw_digest = hashlib.sha256(raw).hexdigest()
            title, issued, body = f"通告 {sample['key']}（原文缺失）", "2026-03-06", ""
        else:
            raw = (SAMPLE_HTML_DIR / sample["file"]).read_bytes()
            raw_digest = hashlib.sha256(raw).hexdigest()
            archive.store(source_id=source_id, digest=raw_digest, raw=raw,
                          url=sample["url"], final_url=sample["url"], status=200,
                          headers={"content-type": "text/html; charset=utf-8"},
                          scan_id=scan_id, kind="detail", fetched_at=moment)
            if sample.get("corrupt"):
                blob = (archive.root / source_id / moment.strftime("%Y%m%d")
                        / f"{raw_digest}.bin")
                blob.write_bytes(b"\x00corrupted bytes replacing the original")
            root, _notes = parse_html(raw.decode("utf-8"))
            record = adapter.parse_detail(root, sample["url"])
            title, issued, body = record.title, record.issued_on, record.body
        store.ensure_identity(source_id, archive_id, notice_key=sample["key"],
                              created_at=stamp)
        store.mark_identity_active(source_id, archive_id, url_key,
                                   notice_key=sample["key"])
        store.add_version(source_id=source_id, archive_id=archive_id,
                          notice_key=sample["key"], url_key=url_key, version_no=1,
                          title=title, issued_on=issued, body=body,
                          canonical_url=None, raw_digest=raw_digest,
                          content_digest=hashlib.sha256(body.encode()).hexdigest(),
                          business_digest=business_digest(title, issued, body),
                          scan_id=scan_id, created_at=stamp)
        built.append({"notice_key": sample["key"], "archive_id": archive_id,
                      "url": url_key, "raw_digest": raw_digest,
                      "file": sample["file"], "note": sample["note"]})
    store.finish_scan(scan_id, finished_at=stamp, status="done", complete=True,
                      paging_loop=False, stop_reason="completed", page_count=1,
                      detail_count=len(built), requests=[], pages=[])
    store.close()
    return {"source_id": source_id, "scan_id": scan_id, "versions": built}


def aid_for(built: dict, notice_key: str, file: str | None) -> str:
    """Look up the archive_id of one built sample version."""
    for version in built["versions"]:
        if version["notice_key"] == notice_key and version["file"] == file:
            return version["archive_id"]
    raise KeyError((notice_key, file))
