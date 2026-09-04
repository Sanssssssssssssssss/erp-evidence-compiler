from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FactProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str
    field_path: str
    revision: str
    fingerprint: str
    source_kind: Literal["odoo_record", "policy", "document"]

    @field_validator("source_ref", "revision")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("fact provenance identity must not be empty")
        return value

    @field_validator("field_path")
    @classmethod
    def json_pointer(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/"):
            raise ValueError("fact provenance field_path must be a JSON pointer")
        return value

    @field_validator("fingerprint")
    @classmethod
    def sha256(cls, value: str) -> str:
        value = value.strip().lower()
        if not _SHA256.fullmatch(value):
            raise ValueError("fact provenance fingerprint must be a SHA-256 hex digest")
        return value


__all__ = ["FactProvenance"]
