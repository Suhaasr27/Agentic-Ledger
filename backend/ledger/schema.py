"""Workflow lead schemas and the disposition catalogue (loaded from ``schemas/``)."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .normalize import normalize_header

FIELD_TYPES = {"string", "phone", "date", "decimal", "enum"}


class SchemaError(ValueError):
    """A workflow schema file is structurally invalid."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True)
class LeadSchema:
    workflow_id: str
    fields: tuple[FieldSpec, ...]
    raw: dict

    @property
    def phone_field(self) -> FieldSpec:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> FieldSpec:
        return next(f for f in self.fields if f.reference)

    @property
    def defaults(self) -> dict[str, str]:
        return {f.name: f.default for f in self.fields if f.default is not None}

    def header_lookup(self) -> dict[str, str]:
        """normalised header text -> field name (field name and every alias)."""
        lookup: dict[str, str] = {}
        for f in self.fields:
            for label in (f.name, *f.aliases):
                lookup.setdefault(normalize_header(label), f.name)
        return lookup

    def map_headers(self, header: list[str]) -> dict[int, str]:
        """column index -> field name. Unmatched columns are ignored; first column wins on clashes."""
        lookup = self.header_lookup()
        mapping: dict[int, str] = {}
        taken: set[str] = set()
        for idx, cell in enumerate(header):
            field = lookup.get(normalize_header(cell))
            if field and field not in taken:
                mapping[idx] = field
                taken.add(field)
        return mapping


@dataclass(frozen=True)
class Disposition:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


def _parse_schema(raw: dict, source: str) -> LeadSchema:
    try:
        fields = tuple(
            FieldSpec(
                name=f["name"],
                type=f["type"],
                required=bool(f.get("required", False)),
                pii=bool(f.get("pii", False)),
                reference=bool(f.get("reference", False)),
                aliases=tuple(f.get("aliases", [])),
                values=tuple(f.get("values", [])),
                default=f.get("default"),
            )
            for f in raw["fields"]
        )
        schema = LeadSchema(workflow_id=raw["workflow_id"], fields=fields, raw=raw)
    except (KeyError, TypeError) as exc:
        raise SchemaError(f"{source}: malformed schema ({exc!r})") from exc

    bad = [f.name for f in fields if f.type not in FIELD_TYPES]
    if bad:
        raise SchemaError(f"{source}: unknown field type for {bad}")
    if sum(f.type == "phone" for f in fields) != 1:
        raise SchemaError(f"{source}: exactly one phone field is required (it is the lead identity)")
    refs = [f for f in fields if f.reference]
    if len(refs) != 1 or not refs[0].required:
        raise SchemaError(f"{source}: exactly one required reference field is needed (external_ref)")
    if any(f.type == "enum" and not f.values for f in fields):
        raise SchemaError(f"{source}: enum fields must list their values")
    return schema


def load_schemas(schemas_dir: Path) -> dict[str, LeadSchema]:
    schemas: dict[str, LeadSchema] = {}
    for path in sorted(schemas_dir.glob("*.json")):
        schema = _parse_schema(json.loads(path.read_text(encoding="utf-8")), path.name)
        schemas[schema.workflow_id] = schema
    if not schemas:
        raise SchemaError(f"no workflow schemas found in {schemas_dir}")
    return schemas


def load_dispositions(path: Path) -> list[Disposition]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [
            Disposition(
                workflow_id=row["workflow_id"].strip(),
                code=row["code"].strip(),
                label=row["label"].strip(),
                is_terminal=row["is_terminal"].strip().lower() == "true",
                is_success=row["is_success"].strip().lower() == "true",
            )
            for row in csv.DictReader(fh)
        ]


@dataclass(frozen=True)
class Catalog:
    schemas: dict[str, LeadSchema]
    dispositions: list[Disposition]

    @classmethod
    def load(cls, schemas_dir: Path) -> "Catalog":
        schemas = load_schemas(schemas_dir)
        dispositions = load_dispositions(schemas_dir / "dispositions.csv")
        unknown = {d.workflow_id for d in dispositions} - set(schemas)
        if unknown:
            raise SchemaError(f"dispositions.csv references workflows without a schema: {sorted(unknown)}")
        return cls(schemas, dispositions)
