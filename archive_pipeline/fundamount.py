"""Money amounts and units for the funding-table job.

All money math uses :class:`decimal.Decimal`; no binary floating point ever
touches an amount.  Supported surface forms:

* thousands separators ``,`` and ``，`` (validated 3-digit grouping),
* full-width digits ``０-９``, the full-width decimal point ``．`` and
  full-width ``，``/``＋``/``－``,
* decimal fractions and an optional leading sign.

Units: 元 (yuan), 千元 (thousand yuan), 万元 (ten-thousand yuan), each with
an optional 人民币 prefix.  A unit declared in an amount column header
overrides the table-level unit declared in the caption.  Dash-like
placeholders (``—``, ``--``, ``待定``, ``暂无`` …) are *missing values* and
are never converted to zero.
"""

from __future__ import annotations

import re
from decimal import Decimal

# Full-width → ASCII folding for characters that may appear in amounts.
_FOLD = {ord(c): chr(ord(c) - 0xFEE0) for c in "０１２３４５６７８９"}
_FOLD[ord("．")] = "."
_FOLD[ord("，")] = ","
_FOLD[ord("＋")] = "+"
_FOLD[ord("－")] = "-"

DIGIT_FOLD = {ord(c): chr(ord(c) - 0xFEE0) for c in "０１２３４５６７８９"}


def fold_fullwidth_digits(text: str) -> str:
    return text.translate(DIGIT_FOLD)


# Placeholders that mean "no value" — never zero, always a rejection reason.
MISSING_TOKENS = {
    "", "-", "--", "—", "–", "―", "/", "／",
    "待定", "暂无", "不详", "未定", "n/a", "N/A",
}

_AMOUNT_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def parse_amount(text: str) -> Decimal | None:
    """Parse one amount cell into an exact Decimal.

    Returns ``None`` when the text is not a plain number: dash/placeholder
    tokens, prose, or malformed thousands grouping (``12,34``).
    """
    t = text.translate(_FOLD).strip()
    t = re.sub(r"\s+", "", t)
    if t in MISSING_TOKENS:
        return None
    if not _AMOUNT_RE.match(t):
        return None
    return Decimal(t.replace(",", ""))


UNIT_TO_YUAN = {"yuan": Decimal(1), "thousand": Decimal(1000), "wan": Decimal(10000)}
UNIT_LABELS = {"yuan": "元", "thousand": "千元", "wan": "万元"}

_UNIT_WORDS = {"元": "yuan", "千元": "thousand", "万元": "wan"}

FOREIGN_CURRENCIES = {
    "美元", "美金", "欧元", "日元", "港币", "港元", "英镑", "法郎", "卢布",
    "usd", "eur", "jpy", "hkd", "gbp", "chf", "rub",
}


def recognize_unit(word: str) -> str | None:
    """Map a unit word (optionally 人民币-prefixed) to yuan/thousand/wan."""
    w = re.sub(r"\s+", "", word)
    w = re.sub(r"^人民币", "", w)
    return _UNIT_WORDS.get(w)


def is_unknown_currency(word: str) -> bool:
    """True for explicit non-RMB currencies and for a bare 人民币 claim."""
    w = re.sub(r"\s+", "", word).lower()
    if w == "人民币":  # bare currency name: magnitude (元/千元/万元) unstated
        return True
    return w in FOREIGN_CURRENCIES


def to_yuan(amount: Decimal, unit: str) -> Decimal:
    """Exact conversion to RMB yuan (multiplication by a power of ten)."""
    return amount * UNIT_TO_YUAN[unit]


def format_yuan(amount: Decimal) -> str:
    """Plain (non-scientific) decimal string, trailing zeros stripped."""
    s = format(amount, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


_PAREN_RE = re.compile(r"[（(]([^（）()]*)[）)]")
_UNIT_CLAIM_RE = re.compile(r"单位\s*[:：]\s*([^\s，,。；;（）()]+)")


def split_unit_annotation(text: str) -> tuple[str, list[str], list[str]]:
    """Strip parenthesized unit annotations from a header/label.

    Returns ``(core, units, unknown_currencies)``: parentheses holding a
    known unit (``(万元)``) or a known foreign currency (``(美元)``) are
    removed from the core text and reported; any other parenthesized text
    is left in place.
    """
    units: list[str] = []
    unknowns: list[str] = []

    def repl(match: re.Match) -> str:
        inner = match.group(1).strip()
        unit = recognize_unit(inner)
        if unit:
            units.append(unit)
            return ""
        if is_unknown_currency(inner):
            unknowns.append(inner)
            return ""
        return match.group(0)

    core = _PAREN_RE.sub(repl, text)
    return core.strip(), units, unknowns


def extract_table_units(caption: str) -> tuple[list[str], list[str]]:
    """Units declared by a table caption.

    Recognizes ``单位：X`` claims (anything after the colon is a unit claim,
    so an unrecognized word there is an unknown currency) and parenthesized
    annotations such as ``（万元）``.  Returns ``(units, unknown_words)``.
    """
    units: list[str] = []
    unknowns: list[str] = []
    for match in _UNIT_CLAIM_RE.finditer(caption):
        word = match.group(1)
        unit = recognize_unit(word)
        if unit:
            units.append(unit)
        else:
            unknowns.append(word)
    _core, paren_units, paren_unknowns = split_unit_annotation(caption)
    units.extend(paren_units)
    unknowns.extend(paren_unknowns)
    return units, unknowns
