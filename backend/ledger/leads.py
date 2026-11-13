"""Lead CSV parsing: header mapping, validation, normalisation, rejection, in-file merging.

Pure with respect to the database: a file goes in, a ``LeadFileParse`` comes out.
The DB layer decides what already exists; this layer only applies the row rules.
"""
from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_phone, parse_date, parse_decimal, parse_enum
from .schema import FieldSpec, LeadSchema


@dataclass(frozen=True)
class Accepted:
    values: dict[str, str]  # field name -> canonical non-null value
    warnings: int


@dataclass(frozen=True)
class Rejected:
    reason: str


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    reason: str


@dataclass
class MergedLead:
    """One lead identity after merging every valid row seen for it (last non-null write wins)."""

    fields: dict[str, str] = field(default_factory=dict)

    def merge(self, values: Mapping[str, str]) -> None:
        self.fields.update(values)


@dataclass
class LeadFileParse:
    records_total: int = 0  # non-blank data rows
    records_accepted: int = 0  # valid rows (leads created + duplicates merged)
    field_warnings: int = 0
    rejections: list[RejectedRow] = field(default_factory=list)
    leads: dict[str, MergedLead] = field(default_factory=dict)  # canonical phone -> lead, first-seen order


def parse_value(spec: FieldSpec, text: str) -> str | None:
    """Canonical storable form of a trimmed, non-empty value; ``None`` if invalid."""
    if spec.type == "string":
        return text
    if spec.type == "phone":
        return normalize_phone(text)
    if spec.type == "date":
        parsed = parse_date(text)
        return parsed.isoformat() if parsed else None
    if spec.type == "decimal":
        amount = parse_decimal(text)
        return str(amount) if amount is not None else None
    if spec.type == "enum":
        return parse_enum(text, spec.values)
    raise ValueError(f"unsupported field type {spec.type!r}")


def validate_row(schema: LeadSchema, raw: Mapping[str, str | None]) -> Accepted | Rejected:
    """Walk the fields in schema order; the first failure is the row's single rejection reason."""
    values: dict[str, str] = {}
    warnings = 0
    for spec in schema.fields:
        text = (raw.get(spec.name) or "").strip()
        if not text:
            if spec.required:
                return Rejected(f"missing_required:{spec.name}")
            continue
        parsed = parse_value(spec, text)
        if parsed is None:
            if spec.type == "phone":
                return Rejected("invalid_phone")
            warnings += 1  # optional field present but invalid -> null + one warning
            continue
        values[spec.name] = parsed
    return Accepted(values, warnings)


def _is_blank(cells: list[str]) -> bool:
    return all(not cell.strip() for cell in cells)


def parse_lead_file(path: Path, schema: LeadSchema) -> LeadFileParse:
    result = LeadFileParse()
    phone_name = schema.phone_field.name
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return result
        mapping = schema.map_headers(header)
        for cells in reader:
            if _is_blank(cells):
                continue
            result.records_total += 1
            raw = {name: cells[idx] if idx < len(cells) else "" for idx, name in mapping.items()}
            outcome = validate_row(schema, raw)
            if isinstance(outcome, Rejected):
                result.rejections.append(RejectedRow(result.records_total, outcome.reason))
                continue
            result.records_accepted += 1
            result.field_warnings += outcome.warnings
            lead = result.leads.setdefault(outcome.values[phone_name], MergedLead())
            lead.merge(outcome.values)
    return result
