"""Persisted layout variants: discovered + reviewed layouts, kept off the code path.

The shipped variants live in code; everything learned from real documents is
stored in a JSON registry so a new mine's layout can be confirmed once and reused
afterwards without a release.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile

from .variants import SHIPPED, Variant, header_signature

SCHEMA = "layout-variants/1"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def variant_payload(variant: Variant) -> dict:
    record = asdict(variant)
    record["header_signature"] = list(variant.header_signature)
    return record


def variant_from_payload(payload: dict) -> Variant:
    return Variant(variant_id=payload["variant_id"], group=payload["group"],
                   header_signature=tuple(payload["header_signature"]),
                   reader=payload["reader"], mine_code=payload.get("mine_code"),
                   params=dict(payload.get("params") or {}), notes=payload.get("notes", ""))


class VariantStore:
    """Append-only registry of reviewed layouts."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> list:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            return []
        return [variant_from_payload(item) for item in payload.get("variants") or []]

    def all(self) -> list:
        return list(SHIPPED) + self.load()

    def revision(self) -> str:
        return hashlib.sha256(_canonical([variant_payload(v) for v in self.load()])
                              .encode("utf-8")).hexdigest()[:16]

    def add(self, variant: Variant) -> dict:
        variants = self.load()
        for existing in variants:
            if (existing.group == variant.group
                    and existing.header_signature == variant.header_signature
                    and (existing.mine_code or None) == (variant.mine_code or None)):
                if existing.reader != variant.reader:
                    raise ValueError("variant already registered with a different reader")
                return {"added": False, "variant": variant_payload(existing),
                        "revision": self.revision()}
        variants.append(variant)
        self._write(variants)
        return {"added": True, "variant": variant_payload(variant), "revision": self.revision()}

    def _write(self, variants) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": SCHEMA, "variants": [variant_payload(v) for v in variants]}
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=1))
            temporary = Path(handle.name)
        try:
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def discover(tables, group: str, *, known=None, mine_code=None) -> list:
    """Group tables by header signature and report the ones no variant covers."""
    from .variants import match_variant
    variants = known if known is not None else SHIPPED
    pending = {}
    for table in tables:
        signature = header_signature(table)
        if not signature:
            continue
        if match_variant(table, group, mine_code=mine_code, variants=variants) is not None:
            continue
        key = signature
        entry = pending.setdefault(key, {"signature": list(signature), "group": group,
                                         "tables": 0, "sample_table_ids": [],
                                         # A signature containing digits is usually a data
                                         # row mistaken for a header, not a new layout.
                                         "suspicious_header": any(
                                             any(ch.isdigit() for ch in label) for label in signature)})
        entry["tables"] += 1
        if len(entry["sample_table_ids"]) < 5:
            entry["sample_table_ids"].append(table.table_id)
    return sorted(pending.values(), key=lambda item: (-item["tables"], item["signature"]))
