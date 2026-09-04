from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.state.attachment_manifest import resolve_manifest_attachment, upsert_manifest_read_items
from app.state.case_store import CaseStore
from app.state.schemas import Attachment
from app.tools.document_extraction import write_extraction_dossiers
from app.tools.pdf_renderer import render_pdf_from_store


TEXT_ATTACHMENT_SUFFIXES = {".csv", ".json", ".log", ".md", ".txt", ".xml", ".yaml", ".yml"}
PDF_ATTACHMENT_SUFFIXES = {".pdf"}
IMAGE_ATTACHMENT_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


class FileWorkspace:
    def __init__(self, store: CaseStore | None = None) -> None:
        self.store = store or CaseStore()

    def read_case_state(self, case_id: str) -> dict[str, Any]:
        return self.store.load(case_id).model_dump()

    def write_case_patch(
        self,
        case_id: str,
        patch: dict[str, Any],
        *,
        review_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if review_artifact is not None:
            return self.store.apply_review_patch(case_id, patch, review_artifact).model_dump()
        return self.store.apply_patch(case_id, patch).model_dump()

    def list_case_files(self, case_id: str) -> dict[str, Any]:
        root = self.store.ensure_case_dirs(case_id)
        files = [str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("*") if path.is_file()]
        return {"case_id": case_id, "files": sorted(files)}

    def write_case_file(self, case_id: str, relative_path: str, content: str = "") -> dict[str, Any]:
        path = self.store.resolve_case_path(case_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"case_id": case_id, "path": str(path), "relative_path": relative_path, "bytes": len(content.encode("utf-8"))}

    def read_case_file(self, case_id: str, relative_path: str) -> str:
        path = self.store.resolve_case_path(case_id, relative_path)
        return path.read_text(encoding="utf-8")

    def read_attachment(
        self,
        case_id: str,
        attachments: list[Attachment],
        *,
        name: str = "",
        path: str = "",
        attachment_id: str = "",
        original_ref: str = "",
        max_chars: int = 12000,
        session_id: str = "",
        turn_id: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        selected: list[Attachment]
        if attachment_id and attachments and not name and not path and not original_ref:
            declared = _select_attachment(attachments, name=attachment_id, path="")
            if declared:
                name = attachment_id
                attachment_id = ""
        if attachment_id or original_ref:
            resolved = resolve_manifest_attachment(
                self.store,
                case_id,
                attachment_id=attachment_id,
                original_ref=original_ref,
            )
            selected = [
                Attachment(
                    name=str(resolved.get("name") or ""),
                    path=str(resolved.get("path") or ""),
                    content_type=str(resolved.get("content_type") or ""),
                )
            ]
        else:
            attachment = _select_attachment(attachments, name=name, path=path)
            if not attachment:
                available = [item.name or item.path for item in attachments]
                raise FileNotFoundError(f"Attachment not declared on this request. Available: {available}")
            selected = [attachment]
            if not name and not path:
                selected = list(attachments)
        items: list[dict[str, Any]] = []
        errors: list[Exception] = []
        for item in selected:
            try:
                items.append(_read_attachment_item(self.store, case_id, item, max_chars=max_chars))
            except Exception as exc:
                errors.append(exc)
                items.append(_attachment_error_item(item, exc))
        manifest_update = upsert_manifest_read_items(
            self.store,
            case_id,
            items,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
        )
        dossier_updates = write_extraction_dossiers(self.store, case_id, items)
        if dossier_updates:
            manifest_update = upsert_manifest_read_items(
                self.store,
                case_id,
                items,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
            )
            manifest_update["extraction_dossiers"] = dossier_updates
        if errors and not any(item.get("status") == "success" for item in items):
            raise _batch_attachment_error(errors)
        combined = "\n\n".join(_combined_attachment_text(item) for item in items)
        first = items[0]
        return {
            "case_id": case_id,
            "name": first["name"],
            "path": first["path"],
            "content_type": first["content_type"],
            "content": combined,
            "truncated": any(bool(item["truncated"]) for item in items),
            "chars": sum(int(item["chars"]) for item in items),
            "attachments": items,
            "attachment_count": len(items),
            "successful_attachment_count": sum(1 for item in items if item.get("status") == "success"),
            "failed_attachment_count": sum(1 for item in items if item.get("status") == "error"),
            "attachment_manifest": manifest_update,
        }

    def render_pdf(self, case_id: str, markdown_path: str, pdf_path: str | None = None) -> dict[str, Any]:
        return render_pdf_from_store(self.store, case_id, markdown_path, pdf_path)


def safe_report_path(filename: str, *, started_at: str = "") -> str:
    clean = Path(filename).name
    stem = Path(clean).stem.lower()
    if not clean or stem in {"final_report", "report", "manager_report"}:
        clean = f"final_report_{report_timestamp(started_at)}.md"
    if not clean.endswith(".md"):
        clean = f"{clean}.md"
    return f"reports/{clean}"


def report_timestamp(started_at: str = "") -> str:
    text = str(started_at or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.strftime("%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def report_paths_for_run(started_at: str = "") -> tuple[str, str]:
    markdown = safe_report_path("final_report.md", started_at=started_at)
    return markdown, markdown.rsplit(".", 1)[0] + ".pdf"


def _select_attachment(attachments: list[Attachment], *, name: str, path: str) -> Attachment | None:
    if attachments and not name and not path:
        return attachments[0]
    wanted_name = (name or "").strip()
    wanted_path = str(Path(path).expanduser().resolve()) if path else ""
    for item in attachments:
        if wanted_name and item.name == wanted_name:
            return item
        if wanted_path and item.path and str(Path(item.path).expanduser().resolve()) == wanted_path:
            return item
    return None


def _read_attachment_item(
    store: CaseStore,
    case_id: str,
    attachment: Attachment,
    *,
    max_chars: int,
) -> dict[str, Any]:
    source = _attachment_source_path(store, case_id, attachment.path)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Attachment path does not exist: {attachment.path}")
    suffix = source.suffix.lower()
    if suffix not in TEXT_ATTACHMENT_SUFFIXES | PDF_ATTACHMENT_SUFFIXES | IMAGE_ATTACHMENT_SUFFIXES:
        raise ValueError(
            f"Unsupported attachment type for evidence review: {source.suffix or '<none>'}. "
            "Supported types are txt, md, json, csv, log, xml, yaml, yml, pdf, jpg, jpeg, png, tif, tiff, webp, gif, and bmp."
        )
    name = attachment.name or source.name
    source_digest = _file_sha256(source)[:12]
    original_ref = _copy_original(store, case_id, source, name)
    warnings: list[str] = []
    preview_paths: list[str] = []
    visual_notes: list[str] = []
    pages_processed = 0
    content_kind = "text"
    extraction_method = "text_direct"
    if suffix in TEXT_ATTACHMENT_SUFFIXES:
        content = _read_text(source)
    elif suffix in PDF_ATTACHMENT_SUFFIXES:
        content_kind = "pdf"
        content, extraction_method, preview_paths, pages_processed, warnings = _read_pdf_attachment(
            store,
            case_id,
            source,
            name=name,
            source_digest=source_digest,
        )
        visual_notes = _visual_notes_for_previews(store, case_id, preview_paths, content)
    else:
        content_kind = "image"
        content, extraction_method, preview_paths, warnings = _read_image_attachment(
            store,
            case_id,
            source,
            name=name,
            source_digest=source_digest,
        )
        preview_images = [store.resolve_case_path(case_id, path) for path in preview_paths] if preview_paths else [source]
        visual_notes = _visual_notes_for_images(preview_images, content)
    truncated = content[:max_chars]
    return {
        "status": "success",
        "name": name,
        "path": str(source),
        "source_path": str(source),
        "original_ref": original_ref,
        "content_type": attachment.content_type,
        "content_kind": content_kind,
        "extraction_method": extraction_method,
        "content": truncated,
        "_full_content": content,
        "truncated": len(content) > len(truncated),
        "chars": len(content),
        "preview_paths": preview_paths,
        "pages_processed": pages_processed,
        "warnings": warnings,
        "visual_notes": visual_notes,
    }


def _attachment_source_path(store: CaseStore, case_id: str, path: str) -> Path:
    raw = Path(path).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return store.resolve_case_path(case_id, str(path))


def _read_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("text", b"", 0, 1, f"Could not decode attachment as text: {path}")


def _read_pdf_attachment(
    store: CaseStore,
    case_id: str,
    source: Path,
    *,
    name: str,
    source_digest: str,
) -> tuple[str, str, list[str], int, list[str]]:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError("pdf_reader_unavailable: PyMuPDF is required to read PDF attachments") from exc

    settings = get_settings()
    preview_paths: list[str] = []
    warnings: list[str] = []
    text_parts: list[str] = []
    try:
        doc = fitz.open(str(source))
    except Exception as exc:
        raise RuntimeError(f"pdf_open_error: could not open PDF attachment {source.name}: {exc}") from exc
    try:
        if getattr(doc, "needs_pass", False):
            raise RuntimeError(f"pdf_open_error: encrypted PDF requires a password: {source.name}")
        page_count = int(getattr(doc, "page_count", 0) or 0)
        if page_count <= 0:
            raise RuntimeError(f"pdf_open_error: PDF has no pages: {source.name}")
        max_pages = max(1, min(int(settings.pdf_max_pages or 5), page_count))
        zoom = max(1.0, min(float(settings.ocr_dpi or 200) / 72.0, 4.0))
        matrix = fitz.Matrix(zoom, zoom)
        for page_index in range(max_pages):
            page = doc.load_page(page_index)
            page_no = page_index + 1
            page_text = str(page.get_text("text") or "").strip()
            if page_text:
                text_parts.append(f"[page {page_no} text]\n{page_text}")
            preview_rel = _preview_relative_path(name, source_digest, page_no)
            preview_abs = store.resolve_case_path(case_id, preview_rel)
            preview_abs.parent.mkdir(parents=True, exist_ok=True)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(str(preview_abs))
            preview_paths.append(preview_rel)
        if page_count > max_pages:
            warnings.append(f"pdf_truncated_to_first_{max_pages}_pages")
    finally:
        doc.close()

    text_content = "\n\n".join(text_parts).strip()
    needs_ocr = len(text_content.strip()) < int(settings.pdf_min_text_chars or 80)
    ocr_parts: list[str] = []
    if needs_ocr:
        if text_content:
            warnings.append("pdf_text_sparse_ocr_fallback_used")
        for page_no, preview_rel in enumerate(preview_paths, start=1):
            preview_abs = store.resolve_case_path(case_id, preview_rel)
            try:
                ocr_text = _run_ocr(preview_abs).strip()
            except RuntimeError as exc:
                warnings.append(str(exc)[:400])
                ocr_text = ""
            if ocr_text:
                ocr_parts.append(f"[page {page_no} OCR]\n{ocr_text}")
        if not ocr_parts:
            warnings.append("ocr_empty_text")
    content_parts = [part for part in [text_content, *ocr_parts] if part]
    content = "\n\n".join(content_parts).strip()
    if ocr_parts and text_content:
        method = "mixed_pdf_text_ocr"
    elif ocr_parts:
        method = "pdf_ocr"
    else:
        method = "pdf_text"
    return content, method, preview_paths, len(preview_paths), warnings


def _read_image_attachment(
    store: CaseStore,
    case_id: str,
    source: Path,
    *,
    name: str,
    source_digest: str,
) -> tuple[str, str, list[str], list[str]]:
    warnings: list[str] = []
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.verify()
    except Exception as exc:
        raise RuntimeError(f"image_open_error: could not open image attachment {source.name}: {exc}") from exc
    preview_paths = [_image_preview(store, case_id, source, name=name, source_digest=source_digest)]
    try:
        content = _run_ocr(source).strip()
    except RuntimeError as exc:
        warnings.append(str(exc)[:400])
        content = ""
    if not content:
        warnings.append("ocr_empty_text")
    return content, "image_ocr", preview_paths, warnings


def _visual_notes_for_previews(store: CaseStore, case_id: str, preview_paths: list[str], content: str) -> list[str]:
    paths: list[Path] = []
    for preview in preview_paths[:3]:
        try:
            paths.append(store.resolve_case_path(case_id, preview))
        except Exception:
            continue
    return _visual_notes_for_images(paths, content)


def _visual_notes_for_images(paths: list[Path], content: str) -> list[str]:
    lowered = str(content or "").lower()
    looks_like_signature_area = any(marker in lowered for marker in ("authorized signatory", "signatory", "signature"))
    notes: list[str] = []
    for path in paths[:3]:
        note = _detect_signature_visual_note(path, require_signature_text=looks_like_signature_area)
        if note:
            notes.append(f"{path.name}: {note}")
            break
    if looks_like_signature_area and not notes:
        notes.append("signatory_label_present_but_no_colored_signature_mark_detected")
    return notes


def _detect_signature_visual_note(path: Path, *, require_signature_text: bool) -> str:
    try:
        from PIL import Image

        with Image.open(path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            crop = rgb.crop((int(width * 0.55), int(height * 0.35), width, int(height * 0.82)))
            pixels = crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
            colored = 0
            for red, green, blue in pixels:
                brightness = (red + green + blue) / 3
                if brightness < 45 or brightness > 245:
                    continue
                color_spread = max(red, green, blue) - min(red, green, blue)
                blue_or_ink = blue > red + 18 or blue > green + 18 or color_spread > 55
                if blue_or_ink:
                    colored += 1
            threshold = 18 if require_signature_text else 80
            if colored >= threshold:
                return "visual_signature_mark_present_near_signatory_area"
    except Exception:
        return ""
    return ""


def _run_ocr(image_path: Path) -> str:
    settings = get_settings()
    command = _tesseract_command(settings.tesseract_cmd)
    with tempfile.TemporaryDirectory(prefix="invoice_agent_ocr_") as tmp:
        output_base = Path(tmp) / "ocr"
        args = [
            command,
            str(image_path),
            str(output_base),
            "-l",
            str(settings.ocr_langs or "eng"),
            "--psm",
            "6",
        ]
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("ocr_unavailable: Tesseract OCR command was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ocr_failed: Tesseract timed out for {image_path.name}") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"ocr_failed: Tesseract failed for {image_path.name}: {stderr[:400]}")
        output_file = output_base.with_suffix(".txt")
        if not output_file.exists():
            raise RuntimeError(f"ocr_failed: Tesseract did not produce text for {image_path.name}")
        return output_file.read_text(encoding="utf-8", errors="replace")


def _tesseract_command(explicit: str) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return str(path)
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        raise RuntimeError(f"ocr_unavailable: configured Tesseract command not found: {explicit}")
    resolved = shutil.which("tesseract")
    if resolved:
        return resolved
    windows_default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if windows_default.exists():
        return str(windows_default)
    raise RuntimeError("ocr_unavailable: Tesseract OCR command was not found")


def _copy_original(store: CaseStore, case_id: str, source: Path, display_name: str) -> str:
    case_root = store.ensure_case_dirs(case_id).resolve()
    try:
        relative = source.resolve().relative_to(case_root)
        if relative.parts[:2] == ("attachments", "originals"):
            return str(relative).replace("\\", "/")
    except ValueError:
        pass
    target_rel = f"attachments/originals/{_source_file_name(source, display_name)}"
    target = store.resolve_case_path(case_id, target_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target_rel


def _source_file_name(source: Path, display_name: str) -> str:
    suffix = source.suffix.lower()
    stem = _safe_name(Path(display_name or source.name).stem or source.stem)
    try:
        stat = source.stat()
        basis = f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        basis = str(source.resolve())
    digest = hashlib.sha1(basis.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{stem}_{digest}{suffix}"


def _preview_relative_path(name: str, source_digest: str, page_no: int) -> str:
    stem = _safe_name(Path(name).stem or "document")
    return f"evidence/previews/{stem}_{source_digest}_p{page_no:03d}.png"


def _image_preview(store: CaseStore, case_id: str, source: Path, *, name: str, source_digest: str) -> str:
    from PIL import Image

    stem = _safe_name(Path(name).stem or source.stem or "image")
    rel = f"evidence/previews/{stem}_{source_digest}_image.png"
    target = store.resolve_case_path(case_id, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.convert("RGB").save(target)
    return rel


def _safe_name(value: str) -> str:
    clean = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value).strip())
    return (clean[:80].strip("._") or "attachment")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _attachment_error_item(attachment: Attachment, exc: Exception) -> dict[str, Any]:
    source = Path(attachment.path).expanduser()
    return {
        "status": "error",
        "name": attachment.name or source.name,
        "path": str(source),
        "source_path": str(source),
        "content_type": attachment.content_type,
        "content_kind": _content_kind_from_suffix(source.suffix.lower()),
        "extraction_method": "failed",
        "content": "",
        "truncated": False,
        "chars": 0,
        "preview_paths": [],
        "pages_processed": 0,
        "warnings": [f"{type(exc).__name__}: {exc}"],
        "visual_notes": [],
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def _content_kind_from_suffix(suffix: str) -> str:
    if suffix in PDF_ATTACHMENT_SUFFIXES:
        return "pdf"
    if suffix in IMAGE_ATTACHMENT_SUFFIXES:
        return "image"
    if suffix in TEXT_ATTACHMENT_SUFFIXES:
        return "text"
    return "unsupported"


def _batch_attachment_error(errors: list[Exception]) -> Exception:
    first = errors[0]
    if len(errors) == 1:
        return first
    joined = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors[:6])
    return RuntimeError(f"All attachment reads failed: {joined}")


def _combined_attachment_text(item: dict[str, Any]) -> str:
    lines = [
        f"--- attachment: {item.get('name') or ''} ---",
        f"status: {item.get('status') or 'success'}",
        f"content_kind: {item.get('content_kind') or ''}",
        f"extraction_method: {item.get('extraction_method') or ''}",
        f"original_ref: {item.get('original_ref') or ''}",
    ]
    previews = item.get("preview_paths") or []
    if previews:
        lines.append(f"preview_paths: {', '.join(str(path) for path in previews)}")
    warnings = item.get("warnings") or []
    if warnings:
        lines.append(f"warnings: {'; '.join(str(warning) for warning in warnings)}")
    visual_notes = item.get("visual_notes") or []
    if visual_notes:
        lines.append(f"visual_notes: {'; '.join(str(note) for note in visual_notes)}")
    visual_check = item.get("visual_check") if isinstance(item.get("visual_check"), dict) else {}
    if visual_check:
        ocr = visual_check.get("ocr_quality") if isinstance(visual_check.get("ocr_quality"), dict) else {}
        page = visual_check.get("page_integrity") if isinstance(visual_check.get("page_integrity"), dict) else {}
        same_source = visual_check.get("same_source_check") if isinstance(visual_check.get("same_source_check"), dict) else {}
        lines.append(
            "visual_check: "
            + "; ".join(
                [
                    f"looks_like_invoice={visual_check.get('looks_like_invoice') or ''}",
                    f"ocr_quality={ocr.get('status') or ''}",
                    f"page_integrity={page.get('status') or ''}",
                    f"same_source={same_source.get('status') or ''}",
                ]
            )
        )
    extraction_ref = item.get("extraction_ref") or ""
    if extraction_ref:
        lines.append(f"extraction_ref: {extraction_ref}")
    body_markdown = str(item.get("body_markdown") or "").strip()
    if body_markdown:
        lines.append("extracted_body_markdown:")
        lines.append(body_markdown[:2400])
    field_inventory = item.get("field_inventory") or []
    if field_inventory:
        lines.append("field_inventory:")
        for field in field_inventory[:18]:
            if not isinstance(field, dict):
                continue
            lines.append(
                "- "
                + "; ".join(
                    [
                        f"field={field.get('field') or ''}",
                        f"value={field.get('value') or ''}",
                        f"status={field.get('status') or ''}",
                        f"locator={field.get('locator') or ''}",
                        f"confidence={field.get('confidence') or ''}",
                    ]
                )
            )
    quality_notes = item.get("quality_notes") or []
    if quality_notes:
        lines.append(f"quality_notes: {'; '.join(str(note) for note in quality_notes[:10])}")
    page_summaries = item.get("page_summaries") or []
    if page_summaries:
        lines.append("page_summaries:")
        for page in page_summaries[:6]:
            if isinstance(page, dict):
                lines.append(
                    f"- page={page.get('page')}; blocks={page.get('block_count')}; tables={page.get('table_count')}; "
                    f"preview={page.get('preview_path')}; text={page.get('text_preview')}"
                )
    if item.get("status") == "error":
        error = item.get("error") or {}
        lines.append(f"error: {error.get('type', '')}: {error.get('message', '')}")
    else:
        lines.append("extracted_text:")
        lines.append(str(item.get("content") or ""))
    return "\n".join(lines)
