"""Extract schedule evidence from one archived notice version.

The extractor is deliberately *local*: given one version's normalized body,
its issue date and the vocabulary, it produces evidence records (which
phrases and dates were found, in which paragraph and at which character
offsets) and provisional pending items.  It never writes anything and never
decides cross-notice effects — those belong to :mod:`schedule` so the whole
replay can be re-done transactionally.

Every extracted date carries::

* the paragraph index and the half-open character span inside the body;
* the sentence text and its own span;
* the original date writing (``2026年3月10日`` / ``3月10日`` …);
* the normalized ISO date, flagged when the year was filled from
  ``issued_on``;
* the role phrase that was matched and the configured role name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .scheduleconfig import ScheduleVocab
from .scheduledate import DateMention, DateRange, DateParseError, find_ranges

_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])")
# 来源在前：来源：SRC，编号：KEY（允许“现就”等引导词插在来源与编号之间）
_EXPLICIT_REF_SRC_FIRST = re.compile(
    r"(?:来源|站点)\s*[：:]\s*(?P<src>[A-Za-z0-9\-]+)"
    r"[^。；;]{0,30}?"
    r"(?:编号|通告编号|公告编号)\s*[：:]\s*"
    r"(?P<key>[A-Za-z][A-Za-z0-9\-]*)(?![A-Za-z0-9\-])"
)
# 编号（来源）: KEY  /  编号: KEY — ASCII and full-width colons/commas.
# The number ends as soon as the next character is not an ASCII letter/digit
# or hyphen (Chinese prose, punctuation, whitespace, end of string).
# Note: Python's \w matches CJK characters, hence the explicit ASCII class.
_EXPLICIT_REF = re.compile(
    r"(?:编号|通告编号|公告编号)\s*[：:]\s*"
    r"(?:(?:来源|站点)\s*[：:]\s*(?P<src>[A-Za-z0-9\-]+)\s*[,，、\s]+)?"
    r"(?P<key>[A-Za-z][A-Za-z0-9\-]*)(?![A-Za-z0-9\-])"
)
# Same, but the source label follows the number: 编号：KEY（来源：SRC）
_EXPLICIT_REF_TRAILING_SRC = re.compile(
    r"(?:编号|通告编号|公告编号)\s*[：:]\s*(?P<key>[A-Za-z][A-Za-z0-9\-]*)"
    r"(?![A-Za-z0-9\-])[^。；;]{0,20}?"
    r"(?:来源|站点)\s*[：:]\s*(?P<src>[A-Za-z0-9\-]+)"
)
_ROUND_TOKEN = re.compile(r"第[一二三四1-4]+(?:轮|期)")
_DATE_HINT = re.compile(r"[日号]|年|月")
# In a correction, dates quoted as the previous wrong value ("原写…有误",
# "此前…作废") are evidence, not new schedule nodes.
_CORRECTION_OLD_DATE = re.compile(
    r"(?:原(?:写|来|定|本)?|此前|之前)[^。；;]{0,12}?(?:有误|错误|作废|更正|失效)"
)


@dataclass
class DateEvidence:
    role_name: str
    role_kind: str                # open / deadline
    role_phrase: str
    role_phrase_start: int
    raw_date: str
    normalized_date: str
    had_year: bool
    is_range: bool
    range_connector: str | None
    endpoint: str | None          # range start / range end / None
    paragraph_index: int
    paragraph_start: int
    sentence_start: int
    sentence_end: int
    date_start: int               # body-level half-open span
    date_end: int
    sentence_text: str
    round_no: int
    round_phrase: str | None


@dataclass
class PendingItem:
    code: str
    detail: str
    paragraph_index: int
    paragraph_start: int
    sentence_start: int
    sentence_end: int
    sentence_text: str
    target_source: str | None = None
    target_key: str | None = None
    role_name: str | None = None
    raw_date: str | None = None
    normalized_date: str | None = None
    date_start: int | None = None
    date_end: int | None = None


@dataclass
class NoticeExtraction:
    notice_type: str                      # application/supplement/...
    notice_type_phrase: str | None
    project_code: str | None
    rounds: tuple[int, ...]
    target_hints: tuple[dict, ...]       # provisional references
    dates: list[DateEvidence] = field(default_factory=list)
    pending: list[PendingItem] = field(default_factory=list)
    cancels_round: list[dict] = field(default_factory=list)
    cancels_node: list[dict] = field(default_factory=list)
    has_concrete_date: bool = False


@dataclass(frozen=True)
class Paragraph:
    index: int
    start: int          # body-level offset of the first character
    end: int
    text: str


def split_paragraphs(body: str) -> list[Paragraph]:
    """Split the normalized body on the blank lines used by the adapter."""
    paragraphs: list[Paragraph] = []
    offset = 0
    for index, chunk in enumerate(body.split("\n\n")):
        end = offset + len(chunk)
        paragraphs.append(Paragraph(index=index, start=offset, end=end,
                                    text=chunk))
        offset = end + 2  # the two-character separator
    return paragraphs


def _split_sentences(paragraph: Paragraph) -> list[tuple[int, int, str]]:
    sentences: list[tuple[int, int, str]] = []
    cursor = 0
    for piece in _SENT_SPLIT.split(paragraph.text):
        if not piece:
            continue
        start = paragraph.start + cursor
        end = start + len(piece)
        sentences.append((start, end, piece))
        cursor += len(piece)
    return sentences


def classify_notice_type(title: str, body: str,
                         vocab: ScheduleVocab) -> tuple[str, str | None]:
    # Specific types (supplement/correction/withdrawal) outrank the generic
    # "申报通知" wording, which often appears inside a supplement's title
    # quoting the original notice.  Title matches outrank body matches so a
    # document quoting another notice's title is not misclassified.
    rank = {"withdrawal": 3, "correction": 3, "supplement": 2,
            "application": 1}

    def _scan(haystack: str) -> tuple[int, int, str, str] | None:
        best = None
        for ntype, aliases in vocab.notice_type_aliases.items():
            for alias in aliases:
                pos = haystack.find(alias)
                if pos < 0:
                    continue
                candidate = (rank[ntype], len(alias), ntype, alias)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        return best

    title_hit = _scan(title)
    if title_hit is not None:
        return title_hit[2], title_hit[3]
    body_hit = _scan(body)
    if body_hit is not None:
        return body_hit[2], body_hit[3]
    return "application", None


def detect_rounds(paragraphs: list[Paragraph],
                  vocab: ScheduleVocab) -> tuple[set[int], dict[int, str]]:
    found: set[int] = set()
    phrases: dict[int, str] = {}
    groups = ((1, vocab.round_first), (2, vocab.round_second),
              (3, vocab.round_third))
    for paragraph in paragraphs:
        for token in _ROUND_TOKEN.findall(paragraph.text):
            for round_no, markers in groups:
                if token in markers:
                    found.add(round_no)
                    phrases.setdefault(round_no, token)
    return found, phrases


def find_target_hints(title: str, body: str, notice_key: str,
                      vocab: ScheduleVocab) -> list[dict]:
    hints: list[dict] = []
    haystack = f"{title}\n{body}"
    seen_refs: set[tuple[str | None, str]] = set()

    def _add_ref(match, trailing: bool) -> None:
        key = match.group("key")
        src = match.group("src")
        if (src, key) in seen_refs:
            return
        seen_refs.add((src, key))
        hints.append({"kind": "explicit", "target_source": src,
                      "target_key": key, "phrase": match.group(0),
                      "trailing_source": trailing})

    for match in _EXPLICIT_REF_SRC_FIRST.finditer(haystack):
        _add_ref(match, False)
    for match in _EXPLICIT_REF.finditer(haystack):
        _add_ref(match, False)
    for match in _EXPLICIT_REF_TRAILING_SRC.finditer(haystack):
        _add_ref(match, True)
    for code in vocab.project_code_pattern.findall(body):
        if any(h.get("code") == code for h in hints):
            continue
        hints.append({"kind": "project_code", "code": code})
    for suffix in vocab.implicit_suffixes:
        base = notice_key
        if base.endswith(suffix) and len(base) > len(suffix):
            hints.append({"kind": "implicit_key",
                          "target_source": None,
                          "target_key": base[: -len(suffix)],
                          "phrase": notice_key})
            break
    return hints


def _role_matches(sentence: str, sentence_start: int,
                  vocab: ScheduleVocab) -> list[tuple[int, int, str, object]]:
    """Non-overlapping role-phrase matches, longest alias first."""
    hits: list[tuple[int, int, str, object]] = []
    for spec in vocab.roles:
        for alias in spec.aliases:
            start = sentence.find(alias)
            while start >= 0:
                hits.append((start, start + len(alias), alias, spec))
                start = sentence.find(alias, start + len(alias))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    chosen: list[tuple[int, int, str, object]] = []
    for hit in hits:
        if any(hit[0] < other[1] and hit[1] > other[0] for other in chosen):
            continue
        chosen.append(hit)
    chosen.sort(key=lambda h: h[0])
    return [(s, e, phrase, spec) for s, e, phrase, spec in chosen]


def _nearest_role(date_start: int,
                  roles: list[tuple[int, int, str, object]]
                  ) -> tuple[int, int, str, object] | None:
    preceding = [r for r in roles if r[0] <= date_start]
    if preceding:
        return preceding[-1]
    return roles[0] if roles else None


_NEW_VALUE_MARKERS = ("应为", "更正为", "调整为", "改为", "确定为", "现确定",
                      "现更正", "现调整")


def _is_new_corrected_date(sentence: str, mention: DateMention) -> bool:
    """Whether a correction-sentence date is the new value rather than the
    quoted old one: a new-value marker appears immediately before it."""
    window = sentence[max(0, mention.start - 6):mention.start]
    return any(marker in window for marker in _NEW_VALUE_MARKERS)


def _round_in_text(text: str, start: int, end: int,
                   round_phrases: dict[int, str], vocab: ScheduleVocab
                   ) -> tuple[int, str | None]:
    window = text[start:end]
    for round_no, markers in ((1, vocab.round_first), (2, vocab.round_second),
                              (3, vocab.round_third)):
        for marker in markers:
            if marker in window:
                return round_no, marker
    return None, None


def extract_notice(*, title: str, notice_key: str, issued_on: str, body: str,
                   vocab: ScheduleVocab) -> NoticeExtraction:
    """Extract all schedule evidence from one notice version."""
    paragraphs = split_paragraphs(body)
    notice_type, type_phrase = classify_notice_type(title, body, vocab)
    round_set, round_phrases = detect_rounds(paragraphs, vocab)
    rounds = tuple(sorted(round_set)) if round_set else (vocab.default_round,)
    hints = find_target_hints(title, body, notice_key, vocab)
    code_match = vocab.project_code_pattern.search(f"{title}\n{body}")
    result = NoticeExtraction(
        notice_type=notice_type, notice_type_phrase=type_phrase,
        project_code=code_match.group(0) if code_match else None,
        rounds=rounds, target_hints=tuple(hints))

    for paragraph in paragraphs:
        current_round = rounds[0]
        current_phrase = round_phrases.get(current_round)
        for sent_start, sent_end, sentence in _split_sentences(paragraph):
            # round markers inside the sentence switch the active round
            marked_round, marked_phrase = _round_in_text(
                body, sent_start, sent_end, round_phrases, vocab)
            if marked_round is not None:
                current_round, current_phrase = marked_round, marked_phrase

            # cancellation semantics first — they never become date nodes
            round_cancel = next((p for p in vocab.cancel_round_phrases
                                 if p in sentence), None)
            if round_cancel is not None:
                result.cancels_round.append({
                    "round_no": current_round,
                    "phrase": round_cancel,
                    "paragraph_index": paragraph.index,
                    "paragraph_start": paragraph.start,
                    "sentence_start": sent_start, "sentence_end": sent_end,
                    "sentence_text": sentence})
                continue
            node_cancel = next((p for p in vocab.cancel_node_phrases
                                if p in sentence), None)
            if node_cancel is not None:
                result.cancels_node.append({
                    "round_no": current_round,
                    "phrase": node_cancel,
                    "paragraph_index": paragraph.index,
                    "paragraph_start": paragraph.start,
                    "sentence_start": sent_start, "sentence_end": sent_end,
                    "sentence_text": sentence})
                # A cancellation sentence may still quote the original date
                # descriptively; such a quote is not a new schedule node.
                continue

            try:
                standalone, ranges = find_ranges(sentence, issued_on)
            except DateParseError as exc:
                result.pending.append(PendingItem(
                    code="unparseable_date", detail=str(exc),
                    paragraph_index=paragraph.index,
                    paragraph_start=paragraph.start,
                    sentence_start=sent_start, sentence_end=sent_end,
                    sentence_text=sentence))
                continue

            roles = _role_matches(sentence, sent_start, vocab)
            if not roles and (standalone or ranges):
                result.pending.append(PendingItem(
                    code="unmapped_date",
                    detail="date without any configured node-role phrase",
                    paragraph_index=paragraph.index,
                    paragraph_start=paragraph.start,
                    sentence_start=sent_start, sentence_end=sent_end,
                    sentence_text=sentence))
                continue

            for mention in standalone:
                role = _nearest_role(mention.start, roles)
                if role is None:
                    continue
                if (notice_type == "correction"
                        and _CORRECTION_OLD_DATE.search(sentence)
                        and not _is_new_corrected_date(sentence, mention)):
                    continue
                _add_evidence(result, mention, role, paragraph,
                              sent_start, sent_end, sentence,
                              current_round, current_phrase,
                              is_range=False, connector=None, endpoint=None)

            for rng in ranges:
                role = _nearest_role(rng.start.start, roles)
                if role is None:
                    result.pending.append(PendingItem(
                        code="unmapped_date",
                        detail="date range without any configured node-role phrase",
                        paragraph_index=paragraph.index,
                        paragraph_start=paragraph.start,
                        sentence_start=sent_start, sentence_end=sent_end,
                        sentence_text=sentence))
                    continue
                spec = role[3]
                endpoint_mention = rng.start if spec.kind == "open" else rng.end
                _add_evidence(result, endpoint_mention, role, paragraph,
                              sent_start, sent_end, sentence,
                              current_round, current_phrase,
                              is_range=True, connector=rng.connector,
                              endpoint=("range_start" if endpoint_mention is rng.start
                                        else "range_end"))

            if not standalone and not ranges and not roles:
                # Only non-application notices are expected to act on another
                # notice; temporal wording there without a concrete,
                # role-bound date is material a human must confirm.  A
                # sentence that merely cites the target's number is a
                # reference, not a vague date statement.
                is_reference = (_EXPLICIT_REF.search(sentence)
                                or _EXPLICIT_REF_TRAILING_SRC.search(sentence)
                                or _EXPLICIT_REF_SRC_FIRST.search(sentence))
                if (notice_type != "application"
                        and _DATE_HINT.search(sentence)
                        and not is_reference):
                    result.pending.append(PendingItem(
                        code="vague_statement",
                        detail="temporal wording without a concrete date",
                        paragraph_index=paragraph.index,
                        paragraph_start=paragraph.start,
                        sentence_start=sent_start, sentence_end=sent_end,
                        sentence_text=sentence))
    result.has_concrete_date = bool(result.dates)
    return result


def _add_evidence(result: NoticeExtraction, mention: DateMention,
                  role: tuple[int, int, str, object], paragraph: Paragraph,
                  sent_start: int, sent_end: int, sentence: str,
                  round_no: int, round_phrase: str | None, *,
                  is_range: bool, connector: str | None,
                  endpoint: str | None) -> None:
    role_start, _role_end, role_phrase, spec = role
    result.dates.append(DateEvidence(
        role_name=spec.name, role_kind=spec.kind,
        role_phrase=role_phrase,
        role_phrase_start=sent_start + role_start,
        raw_date=mention.text, normalized_date=mention.iso,
        had_year=not mention.had_year, is_range=is_range,
        range_connector=connector, endpoint=endpoint,
        paragraph_index=paragraph.index,
        paragraph_start=paragraph.start,
        sentence_start=sent_start, sentence_end=sent_end,
        date_start=sent_start + mention.start,
        date_end=sent_start + mention.end,
        sentence_text=sentence, round_no=round_no,
        round_phrase=round_phrase))
