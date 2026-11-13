"""Workflow lead schemas and the disposition catalogue, loaded from assignment-a-data/schemas."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .normalize import normalize_header


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: Tuple[str, ...] = ()
    values: Tuple[str, ...] = ()
    default: Optional[str] = None

    @property
    def is_pii(self) -> bool:
        # A phone number is always PII, even if a schema forgets to flag it.
        return self.pii or self.type == "phone"


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: List[Field]
    _lookup: Dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for f in self.fields:  # earlier fields win if two fields share a header
            for key in [f.name, *f.aliases]:
                self._lookup.setdefault(normalize_header(key), f.name)

    @property
    def phone_field(self) -> str:
        return next(f.name for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> Optional[str]:
        return next((f.name for f in self.fields if f.reference), None)

    def map_header(self, header: List[str]) -> Dict[str, int]:
        """field name -> column index. Unmatched columns are ignored; first matching column wins."""
        mapping: Dict[str, int] = {}
        for idx, raw in enumerate(header):
            name = self._lookup.get(normalize_header(raw))
            if name is not None and name not in mapping:
                mapping[name] = idx
        return mapping


def load_schemas(schemas_dir: Path) -> Dict[str, WorkflowSchema]:
    schemas: Dict[str, WorkflowSchema] = {}
    for path in sorted(Path(schemas_dir).glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        fields = [
            Field(
                name=f["name"], type=f["type"], required=bool(f.get("required", False)),
                pii=bool(f.get("pii", False)), reference=bool(f.get("reference", False)),
                aliases=tuple(f.get("aliases", ())), values=tuple(f.get("values", ())),
                default=f.get("default"),
            )
            for f in doc["fields"]
        ]
        schemas[doc["workflow_id"]] = WorkflowSchema(doc["workflow_id"], fields)
    if not schemas:
        raise FileNotFoundError(f"no workflow schemas (*.json) found in {schemas_dir}")
    return schemas


@dataclass(frozen=True)
class Disposition:
    is_terminal: bool
    is_success: bool


Catalogue = Mapping[str, Mapping[str, Disposition]]


def load_catalogue(schemas_dir: Path) -> Dict[str, Dict[str, Disposition]]:
    catalogue: Dict[str, Dict[str, Disposition]] = {}
    with open(Path(schemas_dir) / "dispositions.csv", newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            catalogue.setdefault(row["workflow_id"].strip(), {})[row["code"].strip()] = Disposition(
                row["is_terminal"].strip().lower() == "true",
                row["is_success"].strip().lower() == "true",
            )
    return catalogue
