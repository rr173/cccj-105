"""Declarative configuration for the funding-table structuring job.

A single plain-JSON document declares how funding tables are recognized and
how their headers map onto business fields; the job never guesses a mapping
from cell positions.  Example::

    {
      "select": [
        {"class": "funding-table"},
        {"id": "funding-main"},
        {"caption_pattern": "资助"}
      ],
      "headers": {
        "institution": ["机构", "承担单位"],
        "project":     ["资助项目", "拨付项目"],
        "amount":      ["资助金额", "拨付经费"],
        "total":       ["合计", "总计"]
      },
      "ignore_headers": ["备注", "说明"]
    }

* ``select`` (required, non-empty): a table is selected when ANY rule
  matches; inside one rule EVERY given key must match.  ``id`` compares the
  table's ``id`` attribute exactly, ``class`` is a token of the ``class``
  attribute, and ``caption_pattern`` is a regular expression searched in the
  table's ``<caption>`` text.
* ``headers.institution`` / ``project`` / ``amount`` (required, non-empty
  lists): exact-match aliases for header cells.  Matching normalizes the
  cell text (whitespace removed, full-width digits folded) and ignores one
  parenthesized unit annotation such as ``(万元)``.  ``headers.total``
  (default ``["合计", "总计"]``) labels the totals row.
* ``ignore_headers`` (default ``["备注", "说明"]``): aliases for columns
  that may be safely ignored.  Any column whose header matches none of the
  aliases rejects the whole table — fields are never taken by position.

The configuration digest (SHA-256 of the canonical JSON) is recorded on
every outcome and fact, so a rejection can always be traced back to the
exact configuration that produced it, and a fixed configuration triggers
reprocessing of previously rejected tables.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


class FundingConfigError(ValueError):
    """Raised when the funding-tables configuration is invalid."""


@dataclass(frozen=True)
class SelectRule:
    table_id: str | None = None
    class_contains: str | None = None
    caption_pattern: re.Pattern[str] | None = None

    def matches(self, table_id: str, classes: list[str], caption: str) -> bool:
        if self.table_id is not None and table_id != self.table_id:
            return False
        if self.class_contains is not None and self.class_contains not in classes:
            return False
        if self.caption_pattern is not None and not self.caption_pattern.search(caption):
            return False
        return True


@dataclass(frozen=True)
class FundingTablesConfig:
    select: tuple[SelectRule, ...]
    institution_aliases: tuple[str, ...]
    project_aliases: tuple[str, ...]
    amount_aliases: tuple[str, ...]
    total_aliases: tuple[str, ...]
    ignore_aliases: tuple[str, ...]
    digest: str
    canonical_json: str

    def selects(self, table_id: str, classes: list[str], caption: str) -> bool:
        return any(rule.matches(table_id, classes, caption) for rule in self.select)


def _require(mapping: dict, key: str, where: str):
    if key not in mapping:
        raise FundingConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _alias_list(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v.strip() for v in value
    ):
        raise FundingConfigError(f"{where} must be a list of non-empty strings")
    aliases = tuple(v.strip() for v in value)
    if len(set(aliases)) != len(aliases):
        raise FundingConfigError(f"{where} contains duplicates")
    return aliases


def load_funding_tables_config(path: str | Path) -> FundingTablesConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FundingConfigError(f"funding tables config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FundingConfigError(f"funding tables config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise FundingConfigError("funding tables config must be a JSON object")

    select_raw = _require(raw, "select", "config")
    if not isinstance(select_raw, list) or not select_raw:
        raise FundingConfigError("config.select must be a non-empty list")
    rules: list[SelectRule] = []
    for index, item in enumerate(select_raw):
        where = f"config.select[{index}]"
        if not isinstance(item, dict) or not item:
            raise FundingConfigError(f"{where} must be a non-empty object")
        unknown = set(item) - {"id", "class", "caption_pattern"}
        if unknown:
            raise FundingConfigError(f"{where}: unknown keys {sorted(unknown)}")
        compiled = None
        pattern = item.get("caption_pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or not pattern:
                raise FundingConfigError(
                    f"{where}.caption_pattern must be a non-empty string")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise FundingConfigError(
                    f"{where}.caption_pattern is not a valid regex: {exc}") from exc
        for key in ("id", "class"):
            if key in item and (not isinstance(item[key], str) or not item[key]):
                raise FundingConfigError(f"{where}.{key} must be a non-empty string")
        rules.append(SelectRule(item.get("id"), item.get("class"), compiled))

    headers = _require(raw, "headers", "config")
    if not isinstance(headers, dict):
        raise FundingConfigError("config.headers must be an object")
    institution = _alias_list(
        _require(headers, "institution", "config.headers"), "config.headers.institution")
    project = _alias_list(
        _require(headers, "project", "config.headers"), "config.headers.project")
    amount = _alias_list(
        _require(headers, "amount", "config.headers"), "config.headers.amount")
    total = _alias_list(headers.get("total", ["合计", "总计"]), "config.headers.total")
    ignore = _alias_list(raw.get("ignore_headers", ["备注", "说明"]),
                         "config.ignore_headers")

    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return FundingTablesConfig(
        select=tuple(rules),
        institution_aliases=institution,
        project_aliases=project,
        amount_aliases=amount,
        total_aliases=total,
        ignore_aliases=ignore,
        digest=digest,
        canonical_json=canonical,
    )
