"""Minimal robots.txt evaluator with an explicitly documented rule subset.

Supported (and nothing else is supported; the same text is emitted into every
scan report):

* ``User-agent`` group headers.  Group selection: our product token
  (``User-Agent/1.0`` -> ``user-agent``) is matched case-insensitively against
  each group's agent; if one or more non-wildcard groups match, the wildcard
  ``*`` groups are ignored, and all matching specific groups are merged.
* ``Allow`` / ``Disallow`` path rules: literal path prefixes with ``*`` (any
  run of characters) and an optional trailing ``$`` (end anchor).  Longest
  matching pattern wins; on an exact tie ``Allow`` wins.  An empty ``Disallow:``
  is ignored (it matches nothing).
* Matching is against ``path + "?" + query`` (fragment never sent, so ignored).
  Percent-encoded octets are compared as text (no decoding).
* Comments (``#`` to end of line) and blank lines are ignored.
* ``Crawl-delay`` is parsed and reported, but **not honored**: the source's
  configured ``min_interval_ms`` is the spacing contract.
* ``Sitemap`` is ignored.
* Unavailable robots.txt (404/410/5xx/network/redirect) uses the per-source
  ``robots_unavailable_policy``: ``allow_all`` or ``disallow_all``
  (default).  A 2xx file with no applicable rules allows everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

SUPPORTED_SUBSET = (
    "User-agent groups (specific product token overrides '*'); Allow/Disallow "
    "path prefixes with '*' wildcards and trailing '$' anchor; longest match "
    "wins, ties broken by Allow; rules matched against path?query; comments "
    "ignored; Crawl-delay parsed but NOT honored (min_interval_ms is used); "
    "Sitemap ignored; unavailable robots.txt handled by the configured "
    "policy (allow_all|disallow_all)."
)


def _rule_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    parts = [".*" if char == "*" else re.escape(char) for char in body]
    expression = "".join(parts) + ("$" if anchored else "")
    return re.compile(expression)


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow?, pattern)


class RobotsPolicy:
    SUPPORTED_SUBSET = SUPPORTED_SUBSET

    def __init__(self, groups: list[Group], *, available: bool, status: str,
                 crawl_delay: str | None = None):
        self.groups = groups
        self.available = available
        self.status = status
        self.crawl_delay = crawl_delay

    @staticmethod
    def from_unavailable(status: str, policy: str) -> "RobotsPolicy":
        if policy == "allow_all":
            return RobotsPolicy([], available=False, status=status)
        return RobotsPolicy([Group(agents=["*"], rules=[(False, "/")])],
                            available=False, status=status)

    @staticmethod
    def from_text(text: str, *, status: str = "200") -> "RobotsPolicy":
        groups: list[Group] = []
        current: Group | None = None
        in_agent_block = False
        delay: str | None = None
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                if current is None or not in_agent_block:
                    current = Group()
                    groups.append(current)
                    in_agent_block = True
                current.agents.append(value.lower())
            elif key in {"allow", "disallow"}:
                if current is None:
                    current = Group(agents=["*"])
                    groups.append(current)
                in_agent_block = False
                if value:
                    current.rules.append((key == "allow", value))
            else:
                in_agent_block = False
                if key == "crawl-delay":
                    delay = value
        return RobotsPolicy(groups, available=True, status=status, crawl_delay=delay)

    def describe(self) -> list[dict]:
        return [
            {"agents": group.agents,
             "rules": [{"allow": is_allow, "pattern": pattern}
                       for is_allow, pattern in group.rules]}
            for group in self.groups
        ]

    def _select_groups(self, user_agent: str) -> list[Group]:
        token = user_agent.split("/", 1)[0].split()[0].strip().lower() if user_agent else ""
        full = user_agent.lower()
        specific = [
            group for group in self.groups
            if any(agent and agent != "*" and (agent == token or agent in full)
                   for agent in group.agents)
        ]
        if specific:
            return specific
        return [group for group in self.groups if "*" in group.agents]

    def allowed(self, url: str, user_agent: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for group in self._select_groups(user_agent):
            for is_allow, pattern in group.rules:
                if not pattern:
                    continue
                regex = _rule_regex(pattern)
                if not regex.match(target):
                    continue
                candidate = (len(pattern), is_allow)
                if best is None or candidate[0] > best[0] or (
                    candidate[0] == best[0] and candidate[1]
                ):
                    best = candidate
        return True if best is None else best[1]
