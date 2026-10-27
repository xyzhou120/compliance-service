"""Content-addressed normalisation cache.

The key is the file bytes plus the contract and pipeline revisions, so rule edits
never invalidate extraction and identical bytes are extracted once.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

CACHE_SCHEMA = "normalisation-cache/1"


def cache_key(file_hash: str, *, contract_version: str, pipeline_version: str) -> str:
    if not isinstance(file_hash, str) or len(file_hash) < 8:
        raise ValueError("file_hash is required")
    material = json.dumps({"hash": file_hash, "contract": contract_version,
                           "pipeline": pipeline_version}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class NormalisationCache:
    """Plain JSON cache with atomic writes; corrupted entries are ignored."""

    def __init__(self, root, *, contract_version: str, pipeline_version: str):
        self.root = Path(root)
        self.contract_version = contract_version
        self.pipeline_version = pipeline_version
        self.hits = 0
        self.misses = 0

    def path_for(self, file_hash: str) -> Path:
        key = cache_key(file_hash, contract_version=self.contract_version,
                        pipeline_version=self.pipeline_version)
        return self.root / key[:2] / (key + ".json")

    def load(self, file_hash: str):
        path = self.path_for(file_hash)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        if payload.get("schema") != CACHE_SCHEMA or payload.get("file_hash") != file_hash:
            self.misses += 1
            return None
        self.hits += 1
        return payload.get("data")

    def save(self, file_hash: str, data) -> Path:
        path = self.path_for(file_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": CACHE_SCHEMA, "file_hash": file_hash,
                              "contract_version": self.contract_version,
                              "pipeline_version": self.pipeline_version, "data": data},
                             ensure_ascii=False, sort_keys=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def get_or_compute(self, file_hash: str, compute):
        cached = self.load(file_hash)
        if cached is not None:
            return cached
        value = compute()
        self.save(file_hash, value)
        return value

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses}
