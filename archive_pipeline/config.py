"""Configuration loading and validation for the notice archive pipeline.

The configuration is deliberately plain JSON so the pipeline has no third-party
dependencies.  Every source declares its allowed origin(s); the crawler will
never contact a host/port outside that set, not even for a canonical URL or a
redirect target.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SUPPORTED_ADAPTERS = {"layout_a", "layout_b"}
ROBOTS_UNAVAILABLE_POLICIES = {"allow_all", "disallow_all"}


class ConfigError(ValueError):
    """Raised when a configuration file is missing fields or is inconsistent."""


def _origin_tuple(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ConfigError(f"unsupported scheme in origin {url!r}")
    if parts.username or parts.password:
        raise ConfigError(f"userinfo is not allowed in origin {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ConfigError(f"origin has no host: {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    display_name: str
    base_url: str
    entry_point: str
    adapter: str
    allowed_origins: tuple[tuple[str, str, int], ...]
    tracking_params: frozenset[str]
    min_interval_ms: int
    robots_unavailable_policy: str
    directory_pattern: re.Pattern[str]
    detail_pattern: re.Pattern[str]
    max_pages_per_scan: int
    max_redirects: int
    timeout_seconds: float
    max_response_bytes: int

    def origin_allowed(self, scheme: str, host: str, port: int) -> bool:
        return (scheme, host.lower(), port) in self.allowed_origins

    def classify(self, path: str) -> str:
        """Return "directory" or "detail" for a normalized path."""
        if self.directory_pattern.search(path):
            return "directory"
        return "detail"


@dataclass(frozen=True)
class Config:
    user_agent: str
    archive_dir: Path
    sources: tuple[SourceConfig, ...]

    def source(self, source_id: str) -> SourceConfig:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise KeyError(source_id)


def _require(mapping: dict, key: str, where: str):
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def load_config(path: str | Path, archive_dir_override: str | Path | None = None) -> Config:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {exc}") from exc

    user_agent = _require(raw, "user_agent", "config")
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ConfigError("config.user_agent must be a non-empty string")

    archive_dir = Path(archive_dir_override or raw.get("archive_dir", "archive"))
    if not archive_dir.is_absolute():
        archive_dir = (path.parent / archive_dir).resolve()

    sources_raw = _require(raw, "sources", "config")
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ConfigError("config.sources must be a non-empty list")

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(sources_raw):
        where = f"config.sources[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be an object")
        source_id = _require(item, "source_id", where)
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9_\-]+", source_id):
            raise ConfigError(f"{where}.source_id must be a lowercase slug")
        if source_id in seen:
            raise ConfigError(f"duplicate source_id {source_id!r}")
        seen.add(source_id)

        adapter = _require(item, "adapter", where)
        if adapter not in SUPPORTED_ADAPTERS:
            raise ConfigError(
                f"{where}.adapter must be one of {sorted(SUPPORTED_ADAPTERS)}, got {adapter!r}"
            )
        base_url = _require(item, "base_url", where)
        entry_point = _require(item, "entry_point", where)
        origins_raw = _require(item, "allowed_origins", where)
        if not isinstance(origins_raw, list) or not origins_raw:
            raise ConfigError(f"{where}.allowed_origins must be a non-empty list")
        origins = tuple(_origin_tuple(origin) for origin in origins_raw)
        if len(set(origins)) != len(origins):
            raise ConfigError(f"{where}.allowed_origins contains duplicates")

        entry_origin = _origin_tuple(entry_point)
        if entry_origin not in origins:
            raise ConfigError(f"{where}.entry_point is outside allowed_origins")
        base_origin = _origin_tuple(base_url)
        if base_origin not in origins:
            raise ConfigError(f"{where}.base_url is outside allowed_origins")

        tracking_raw = item.get("tracking_params", [])
        if not isinstance(tracking_raw, list) or any(
            not isinstance(v, str) or not v for v in tracking_raw
        ):
            raise ConfigError(f"{where}.tracking_params must be a list of strings")
        tracking = frozenset(v.lower() for v in tracking_raw)

        min_interval = item.get("min_interval_ms", 250)
        if not isinstance(min_interval, int) or min_interval < 0:
            raise ConfigError(f"{where}.min_interval_ms must be a non-negative integer")

        policy = item.get("robots_unavailable_policy", "disallow_all")
        if policy not in ROBOTS_UNAVAILABLE_POLICIES:
            raise ConfigError(
                f"{where}.robots_unavailable_policy must be one of "
                f"{sorted(ROBOTS_UNAVAILABLE_POLICIES)}"
            )

        def compile_pattern(key: str, default: str) -> re.Pattern[str]:
            value = item.get(key, default)
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{where}.{key} must be a non-empty regex")
            try:
                return re.compile(value)
            except re.error as exc:
                raise ConfigError(f"{where}.{key} is not a valid regex: {exc}") from exc

        directory_pattern = compile_pattern("directory_pattern", r"^/notices/(?:page/.*)?$")
        detail_pattern = compile_pattern("detail_pattern", r".*")
        if directory_pattern.search("/") and detail_pattern.search("/"):
            # Not fatal: classification gives directories precedence, but a
            # pattern that matches everything makes provenance confusing.
            raise ConfigError(
                f"{where}: directory_pattern and detail_pattern both match '/'; "
                "classify directory pages precisely"
            )

        max_pages = item.get("max_pages_per_scan", 200)
        if not isinstance(max_pages, int) or max_pages <= 0:
            raise ConfigError(f"{where}.max_pages_per_scan must be a positive integer")
        max_redirects = item.get("max_redirects", 5)
        if not isinstance(max_redirects, int) or max_redirects < 0:
            raise ConfigError(f"{where}.max_redirects must be a non-negative integer")
        timeout = float(item.get("timeout_seconds", 5.0))
        if timeout <= 0:
            raise ConfigError(f"{where}.timeout_seconds must be positive")
        max_bytes = item.get("max_response_bytes", 2_000_000)
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ConfigError(f"{where}.max_response_bytes must be a positive integer")

        sources.append(
            SourceConfig(
                source_id=source_id,
                display_name=str(item.get("display_name", source_id)),
                base_url=base_url,
                entry_point=entry_point,
                adapter=adapter,
                allowed_origins=origins,
                tracking_params=tracking,
                min_interval_ms=min_interval,
                robots_unavailable_policy=policy,
                directory_pattern=directory_pattern,
                detail_pattern=detail_pattern,
                max_pages_per_scan=max_pages,
                max_redirects=max_redirects,
                timeout_seconds=timeout,
                max_response_bytes=max_bytes,
            )
        )

    return Config(user_agent=user_agent, archive_dir=archive_dir, sources=tuple(sources))
