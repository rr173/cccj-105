"""Typed application field reading for the eligibility pre-screen job.

Every field used by a clause must be declared as one of four application
types: ``string``, ``boolean``, ``decimal`` or ``date``.  The reader turns a
raw JSON value into either a usable typed value or an explicit *unknown*
with a reason:

* ``missing_field`` — the key is absent;
* ``null_value`` — the key is present and JSON ``null``;
* ``type_mismatch`` — the key is present with a value that does not conform
  to the declared type (including a decimal supplied as a JSON number or a
  string that is not a plain decimal, and a date that is not a valid ISO
  calendar date).

Decimal values and decimal clause constants are **always carried as strings**
and converted to :class:`decimal.Decimal`; no binary floating point is ever
used for comparison.  A JSON number is therefore a type mismatch, not a
number to be coerced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

FIELD_TYPES = ("string", "boolean", "decimal", "date")
ORDERED_TYPES = ("decimal", "date")

# A plain decimal string.  No exponent, no whitespace, no thousands separators.
_DECIMAL_TEXT = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$")
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

UNKNOWN_MISSING = "missing_field"
UNKNOWN_NULL = "null_value"
UNKNOWN_TYPE = "type_mismatch"


@dataclass(frozen=True)
class FieldRead:
    """One field read, either usable or an explicit unknown."""

    field: str
    declared_type: str
    presence: str                 # present / missing / null
    raw_value: Any = None         # JSON-compatible raw value, when present
    value: Any = None             # typed value (Decimal / date-string / …)
    observed_type: str | None = None
    unknown_reason: str | None = None
    type_detail: str | None = None

    @property
    def known(self) -> bool:
        return self.unknown_reason is None


def observed_type(value: Any) -> str:
    """A JSON-ish type name for diagnostics (never used for coercion)."""
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def parse_decimal_text(text: Any) -> Decimal | None:
    """Strictly parse a decimal carried as a string; return None if invalid."""
    if type(text) is not str or not _DECIMAL_TEXT.match(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_iso_date(text: Any) -> str | None:
    """Validate a strictly formatted ISO date string (YYYY-MM-DD)."""
    if type(text) is not str or not _ISO_DATE.match(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def validate_constant(declared_type: str, value: Any) -> tuple[Any, str | None]:
    """Validate a clause constant against a declared field type.

    Returns ``(normalized_value, error)``.  Clause constants are checked
    *before any application is read*; a wrong constant type is a clause
    authoring error, never a per-application unknown.
    """
    if declared_type == "string":
        if type(value) is not str:
            return None, f"expected a string constant, got {observed_type(value)}"
        return value, None
    if declared_type == "boolean":
        if type(value) is not bool:
            return None, f"expected a boolean constant, got {observed_type(value)}"
        return value, None
    if declared_type == "decimal":
        if type(value) is not str:
            return None, ("decimal constants must be carried as strings, "
                          f"got {observed_type(value)}")
        parsed = parse_decimal_text(value)
        if parsed is None:
            return None, f"invalid decimal string {value!r}"
        return value, None
    if declared_type == "date":
        normalized = parse_iso_date(value)
        if normalized is None:
            return None, f"invalid ISO date constant {value!r} (want YYYY-MM-DD)"
        return normalized, None
    return None, f"unknown declared type {declared_type!r}"


def read_field(container: dict, field: str, declared_type: str,
               container_present: bool = True) -> FieldRead:
    """Read and type-check one declared field from a JSON object."""
    if not container_present:
        return FieldRead(field=field, declared_type=declared_type,
                         presence="missing", unknown_reason=UNKNOWN_MISSING)
    if field not in container:
        return FieldRead(field=field, declared_type=declared_type,
                         presence="missing", unknown_reason=UNKNOWN_MISSING)
    raw = container[field]
    if raw is None:
        return FieldRead(field=field, declared_type=declared_type,
                         presence="null", raw_value=None,
                         observed_type="null", unknown_reason=UNKNOWN_NULL)
    obs = observed_type(raw)
    if declared_type == "string":
        if type(raw) is not str:
            return FieldRead(field=field, declared_type=declared_type,
                             presence="present", raw_value=raw,
                             observed_type=obs, unknown_reason=UNKNOWN_TYPE,
                             type_detail="expected a JSON string")
        return FieldRead(field=field, declared_type=declared_type,
                         presence="present", raw_value=raw, value=raw,
                         observed_type=obs)
    if declared_type == "boolean":
        if type(raw) is not bool:
            return FieldRead(field=field, declared_type=declared_type,
                             presence="present", raw_value=raw,
                             observed_type=obs, unknown_reason=UNKNOWN_TYPE,
                             type_detail="expected a JSON boolean")
        return FieldRead(field=field, declared_type=declared_type,
                         presence="present", raw_value=raw, value=raw,
                         observed_type=obs)
    if declared_type == "decimal":
        if type(raw) is not str:
            return FieldRead(field=field, declared_type=declared_type,
                             presence="present", raw_value=raw,
                             observed_type=obs, unknown_reason=UNKNOWN_TYPE,
                             type_detail=("expected a decimal carried as a "
                                          "JSON string (no number, no float)"))
        parsed = parse_decimal_text(raw)
        if parsed is None:
            return FieldRead(field=field, declared_type=declared_type,
                             presence="present", raw_value=raw,
                             observed_type=obs, unknown_reason=UNKNOWN_TYPE,
                             type_detail="string is not a plain decimal")
        return FieldRead(field=field, declared_type=declared_type,
                         presence="present", raw_value=raw, value=parsed,
                         observed_type=obs)
    if declared_type == "date":
        if type(raw) is not str:
            return FieldRead(field=field, declared_type=declared_type,
                             presence="present", raw_value=raw,
                             observed_type=obs, unknown_reason=UNKNOWN_TYPE,
                             type_detail="expected an ISO date string")
        normalized = parse_iso_date(raw)
        if normalized is None:
            return FieldRead(field=field, declared_type=declared_type,
                             presence="present", raw_value=raw,
                             observed_type=obs, unknown_reason=UNKNOWN_TYPE,
                             type_detail="invalid ISO calendar date (want YYYY-MM-DD)")
        return FieldRead(field=field, declared_type=declared_type,
                         presence="present", raw_value=raw, value=normalized,
                         observed_type=obs)
    raise ValueError(f"unknown declared type {declared_type!r}")


def _constant_value(declared_type: str, raw: Any):
    """Clause constants were validated earlier, so conversion cannot fail."""
    if declared_type == "decimal":
        parsed = parse_decimal_text(raw)
        if parsed is None:  # defensive; validation should have rejected it
            raise ValueError(f"invalid pre-validated decimal constant {raw!r}")
        return parsed
    if declared_type == "date":
        normalized = parse_iso_date(raw)
        if normalized is None:  # defensive
            raise ValueError(f"invalid pre-validated date constant {raw!r}")
        return normalized
    return raw


def compare(typed_value: Any, constant_raw: Any, declared_type: str,
            op: str) -> bool:
    """Compare a typed field value with a validated clause constant.

    ``op`` is one of ``eq``, ``gt``, ``ge``, ``lt``, ``le``.  Decimal
    comparisons use :class:`~decimal.Decimal`; date comparisons use validated
    ISO strings.  The caller guarantees the constant type matched the field
    declaration and never passes an unknown value here.
    """
    constant = _constant_value(declared_type, constant_raw)
    if op == "eq":
        return typed_value == constant
    if declared_type == "decimal":
        left, right = typed_value, constant
    elif declared_type == "date":
        left, right = typed_value, constant
    else:
        raise TypeError(f"ordered comparison is not supported for {declared_type}")
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    raise ValueError(f"unknown comparison operator {op!r}")
