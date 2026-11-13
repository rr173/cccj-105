"""Chinese/ISO date extraction with provenance spans.

The schedule job must explain *where* every date came from, so extraction
never returns a bare :class:`datetime.date`: every match keeps its original
text and its half-open character span inside the normalized body.

Supported forms (full-width digits are folded to ASCII before matching):

* full forms — ``2026年3月10日``, ``二〇二六年三月十日``,
  ``2026-03-10``, ``2026/3/10``, ``2026.3.10``;
* year-less forms — ``3月10日`` / ``3月10号``: the missing year is filled
  from the notice's publication date, never guessed from semantics;
* ranges joined by ``至`` / ``到`` / ``~`` / ``—`` / ``–`` / ``-`` where
  both endpoints are valid dates.

Only real calendar dates parse; things like ``2026年13月40日`` are rejected
so an unparseable fragment can be reported for manual review instead of
producing a silently wrong schedule.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

_CN_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_NUMERAL = re.compile(r"[〇零一二两三四五六七八九十]+")
_RANGE_CONNECTORS = ("至", "到", "~", "—", "–", "-")


class DateParseError(ValueError):
    """A fragment looked date-like but is not a valid calendar date."""


@dataclass(frozen=True)
class DateMention:
    text: str           # exact substring of the normalized body
    start: int          # half-open character span
    end: int
    iso: str            # normalized YYYY-MM-DD
    had_year: bool      # False when the year was filled from issued_on


@dataclass(frozen=True)
class DateRange:
    start: DateMention
    end: DateMention
    connector: str
    span_start: int
    span_end: int


def fold_fullwidth(text: str) -> str:
    """Fold full-width ASCII variants (digits, letters, punctuation)."""
    return unicodedata.normalize("NFKC", text)


def _cn_number(token: str) -> int | None:
    """Parse a small Chinese numeral (0..31): 三, 十, 十一, 二十, 二十三."""
    if not token:
        return None
    if all(ch in _CN_DIGITS for ch in token):
        value = 0
        for ch in token:
            value = value * 10 + _CN_DIGITS[ch]
        return value
    if "十" not in token:
        return None
    tens, _, ones = token.partition("十")
    tens_value = 1 if tens == "" else _CN_DIGITS.get(tens)
    if tens_value is None:
        return None
    if ones == "":
        ones_value = 0
    elif len(ones) == 1 and ones in _CN_DIGITS:
        ones_value = _CN_DIGITS[ones]
    else:
        return None
    return tens_value * 10 + ones_value


def _cn_year(token: str) -> int | None:
    if len(token) != 4 or any(ch not in _CN_DIGITS for ch in token):
        return None
    value = 0
    for ch in token:
        value = value * 10 + _CN_DIGITS[ch]
    return value


def _build_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise DateParseError(f"not a calendar date: {year}-{month}-{day}") from exc


# Each pattern is tried in order; named groups carry the numeric pieces.
# Chinese numerals are matched loosely first and validated afterwards so that
# 十/二十/三十一 style months and days all work.
_CN_FULL = re.compile(
    r"(?P<y>[〇零一二两三四五六七八九]{4})年"
    r"(?P<m>[〇零一二两三四五六七八九十]{1,3})月"
    r"(?P<d>[〇零一二两三四五六七八九十]{1,3})[日号]"
)
_CN_YEARLESS = re.compile(
    r"(?<!\d)(?<!年)(?P<m>[〇零一二两三四五六七八九十]{1,3})月"
    r"(?P<d>[〇零一二两三四五六七八九十]{1,3})[日号]"
)
_NUM_FULL = re.compile(
    r"(?P<y>\d{4})[年\-\./](?P<m>\d{1,2})[月\-\./](?P<d>\d{1,2})日?"
)
_NUM_YEARLESS = re.compile(
    r"(?<!\d)(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"
)


def _find_cn(folded: str, original: str, pattern: re.Pattern,
             issued_year: int, full: bool) -> list[DateMention]:
    out: list[DateMention] = []
    for match in pattern.finditer(folded):
        y_token = match.group("y") if full else None
        m_token, d_token = match.group("m"), match.group("d")
        year = _cn_year(y_token) if y_token else issued_year
        month, day = _cn_number(m_token), _cn_number(d_token)
        if year is None or month is None or day is None:
            raise DateParseError(
                f"unparseable Chinese date fragment: {original[match.start():match.end()]}")
        parsed = _build_date(year, month, day)
        out.append(DateMention(
            text=original[match.start():match.end()],
            start=match.start(), end=match.end(),
            iso=parsed.isoformat(), had_year=full))
    return out


def _find_num(folded: str, original: str, pattern: re.Pattern,
              issued_year: int, full: bool) -> list[DateMention]:
    out: list[DateMention] = []
    for match in pattern.finditer(folded):
        year = int(match.group("y")) if full else issued_year
        month, day = int(match.group("m")), int(match.group("d"))
        parsed = _build_date(year, month, day)
        out.append(DateMention(
            text=original[match.start():match.end()],
            start=match.start(), end=match.end(),
            iso=parsed.isoformat(), had_year=full))
    return out


def find_dates(text: str, issued_on: str) -> list[DateMention]:
    """Find every date mention in ``text`` ordered by position.

    ``issued_on`` is an ISO ``YYYY-MM-DD`` string; its year fills year-less
    forms.  Overlapping matches (a full form also matching the year-less
    pattern) are de-duplicated by span, full forms winning.
    """
    if not issued_on or len(issued_on) < 4:
        raise DateParseError(
            "cannot fill an omitted year without an issue date")
    issued_year = int(issued_on[:4])
    folded = fold_fullwidth(text)
    mentions: list[DateMention] = []
    mentions += _find_cn(folded, text, _CN_FULL, issued_year, True)
    mentions += _find_num(folded, text, _NUM_FULL, issued_year, True)
    mentions += _find_cn(folded, text, _CN_YEARLESS, issued_year, False)
    mentions += _find_num(folded, text, _NUM_YEARLESS, issued_year, False)
    mentions.sort(key=lambda m: (m.start, -(m.end - m.start)))
    chosen: list[DateMention] = []
    for mention in mentions:
        if any(mention.start >= kept.start and mention.end <= kept.end
               and mention is not kept for kept in chosen):
            continue
        chosen.append(mention)
    chosen.sort(key=lambda m: m.start)
    return chosen


def find_ranges(text: str, issued_on: str) -> tuple[list[DateMention], list[DateRange]]:
    """Split dates into standalone mentions and two-endpoint ranges.

    Two adjacent date mentions joined (with optional surrounding whitespace)
    by one of the range connectors form a :class:`DateRange`; both endpoint
    mentions remain available individually as well for provenance.
    """
    mentions = find_dates(text, issued_on)
    ranges: list[DateRange] = []
    consumed: set[tuple[int, int]] = set()
    for earlier, later in zip(mentions, mentions[1:]):
        gap = text[earlier.end:later.start]
        stripped = gap.strip()
        if stripped not in _RANGE_CONNECTORS:
            continue
        parsed_conn = stripped
        ranges.append(DateRange(
            start=earlier, end=later, connector=parsed_conn,
            span_start=earlier.start, span_end=later.end))
        consumed.add((earlier.start, earlier.end))
        consumed.add((later.start, later.end))
    standalone = [m for m in mentions
                  if (m.start, m.end) not in consumed]
    return standalone, ranges
