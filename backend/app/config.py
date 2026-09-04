from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge" / "invoice_payment"


class Settings(BaseModel):
    project_root: Path = PROJECT_ROOT
    backend_dir: Path = BACKEND_DIR
    workspace_root: Path = PROJECT_ROOT / "workspace" / "cases"
    storage_root: Path = BACKEND_DIR / "storage"
    session_db_path: Path = BACKEND_DIR / "storage" / "sessions.sqlite"
    memory_db_path: Path = BACKEND_DIR / "storage" / "memory.sqlite"
    knowledge_roots: list[Path] = [DEFAULT_KNOWLEDGE_ROOT]
    llm_provider: str = "openai"
    llm_model: str = "gpt-4.1-mini"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_temperature: float = 0.1
    llm_thinking_type: str | None = None
    manager_thinking_type: str = "high"
    llm_timeout_seconds: float = 90.0
    evidence_reviewer_timeout_seconds: float = 600.0
    case_patch_writer_timeout_seconds: float = 300.0
    session_compact_model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str | None = None
    embedding_base_url: str = "https://api.openai.com/v1"
    max_steps: int = 10
    context_char_limit: int = 200000
    tesseract_cmd: str = ""
    ocr_langs: str = "eng"
    pdf_max_pages: int = 5
    ocr_dpi: int = 200
    pdf_min_text_chars: int = 80
    docling_enabled: bool = True
    rapidocr_enabled: bool = True
    enable_langfuse: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_capture_payloads: str = "summary"
    llm_input_cost_per_1m: float = 0.0
    llm_output_cost_per_1m: float = 0.0
    llm_cached_input_cost_per_1m: float = 0.0
    llm_pricing_version: str = ""
    llm_pricing_currency: str = "USD"
    strict_context_partition: bool = False

    def timeout_for_role(self, role: str) -> float:
        if role == "evidence_reviewer":
            return self.evidence_reviewer_timeout_seconds
        if role == "case_patch_writer":
            return self.case_patch_writer_timeout_seconds
        return self.llm_timeout_seconds


def _load_env_files() -> None:
    explicit = os.getenv("INVOICE_AGENT_ENV_FILE", "").strip()
    candidates = [Path(explicit) if explicit else None, BACKEND_DIR / ".env"]
    for candidate in candidates:
        if candidate and candidate.exists():
            load_dotenv(candidate, override=False)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _first_float_env(default: float, *names: str) -> float:
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _paths_env(name: str, default: list[Path]) -> list[Path]:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    paths = [Path(item.strip()) for item in raw.split(";") if item.strip()]
    return paths or default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_env_files()
    provider = (_first_env("LLM_PROVIDER") or "openai").lower()
    model = _first_env("LLM_MODEL") or "gpt-4.1-mini"
    api_key = _first_env("LLM_API_KEY")
    base_url = _first_env("LLM_BASE_URL") or "https://api.openai.com/v1"
    embedding_key = _first_env("EMBEDDING_API_KEY", "LLM_API_KEY")
    embedding_base = _first_env("EMBEDDING_BASE_URL", "LLM_BASE_URL") or base_url
    storage_root = Path(os.getenv("INVOICE_AGENT_STORAGE_ROOT", BACKEND_DIR / "storage"))
    return Settings(
        workspace_root=Path(os.getenv("INVOICE_AGENT_WORKSPACE_ROOT", PROJECT_ROOT / "workspace" / "cases")),
        storage_root=storage_root,
        session_db_path=Path(os.getenv("INVOICE_AGENT_SESSION_DB", storage_root / "sessions.sqlite")),
        memory_db_path=Path(os.getenv("INVOICE_AGENT_MEMORY_DB", storage_root / "memory.sqlite")),
        knowledge_roots=_paths_env("INVOICE_AGENT_KNOWLEDGE_ROOTS", [DEFAULT_KNOWLEDGE_ROOT]),
        llm_provider=provider,
        llm_model=model,
        llm_api_key=api_key,
        llm_base_url=base_url,
        llm_temperature=_float_env("LLM_TEMPERATURE", 0.1),
        llm_thinking_type=_first_env("LLM_THINKING_TYPE", "KIMI_THINKING_TYPE"),
        manager_thinking_type=_first_env("INVOICE_AGENT_MANAGER_THINKING_TYPE") or "high",
        llm_timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", 90.0),
        evidence_reviewer_timeout_seconds=_first_float_env(
            600.0,
            "INVOICE_AGENT_EVIDENCE_REVIEWER_TIMEOUT_SECONDS",
            "EVIDENCE_REVIEWER_TIMEOUT_SECONDS",
            "LLM_EVIDENCE_REVIEWER_TIMEOUT_SECONDS",
        ),
        case_patch_writer_timeout_seconds=_first_float_env(
            300.0,
            "INVOICE_AGENT_CASE_PATCH_WRITER_TIMEOUT_SECONDS",
            "CASE_PATCH_WRITER_TIMEOUT_SECONDS",
            "LLM_CASE_PATCH_WRITER_TIMEOUT_SECONDS",
        ),
        session_compact_model=_first_env("SESSION_COMPACT_MODEL", "INVOICE_AGENT_SESSION_COMPACT_MODEL") or model,
        embedding_model=_first_env("EMBEDDING_MODEL") or "text-embedding-3-small",
        embedding_api_key=embedding_key,
        embedding_base_url=embedding_base,
        max_steps=_int_env("INVOICE_AGENT_MAX_STEPS", 10),
        context_char_limit=_int_env("INVOICE_AGENT_CONTEXT_CHAR_LIMIT", 200000),
        tesseract_cmd=_first_env("INVOICE_AGENT_TESSERACT_CMD") or "",
        ocr_langs=_first_env("INVOICE_AGENT_OCR_LANGS") or "eng",
        pdf_max_pages=_int_env("INVOICE_AGENT_PDF_MAX_PAGES", 5),
        ocr_dpi=_int_env("INVOICE_AGENT_OCR_DPI", 200),
        pdf_min_text_chars=_int_env("INVOICE_AGENT_PDF_MIN_TEXT_CHARS", 80),
        docling_enabled=_bool_env("INVOICE_AGENT_DOCLING_ENABLED", True),
        rapidocr_enabled=_bool_env("INVOICE_AGENT_RAPIDOCR_ENABLED", True),
        enable_langfuse=_bool_env("INVOICE_AGENT_ENABLE_LANGFUSE", False),
        langfuse_public_key=_first_env("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_first_env("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=_first_env("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com",
        langfuse_capture_payloads=(_first_env("INVOICE_AGENT_LANGFUSE_CAPTURE_PAYLOADS") or "summary").lower(),
        llm_input_cost_per_1m=_float_env("INVOICE_AGENT_LLM_INPUT_COST_PER_1M", 0.0),
        llm_output_cost_per_1m=_float_env("INVOICE_AGENT_LLM_OUTPUT_COST_PER_1M", 0.0),
        llm_cached_input_cost_per_1m=_float_env("INVOICE_AGENT_LLM_CACHED_INPUT_COST_PER_1M", 0.0),
        llm_pricing_version=_first_env("INVOICE_AGENT_LLM_PRICING_VERSION") or "",
        llm_pricing_currency=(
            _first_env("INVOICE_AGENT_LLM_PRICING_CURRENCY") or "USD"
        ).upper(),
        strict_context_partition=_bool_env("INVOICE_AGENT_STRICT_CONTEXT_PARTITION", False),
    )
