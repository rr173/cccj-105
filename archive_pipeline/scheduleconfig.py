"""Declarative vocabulary configuration for the schedule-organizing job.

One plain-JSON document (``config/schedule_vocab.json``) declares how the
organizer recognizes notice types and schedule-node roles; the job never
hard-codes a business phrase in its parser::

    {
      "notice_types": {
        "application": ["申报通知", ...],
        "supplement":  ["补充通知", "延期通知", ...],
        "correction":  ["更正通知", ...],
        "withdrawal":  ["撤销通知", ...]
      },
      "roles": {
        "apply_open":        {"kind": "open",     "aliases": ["报名开放", ...]},
        "submit_deadline":   {"kind": "deadline", "aliases": ["材料提交截止", ...]},
        "supplement_deadline":{"kind": "deadline","aliases": ["补正材料截止", ...]},
        "review_start":      {"kind": "open",     "aliases": ["专家评审开始", ...]}
      },
      "round_markers": {"first": ["第一轮", ...], "second": ["第二轮", ...], ...},
      "target_ref":    {"explicit_labels": [...], "project_code_pattern": "..."},
      "cancellation":  {"round_phrases": [...], "node_phrases": [...]}
    }

The configuration digest (SHA-256 of the canonical JSON) is stamped on every
extraction, pending item and ledger row.  Changing the vocabulary therefore
produces a new, separately traceable set of judgments while every old
judgment — including its reason — remains in the history tables.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

NOTICE_TYPES = ("application", "supplement", "correction", "withdrawal")
ROLE_KINDS = ("open", "deadline")


class ScheduleConfigError(ValueError):
    """Raised when the schedule vocabulary configuration is invalid."""


@dataclass(frozen=True)
class RoleSpec:
    name: str
    kind: str                       # open / deadline
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleVocab:
    notice_type_aliases: dict[str, tuple[str, ...]]
    roles: tuple[RoleSpec, ...]
    round_first: tuple[str, ...]
    round_second: tuple[str, ...]
    round_third: tuple[str, ...]
    default_round: int
    target_labels: tuple[str, ...]
    source_labels: tuple[str, ...]
    implicit_suffixes: tuple[str, ...]
    project_code_pattern: re.Pattern[str]
    cancel_round_phrases: tuple[str, ...]
    cancel_node_phrases: tuple[str, ...]
    digest: str
    canonical_json: str

    def role_by_alias(self, phrase: str) -> RoleSpec | None:
        for spec in self.roles:
            if phrase in spec.aliases:
                return spec
        return None

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.roles)

    def role_spec(self, name: str) -> RoleSpec | None:
        for spec in self.roles:
            if spec.name == name:
                return spec
        return None


def _alias_list(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v.strip() for v in value
    ):
        raise ScheduleConfigError(f"{where} must be a list of non-empty strings")
    aliases = tuple(v.strip() for v in value)
    if len(set(aliases)) != len(aliases):
        raise ScheduleConfigError(f"{where} contains duplicate aliases")
    return aliases


def _check_no_alias_overlap(groups: dict[str, tuple[str, ...]], where: str) -> None:
    seen: dict[str, str] = {}
    for group, aliases in groups.items():
        for alias in aliases:
            if alias in seen:
                raise ScheduleConfigError(
                    f"{where}: alias {alias!r} is claimed by both "
                    f"{seen[alias]!r} and {group!r}")
            seen[alias] = group


def load_schedule_vocab(path: str | Path) -> ScheduleVocab:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScheduleConfigError(f"schedule vocab config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScheduleConfigError(
            f"schedule vocab config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScheduleConfigError("schedule vocab config must be a JSON object")

    types_raw = raw.get("notice_types")
    if not isinstance(types_raw, dict) or not types_raw:
        raise ScheduleConfigError("config.notice_types must be a non-empty object")
    notice_types: dict[str, tuple[str, ...]] = {}
    for ntype in NOTICE_TYPES:
        if ntype not in types_raw:
            raise ScheduleConfigError(
                f"config.notice_types missing required key {ntype!r}")
        notice_types[ntype] = _alias_list(types_raw[ntype],
                                          f"config.notice_types.{ntype}")
    unknown_types = set(types_raw) - set(NOTICE_TYPES)
    if unknown_types:
        raise ScheduleConfigError(
            f"config.notice_types has unknown keys: {sorted(unknown_types)}")
    _check_no_alias_overlap(notice_types, "config.notice_types")

    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, dict) or not roles_raw:
        raise ScheduleConfigError("config.roles must be a non-empty object")
    roles: list[RoleSpec] = []
    role_alias_groups: dict[str, tuple[str, ...]] = {}
    for name, spec_raw in roles_raw.items():
        where = f"config.roles.{name}"
        if not isinstance(spec_raw, dict):
            raise ScheduleConfigError(f"{where} must be an object")
        kind = spec_raw.get("kind")
        if kind not in ROLE_KINDS:
            raise ScheduleConfigError(
                f"{where}.kind must be one of {ROLE_KINDS}, got {kind!r}")
        aliases = _alias_list(spec_raw.get("aliases"), f"{where}.aliases")
        roles.append(RoleSpec(name=name, kind=kind, aliases=aliases))
        role_alias_groups[name] = aliases
    _check_no_alias_overlap(role_alias_groups, "config.roles")

    rounds_raw = raw.get("round_markers")
    if not isinstance(rounds_raw, dict):
        raise ScheduleConfigError("config.round_markers must be an object")
    round_groups: dict[str, tuple[str, ...]] = {}
    for key in ("first", "second", "third"):
        if key not in rounds_raw:
            raise ScheduleConfigError(
                f"config.round_markers missing required key {key!r}")
        round_groups[key] = _alias_list(rounds_raw[key],
                                        f"config.round_markers.{key}")
    _check_no_alias_overlap(round_groups, "config.round_markers")
    default_round = rounds_raw.get("default_round", 1)
    if not isinstance(default_round, int) or default_round < 1:
        raise ScheduleConfigError(
            "config.round_markers.default_round must be a positive integer")

    target_raw = raw.get("target_ref")
    if not isinstance(target_raw, dict):
        raise ScheduleConfigError("config.target_ref must be an object")
    target_labels = _alias_list(target_raw.get("explicit_labels", []),
                                "config.target_ref.explicit_labels")
    source_labels = _alias_list(target_raw.get("source_labels", []),
                                "config.target_ref.source_labels")
    implicit_suffixes = _alias_list(target_raw.get("implicit_suffixes", []),
                                    "config.target_ref.implicit_suffixes")
    pattern_text = target_raw.get("project_code_pattern")
    if not isinstance(pattern_text, str) or not pattern_text:
        raise ScheduleConfigError(
            "config.target_ref.project_code_pattern must be a non-empty string")
    try:
        project_pattern = re.compile(pattern_text)
    except re.error as exc:
        raise ScheduleConfigError(
            f"config.target_ref.project_code_pattern is not a valid regex: {exc}"
        ) from exc

    cancel_raw = raw.get("cancellation")
    if not isinstance(cancel_raw, dict):
        raise ScheduleConfigError("config.cancellation must be an object")
    cancel_round = _alias_list(cancel_raw.get("round_phrases", []),
                               "config.cancellation.round_phrases")
    cancel_node = _alias_list(cancel_raw.get("node_phrases", []),
                              "config.cancellation.node_phrases")

    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ScheduleVocab(
        notice_type_aliases=notice_types,
        roles=tuple(roles),
        round_first=round_groups["first"],
        round_second=round_groups["second"],
        round_third=round_groups["third"],
        default_round=default_round,
        target_labels=target_labels,
        source_labels=source_labels,
        implicit_suffixes=implicit_suffixes,
        project_code_pattern=project_pattern,
        cancel_round_phrases=cancel_round,
        cancel_node_phrases=cancel_node,
        digest=digest,
        canonical_json=canonical,
    )
