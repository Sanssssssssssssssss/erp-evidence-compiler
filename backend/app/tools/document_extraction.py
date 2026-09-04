from __future__ import annotations

import hashlib
import json
import re
import csv
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from app.config import get_settings


DOSSIER_VERSION = "extraction_dossier_v1"
EXTRACTION_ROOT = "evidence/extractions"
FIELD_PATTERNS: dict[str, list[str]] = {
    "invoice_number": [
        r"\binvoice\s*(?:no\.?|number|#)\s*[:#\s-]*([A-Z0-9_./-]{5,})",
        r"\b(INV[-_./A-Z0-9]{4,})\b",
    ],
    "supplier": [
        r"\b(?:sold\s+by|supplier|vendor|from)\s*[:：]\s*([^\n\r]{3,120})",
        r"\b(?:bill\s+from)\s*[:：]\s*([^\n\r]{3,120})",
    ],
    "buyer": [
        r"\b(?:buyer|bill\s+to|billing\s+address|sold\s+to|customer)\s*[:：]\s*([^\n\r]{3,140})",
        r"\b(?:ship\s+to|shipping\s+address)\s*[:：]\s*([^\n\r]{3,140})",
    ],
    "invoice_date": [
        r"\binvoice\s+date\s*[:：-]?\s*([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})",
        r"\bdate\s*[:：-]?\s*([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})",
    ],
    "amount_total": [
        r"\b(?:grand\s+total|amount\s+due|total\s+amount|total)\s*[:：]?\s*(?:[$€£₹¥￥]|rs\.?|USD|EUR|GBP|INR|CNY|RMB)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"(?:(?:[$€£₹¥￥]|rs\.?)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    ],
    "currency_tax": [
        r"\b(?:tax|vat|gst|cgst|sgst)\s*[:：%]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?%?)",
        r"(USD|EUR|GBP|INR|CNY|RMB|Rs\.?|[$€£₹¥￥])",
    ],
    "line_items_product_title": [
        r"\b(?:product|item|description|title)\s*[:：]?\s*([^\n\r]{3,160})",
        r"\bqty\s+price\s+tax\s+total\b",
    ],
    "signature_or_authorized_signatory": [
        r"\b(?:authorized\s+signatory|signature|signed\s+by)\b[^\n\r]{0,120}",
    ],
    "po_ref": [
        r"\b(?:PO|purchase\s+order)\s*(?:no\.?|number|#)?\s*[:#\s-]*([A-Z0-9_./-]{4,})",
    ],
    "grn_ref": [
        r"\b(?:GRN|goods\s+receipt)\s*(?:no\.?|number|#)?\s*[:#\s-]*([A-Z0-9_./-]{4,})",
    ],
    "bank_details": [
        r"\b(?:bank|iban|bic|swift|account|payment\s+account)\b[^\n\r]{0,220}",
    ],
}


def write_extraction_dossiers(store: Any, case_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Write one compact, replayable extraction dossier per successful attachment."""

    written: list[dict[str, Any]] = []
    for item in items:
        if item.get("status") != "success":
            item.pop("_full_content", None)
            continue
        attachment_id = str(item.get("attachment_id") or "").strip()
        if not attachment_id:
            item.pop("_full_content", None)
            continue
        dossier = build_extraction_dossier(store, case_id, item)
        rel = f"{EXTRACTION_ROOT}/{attachment_id}.json"
        target = store.resolve_case_path(case_id, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dossier, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        item["extraction_ref"] = rel
        item["extraction_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        item["extraction_methods"] = dossier.get("extraction_methods", [])
        item["body_markdown"] = _brief(dossier.get("body_markdown") or dossier.get("full_text") or "", 2400)
        item["field_inventory"] = dossier.get("field_inventory", [])[:24]
        item["page_summaries"] = dossier.get("page_summaries", [])[:8]
        item["quality_notes"] = dossier.get("quality_notes", [])[:12]
        item["visual_regions"] = dossier.get("visual_regions", [])[:12]
        item["visual_check"] = dossier.get("visual_check", {})
        item["block_crops"] = dossier.get("block_crops", [])[:48]
        item["table_count"] = len(dossier.get("tables") or [])
        item["line_items"] = dossier.get("line_items", [])[:200]
        item["line_item_count"] = int(dossier.get("line_item_count") or 0)
        item["line_item_pages"] = list(dossier.get("line_item_pages") or [])[:12]
        item.pop("_full_content", None)
        written.append(
            {
                "attachment_id": attachment_id,
                "extraction_ref": rel,
                "extraction_sha256": item["extraction_sha256"],
                "field_inventory_count": len(dossier.get("field_inventory") or []),
                "page_count": len(dossier.get("pages") or []),
                "table_count": len(dossier.get("tables") or []),
            }
        )
    return written


def build_extraction_dossier(store: Any, case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    source = _source_path(store, case_id, item)
    content_kind = str(item.get("content_kind") or _content_kind(source))
    original_text = str(item.get("_full_content") or item.get("content") or "")
    preview_paths = [str(path) for path in item.get("preview_paths") or [] if str(path)]
    quality_notes: list[str] = []
    extraction_methods = _unique_strings([item.get("extraction_method") or "attachment_reader"])

    pdf_markdown, pymupdf4llm_notes = _try_pymupdf4llm(source) if content_kind == "pdf" else ("", [])
    quality_notes.extend(pymupdf4llm_notes)
    if pdf_markdown:
        extraction_methods.append("pymupdf4llm_markdown")

    pages, blocks, tables, pymupdf_notes = _pymupdf_pages(store, case_id, source, preview_paths)
    quality_notes.extend(pymupdf_notes)
    if pages or blocks or tables:
        extraction_methods.append("pymupdf_structured")

    strong_extraction_needed = _needs_strong_extraction(content_kind, original_text, item) or (
        content_kind == "pdf"
        and not tables
        and _looks_like_tabular_text(pdf_markdown or original_text)
    )
    if strong_extraction_needed:
        docling_text, docling_tables, docling_notes = _try_docling(source)
        quality_notes.extend(docling_notes)
        if docling_text or docling_tables:
            extraction_methods.append("docling_structured")
            tables.extend(docling_tables)
            pages_by_number = {int(page.get("page") or 0): page for page in pages}
            for table in docling_tables:
                page = pages_by_number.get(int(table.get("page") or 0))
                if page is not None:
                    page.setdefault("tables", []).append(str(table.get("id") or ""))
    else:
        docling_text, docling_tables = "", []
        quality_notes.append("strong_extraction_skipped_fast_layout_sufficient")

    if strong_extraction_needed and not docling_text:
        rapid_blocks, rapid_text, rapid_notes = _try_rapidocr(store, case_id, source, preview_paths, content_kind)
        quality_notes.extend(rapid_notes)
        if rapid_blocks:
            extraction_methods.append("rapidocr")
            blocks.extend(rapid_blocks)
    else:
        rapid_text = ""

    page_text = "\n".join(page.get("text", "") for page in pages)
    body_markdown = docling_text if docling_tables else (pdf_markdown or _pages_to_markdown(pages, tables))
    full_text = _merge_texts([pdf_markdown, docling_text, original_text, page_text, rapid_text])
    if not pages:
        pages = [
            {
                "page": 1,
                "text": _brief(full_text, 8000),
                "preview_path": preview_paths[0] if preview_paths else "",
                "blocks": [],
                "tables": [],
                "quality_notes": [],
            }
        ]
    for page in pages:
        if not page.get("text") and full_text:
            page["text"] = _brief(full_text, 8000)
            break

    key_values = _key_value_candidates(full_text) + _block_field_candidates(blocks)
    line_items = _line_items_from_tables(tables)
    line_item_pages = sorted({int(item.get("page") or 0) for item in line_items if item.get("page")})
    key_values.extend(_line_item_field_candidates(line_items, line_item_pages))
    visual_regions = _visual_regions(item, preview_paths)
    field_inventory = _field_inventory(key_values, visual_regions, item)
    block_crops = _build_block_crops(store, case_id, item, blocks, pages, preview_paths, visual_regions)
    field_inventory = _attach_field_crop_refs(field_inventory, block_crops, preview_paths)
    page_summaries = _page_summaries(pages, blocks, tables)
    warnings = [str(warning) for warning in item.get("warnings") or [] if str(warning)]
    if not full_text.strip():
        quality_notes.append("no_extracted_text_available")
    if any("signatory" in str(note).lower() for note in item.get("visual_notes") or []) and not any(
        field.get("field") == "signature_or_authorized_signatory" for field in field_inventory
    ):
        field_inventory.append(
            {
                "field": "signature_or_authorized_signatory",
                "value": "visual signature/signatory candidate",
                "status": "unclear",
                "source_quote": "; ".join(str(note) for note in item.get("visual_notes") or [])[:220],
                "locator": "visual_notes",
                "confidence": "medium",
            }
        )

    visual_check = _visual_check(
        store=store,
        case_id=case_id,
        item=item,
        content_kind=content_kind,
        full_text=full_text,
        pages=pages,
        tables=tables,
        field_inventory=field_inventory,
        page_summaries=page_summaries,
        visual_regions=visual_regions,
        preview_paths=preview_paths,
        extraction_methods=_unique_strings(extraction_methods),
        quality_notes=_unique_strings(quality_notes),
        warnings=warnings,
    )

    return {
        "version": DOSSIER_VERSION,
        "attachment_id": item.get("attachment_id", ""),
        "name": item.get("name", ""),
        "source_path": str(source),
        "original_ref": item.get("original_ref", ""),
        "content_kind": content_kind,
        "extraction_methods": _unique_strings(extraction_methods),
        "full_text": full_text,
        "body_markdown": body_markdown,
        "pages": pages,
        "blocks": blocks[:300],
        "tables": tables[:40],
        "line_items": line_items[:200],
        "line_item_count": len(line_items),
        "line_item_pages": line_item_pages,
        "key_value_candidates": key_values[:80],
        "visual_regions": visual_regions[:40],
        "block_crops": block_crops[:300],
        "preview_paths": preview_paths,
        "field_inventory": field_inventory[:80],
        "page_summaries": page_summaries,
        "visual_check": visual_check,
        "quality_notes": _unique_strings(quality_notes)[:40],
        "warnings": warnings,
        "chars": len(full_text),
        "context_delivery": {
            "content_is_excerpt_for_model_context": bool(item.get("truncated")),
            "full_text_preserved": bool(full_text),
            "full_text_chars": len(full_text),
        },
        "source_refs": {
            "original_ref": item.get("original_ref", ""),
            "preview_paths": preview_paths,
            "extraction_ref": f"{EXTRACTION_ROOT}/{item.get('attachment_id', '')}.json" if item.get("attachment_id") else "",
        },
    }


def compact_dossier_for_context(dossier: dict[str, Any], *, max_fields: int = 24) -> dict[str, Any]:
    return {
        "attachment_id": dossier.get("attachment_id", ""),
        "name": dossier.get("name", ""),
        "content_kind": dossier.get("content_kind", ""),
        "extraction_methods": list(dossier.get("extraction_methods") or [])[:8],
        "source_refs": dossier.get("source_refs") or {},
        "field_inventory": list(dossier.get("field_inventory") or [])[:max_fields],
        "body_markdown": _brief(dossier.get("body_markdown") or dossier.get("full_text") or "", 2400),
        "block_crops": list(dossier.get("block_crops") or [])[:48],
        "page_summaries": list(dossier.get("page_summaries") or [])[:8],
        "tables": [
            {
                "id": table.get("id", ""),
                "page": table.get("page", ""),
                "summary": _brief(table.get("markdown") or table.get("csv") or "", 360),
                "source": table.get("source", ""),
            }
            for table in list(dossier.get("tables") or [])[:8]
            if isinstance(table, dict)
        ],
        "line_items": list(dossier.get("line_items") or [])[:200],
        "line_item_count": int(dossier.get("line_item_count") or 0),
        "line_item_pages": list(dossier.get("line_item_pages") or [])[:12],
        "visual_regions": list(dossier.get("visual_regions") or [])[:12],
        "visual_check": _compact_visual_check(dossier.get("visual_check")),
        "quality_notes": list(dossier.get("quality_notes") or [])[:12],
        "warnings": list(dossier.get("warnings") or [])[:8],
    }


def _source_path(store: Any, case_id: str, item: dict[str, Any]) -> Path:
    original_ref = str(item.get("original_ref") or "")
    if original_ref:
        try:
            return store.resolve_case_path(case_id, original_ref)
        except Exception:
            pass
    return Path(str(item.get("source_path") or item.get("path") or "")).expanduser().resolve()


def _content_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}:
        return "image"
    return "text"


def _needs_strong_extraction(content_kind: str, text: str, item: dict[str, Any]) -> bool:
    if content_kind not in {"pdf", "image"}:
        return False
    warnings = " ".join(str(warning).lower() for warning in item.get("warnings") or [])
    if any(marker in warnings for marker in ("ocr_empty", "ocr_failed", "ocr_unavailable", "table")):
        return True
    if "sparse" in warnings and len(str(text or "").strip()) < 40:
        return True
    if content_kind == "pdf" and len(str(text or "").strip()) < 40:
        return True
    if content_kind == "image" and len(str(text or "").strip()) < 80:
        return True
    return False


def _looks_like_tabular_text(text: str) -> bool:
    """Detect layout that a flat text extraction is likely to have collapsed."""

    for line in str(text or "").splitlines():
        if line.count("|") >= 3:
            return True
        if len(re.findall(r"\*\*[^*\n]{1,80}\*\*", line)) >= 3:
            return True
    return False


def _try_pymupdf4llm(source: Path) -> tuple[str, list[str]]:
    """Use PyMuPDF4LLM as a lightweight PDF-to-markdown body extractor when available."""

    if source.suffix.lower() != ".pdf":
        return "", []
    try:
        import pymupdf4llm  # type: ignore[import-not-found]
    except Exception:
        return "", []
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            text = pymupdf4llm.to_markdown(str(source))
        return str(text or "").strip(), []
    except Exception as exc:
        return "", [f"pymupdf4llm_failed:{type(exc).__name__}"]


def _try_docling(source: Path) -> tuple[str, list[dict[str, Any]], list[str]]:
    if not getattr(get_settings(), "docling_enabled", True):
        return "", [], []
    try:
        from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
    except Exception:
        return "", [], ["docling_not_installed"]
    try:
        converter = DocumentConverter()
        converted = converter.convert(str(source))
        document = getattr(converted, "document", None)
        if document is None:
            return "", [], ["docling_returned_no_document"]
        if hasattr(document, "export_to_markdown"):
            text = str(document.export_to_markdown() or "")
        elif hasattr(document, "export_to_text"):
            text = str(document.export_to_text() or "")
        else:
            text = str(document)
        return text.strip(), _docling_tables(document), []
    except Exception as exc:
        return "", [], [f"docling_failed:{type(exc).__name__}"]


def _docling_tables(document: Any) -> list[dict[str, Any]]:
    try:
        payload = document.export_to_dict()
    except Exception:
        return []
    tables: list[dict[str, Any]] = []
    for index, raw_table in enumerate(list(payload.get("tables") or [])[:40], start=1):
        data = dict(raw_table.get("data") or {})
        grid = list(data.get("grid") or [])
        rows = [
            [str((cell or {}).get("text") or "").strip() for cell in list(row or [])]
            for row in grid
        ]
        if not rows:
            continue
        provenance = list(raw_table.get("prov") or [])
        page = int((provenance[0] if provenance else {}).get("page_no") or 1)
        table_id = f"p{page}_dt{index:03d}"
        cells: list[dict[str, Any]] = []
        for row_index, row in enumerate(grid):
            for column_index, cell in enumerate(list(row or [])):
                cell = dict(cell or {})
                bbox = dict(cell.get("bbox") or {})
                cells.append(
                    {
                        "row": row_index,
                        "column": column_index,
                        "text": str(cell.get("text") or "").strip(),
                        "bbox": [bbox.get(key) for key in ("l", "t", "r", "b")],
                        "coordinate_origin": str(bbox.get("coord_origin") or ""),
                    }
                )
        table_bbox = dict((provenance[0] if provenance else {}).get("bbox") or {})
        tables.append(
            {
                "id": table_id,
                "page": page,
                "markdown": _rows_to_markdown(rows),
                "csv": _rows_to_csv(rows),
                "bbox": [table_bbox.get(key) for key in ("l", "t", "r", "b")],
                "cells": cells,
                "source": "docling_tableformer",
            }
        )
    return tables


def _pymupdf_pages(
    store: Any,
    case_id: str,
    source: Path,
    preview_paths: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if source.suffix.lower() != ".pdf":
        return [], [], [], []
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return [], [], [], ["pymupdf_not_installed"]
    settings = get_settings()
    pages: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    notes: list[str] = []
    try:
        doc = fitz.open(str(source))
    except Exception as exc:
        return [], [], [], [f"pymupdf_open_failed:{type(exc).__name__}"]
    try:
        max_pages = max(1, min(int(settings.pdf_max_pages or 5), int(getattr(doc, "page_count", 0) or 0)))
        for page_index in range(max_pages):
            page = doc.load_page(page_index)
            page_no = page_index + 1
            text = str(page.get_text("text") or "").strip()
            page_blocks: list[str] = []
            for block_index, block in enumerate(page.get_text("blocks") or [], start=1):
                if len(block) < 5:
                    continue
                block_text = str(block[4] or "").strip()
                if not block_text:
                    continue
                block_id = f"p{page_no}_b{block_index:03d}"
                page_blocks.append(block_id)
                blocks.append(
                    {
                        "id": block_id,
                        "page": page_no,
                        "type": "text",
                        "text": _brief(block_text, 1600),
                        "bbox": [round(float(value), 2) for value in block[:4]],
                        "source": "pymupdf",
                    }
                )
            page_tables = _pymupdf_tables(page, page_no)
            tables.extend(page_tables)
            pages.append(
                {
                    "page": page_no,
                    "width": round(float(page.rect.width), 2),
                    "height": round(float(page.rect.height), 2),
                    "text": _brief(text, 8000),
                    "preview_path": preview_paths[page_index] if page_index < len(preview_paths) else "",
                    "blocks": page_blocks[:80],
                    "tables": [table.get("id", "") for table in page_tables],
                    "quality_notes": [],
                }
            )
    finally:
        doc.close()
    return pages, blocks, tables, notes


def _pymupdf_tables(page: Any, page_no: int) -> list[dict[str, Any]]:
    if not hasattr(page, "find_tables"):
        return []
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            found = page.find_tables()
        raw_tables = list(getattr(found, "tables", []) or [])
    except Exception:
        return []
    tables: list[dict[str, Any]] = []
    for index, table in enumerate(raw_tables[:8], start=1):
        rows: list[list[str]] = []
        try:
            rows = [[str(cell or "").strip() for cell in row] for row in table.extract()]
        except Exception:
            rows = []
        if not rows:
            continue
        table_id = f"p{page_no}_t{index:03d}"
        tables.append(
            {
                "id": table_id,
                "page": page_no,
                "markdown": _rows_to_markdown(rows),
                "csv": _rows_to_csv(rows),
                "bbox": [round(float(value), 2) for value in getattr(table, "bbox", []) or []],
                "source": "pymupdf_find_tables",
            }
        )
    return tables


def _line_items_from_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    line_items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, int]] = set()
    for table in tables[:40]:
        page = int(table.get("page") or 0)
        table_id = str(table.get("id") or "")
        csv_text = str(table.get("csv") or "")
        if not csv_text.strip():
            continue
        rows = [[str(cell or "").strip() for cell in row] for row in csv.reader(StringIO(csv_text))]
        columns = _line_item_columns(rows[0]) if rows else None
        candidates = rows[1:] if columns else rows
        for row in candidates:
            cells = [cell for cell in row if cell]
            parsed = _parse_line_item_cells(cells, columns=columns, raw_row=row)
            if not parsed:
                continue
            parsed["page"] = page
            parsed["table_id"] = table_id
            key = (
                str(parsed.get("position") or ""),
                str(parsed.get("text") or ""),
                str(parsed.get("quantity") or ""),
                str(parsed.get("unit_price") or ""),
                str(parsed.get("total_amount") or ""),
                page,
            )
            if key in seen:
                continue
            seen.add(key)
            line_items.append(parsed)
    return line_items


def _line_item_columns(header: list[str]) -> dict[str, int] | None:
    normalized = [re.sub(r"[^a-z0-9]+", " ", cell.lower()).strip() for cell in header]
    aliases = {
        "text": {"description", "item", "product", "service", "details"},
        "quantity": {"qty", "quantity"},
        "unit_price": {"unit price", "price", "rate"},
        "total_amount": {"amount", "total", "line total"},
    }
    result: dict[str, int] = {}
    for field, names in aliases.items():
        for index, value in enumerate(normalized):
            if value in names:
                result[field] = index
                break
    return result if set(result) == set(aliases) else None


def _parse_line_item_cells(
    cells: list[str],
    *,
    columns: dict[str, int] | None = None,
    raw_row: list[str] | None = None,
) -> dict[str, Any] | None:
    if columns:
        row = raw_row or cells
        if max(columns.values()) >= len(row):
            return None
        text = row[columns["text"]]
        quantity = row[columns["quantity"]]
        unit_price = row[columns["unit_price"]]
        total_amount = row[columns["total_amount"]]
        if not text or not all(_number_value(value) is not None for value in (quantity, unit_price, total_amount)):
            return None
        return {
            "position": "",
            "text": text,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": total_amount,
        }
    if len(cells) < 4:
        return None
    position = cells[0]
    if not re.fullmatch(r"\d{1,3}\.\d{1,3}", position):
        return None
    text = cells[1]
    if not text or text.lower() in {"text", "description", "title"}:
        return None
    quantity = cells[2] if len(cells) > 2 else ""
    unit_price = cells[3] if len(cells) > 3 else ""
    total_amount = cells[4] if len(cells) > 4 else ""
    if not _number_value(total_amount) and len(cells) > 3:
        numeric_cells = [cell for cell in cells[2:] if _number_value(cell)]
        if len(numeric_cells) >= 2:
            unit_price = numeric_cells[-2]
            total_amount = numeric_cells[-1]
    return {
        "position": position,
        "text": text,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_amount": total_amount,
    }


def _line_item_field_candidates(line_items: list[dict[str, Any]], pages: list[int]) -> list[dict[str, Any]]:
    if not line_items:
        return []
    page_label = ", ".join(str(page) for page in pages[:8])
    first = line_items[0]
    last = line_items[-1]
    source_quote = (
        f"{len(line_items)} structured line items across pages {page_label}; "
        f"first {first.get('position')} {first.get('text')}; "
        f"last {last.get('position')} {last.get('text')}"
    )
    return [
        {
            "field": "line_items_product_title",
            "key": "line_items_product_title",
            "value": f"{len(line_items)} structured line items",
            "source_quote": source_quote,
            "locator": f"tables pages {page_label}".strip(),
            "confidence": "high",
            "_score": 96,
        }
    ]


def _try_rapidocr(
    store: Any,
    case_id: str,
    source: Path,
    preview_paths: list[str],
    content_kind: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    if not getattr(get_settings(), "rapidocr_enabled", True):
        return [], "", []
    image_paths: list[Path] = []
    if content_kind == "image":
        image_paths.append(source)
    for preview in preview_paths[:5]:
        try:
            image_paths.append(store.resolve_case_path(case_id, preview))
        except Exception:
            continue
    if not image_paths:
        return [], "", []
    try:
        from rapidocr import RapidOCR  # type: ignore[import-not-found]
        from rapidocr.utils.typings import EngineType  # type: ignore[import-not-found]
    except Exception:
        return [], "", ["rapidocr_not_installed"]
    try:
        params = {f"{part}.engine_type": EngineType.TORCH for part in ("Det", "Cls", "Rec")}
        ocr = RapidOCR(params=params)
    except Exception as exc:
        return [], "", [f"rapidocr_init_failed:{type(exc).__name__}"]
    blocks: list[dict[str, Any]] = []
    texts: list[str] = []
    notes: list[str] = []
    for page_no, image_path in enumerate(image_paths, start=1):
        try:
            result = ocr(str(image_path))
        except Exception as exc:
            notes.append(f"rapidocr_failed:{image_path.name}:{type(exc).__name__}")
            continue
        raw_texts = getattr(result, "txts", None)
        raw_boxes = getattr(result, "boxes", None)
        raw_scores = getattr(result, "scores", None)
        result_texts = list(raw_texts) if raw_texts is not None else []
        result_boxes = list(raw_boxes) if raw_boxes is not None else []
        result_scores = list(raw_scores) if raw_scores is not None else []
        for line_index, text_value in enumerate(result_texts, start=1):
            text = str(text_value or "").strip()
            if not text:
                continue
            raw_box = result_boxes[line_index - 1] if line_index <= len(result_boxes) else []
            bbox = raw_box.tolist() if hasattr(raw_box, "tolist") else raw_box
            texts.append(text)
            blocks.append(
                {
                    "id": f"p{page_no}_ocr{line_index:03d}",
                    "page": page_no,
                    "type": "ocr_line",
                    "text": _brief(text, 800),
                    "bbox": bbox or [],
                    "confidence": result_scores[line_index - 1] if line_index <= len(result_scores) else "",
                    "source": "rapidocr",
                }
            )
    return blocks, "\n".join(texts), notes


def _key_value_candidates(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r", "\n")
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for field, patterns in FIELD_PATTERNS.items():
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
                value = _clean_value(match.group(1) if match.groups() else match.group(0))
                if not value or not _valid_candidate(field, value, match.group(0)):
                    continue
                key = (field, value.lower())
                if key in seen:
                    continue
                seen.add(key)
                quote = _brief(match.group(0), 220)
                candidates.append(
                    {
                        "field": field,
                        "key": field,
                        "value": value,
                        "source_quote": quote,
                        "locator": "dossier_text_regex",
                        "confidence": _confidence_from_score(_candidate_score(field, value, match.group(0))),
                        "_score": _candidate_score(field, value, match.group(0)),
                    }
                )
    return candidates


def _block_field_candidates(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    header_y: float | None = None
    for block in blocks:
        text = str(block.get("text") or "").strip()
        lower = text.lower()
        if "product" in lower and "title" in lower and ("qty" in lower or "price" in lower):
            bbox = block.get("bbox") or []
            if len(bbox) >= 2:
                header_y = float(bbox[1])
            break
    for block_index, block in enumerate(blocks):
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        lower = text.lower()
        locator = str(block.get("id") or "pymupdf_block")
        page = block.get("page", "")
        if "sold by" in lower:
            before = text.split("Sold By", 1)[0].strip(" ,:-")
            if before:
                candidates.append(_candidate("supplier", before, text, f"page {page} block {locator}", 85))
        if _looks_like_letterhead_supplier(block, text):
            candidates.append(_candidate("supplier", text.split("|", 1)[0].strip(), text, f"page {page} block {locator}", 82))
        if text.startswith("SAP SE"):
            candidates.append(_candidate("buyer", "SAP SE", text, f"page {page} block {locator}", 75))
        if lower.startswith("billing address"):
            buyer = _billing_address_name(text)
            if buyer:
                candidates.append(_candidate("buyer", buyer, text, f"page {page} block {locator}", 85))
        if "invoice no" in lower or "invoice #" in lower:
            match = re.search(r"(?:invoice\s*(?:no\.?|number|#)\s*[:#\s-]*)([A-Z0-9_./-]{5,})", text, re.I)
            if match:
                candidates.append(_candidate("invoice_number", _clean_value(match.group(1)), text, f"page {page} block {locator}", 90))
        if "grand total" in lower:
            amount = _last_number(text)
            if not amount and block_index + 1 < len(blocks):
                next_block = blocks[block_index + 1]
                if int(next_block.get("page") or 0) == int(page or 0):
                    amount = _last_number(str(next_block.get("text") or ""))
            if amount:
                candidates.append(_candidate("amount_total", amount, text, f"page {page} block {locator}", 95))
        if "authorized signatory" in lower or "signature" in lower:
            candidates.append(_candidate("signature_or_authorized_signatory", text, text, f"page {page} block {locator}", 80))
        if _looks_like_bank_details(text):
            candidates.append(_candidate("bank_details", _bank_details_value(text), text, f"page {page} block {locator}", 90))
        if _looks_like_tax_block(text):
            score = 70
            if re.search(r"\d+(?:\.\d+)?%", text):
                score += 25
            if "tax total" in lower and not re.search(r"\d", text):
                score -= 35
            candidates.append(_candidate("currency_tax", text, text, f"page {page} block {locator}", score))
        if header_y is not None and _looks_like_line_item_block(block, header_y):
            bbox = block.get("bbox") or []
            x0 = float(bbox[0]) if len(bbox) >= 1 else 0.0
            score = 75 + min(len(text) // 8, 25) + (20 if x0 > 80 else 0)
            candidates.append(_candidate("line_items_product_title", text, text, f"page {page} block {locator}", score))
    return candidates


def _candidate(field: str, value: str, quote: str, locator: str, score: int) -> dict[str, Any]:
    return {
        "field": field,
        "key": field,
        "value": _clean_value(value),
        "source_quote": _brief(quote, 220),
        "locator": locator,
        "confidence": _confidence_from_score(score),
        "_score": score,
    }


def _confidence_from_score(score: int) -> str:
    if score >= 70:
        return "high"
    if score <= 35:
        return "low"
    return "medium"


def _looks_like_bank_details(text: str) -> bool:
    lower = str(text or "").lower()
    return bool(re.search(r"\b(iban|bic|swift|bank|account|payment account)\b", lower)) and bool(
        re.search(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{8,}|\b[A-Z0-9]{6,}\b", str(text or ""), re.I)
    )


def _bank_details_value(text: str) -> str:
    clean = _clean_value(text)
    iban = re.search(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{8,}\b", clean, re.I)
    bic = re.search(r"\bBIC\s+([A-Z0-9]{6,})\b", clean, re.I)
    parts = []
    if "bank" in clean.lower():
        parts.append(clean.split(".")[0].strip())
    if bic:
        parts.append(f"BIC {bic.group(1)}")
    if iban:
        parts.append(f"IBAN {iban.group(0).strip()}")
    return "; ".join(_unique_strings(parts)) or clean


def _looks_like_line_item_block(block: dict[str, Any], header_y: float) -> bool:
    text = str(block.get("text") or "").strip()
    if len(text) < 10:
        return False
    lower = text.lower()
    if any(token in lower for token in ("price is inclusive", "total ", "grand total", "invoice", "shipping", "billing")):
        return False
    if re.fullmatch(r"[0-9.,%\sA-Z]{3,}", text):
        return False
    bbox = block.get("bbox") or []
    if len(bbox) < 4:
        return False
    x0, y0, _x1, _y1 = [float(value) for value in bbox[:4]]
    return header_y + 5 <= y0 <= header_y + 110 and x0 <= 330


def _billing_address_name(text: str) -> str:
    value = re.sub(r"(?i)^billing\s+address\s*", "", str(text or "")).strip()
    if not value:
        return ""
    stop = re.search(r"\b\d", value)
    if stop:
        value = value[: stop.start()].strip()
    return " ".join(value.split()[:6]).strip(" ,:-")


def _looks_like_tax_block(text: str) -> bool:
    lower = str(text or "").lower()
    if "vat/tin" in lower or "service tax #" in lower:
        return False
    return (
        any(token in lower for token in ("tax", "vat", "gst", "cst", "inr", "rs", "usd", "eur", "gbp", "cny", "rmb"))
        or "%" in lower
        or any(symbol in text for symbol in ("€", "₹", "$", "£", "¥", "￥"))
    )


def _looks_like_letterhead_supplier(block: dict[str, Any], text: str) -> bool:
    page = int(block.get("page") or 0)
    bbox = block.get("bbox") or []
    if page != 1 or len(bbox) < 4:
        return False
    x0, y0 = float(bbox[0]), float(bbox[1])
    lower = text.lower()
    if y0 > 110 or x0 > 140:
        return False
    if any(token in lower for token in ("invoice", "project", "order", "page", "billing", "shipping")):
        return False
    return any(token in lower for token in (" ltd", " limited", " inc", " gmbh", " se", "installations", "services", "address", "|"))


def _last_number(text: str) -> str:
    matches = re.findall(r"(?:[$€£₹]\s*)?[0-9][0-9,]*(?:\.[0-9]{1,2})?", str(text or ""))
    return matches[-1] if matches else ""


def _field_inventory(
    key_values: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    ranked_candidates = sorted(key_values, key=lambda row: int(row.get("_score") or 0), reverse=True)
    for candidate in ranked_candidates:
        field = str(candidate.get("field") or candidate.get("key") or "")
        if not field or field in fields:
            continue
        fields[field] = {
            "field": field,
            "value": candidate.get("value", ""),
            "status": "present",
            "source_quote": candidate.get("source_quote", ""),
            "locator": candidate.get("locator", ""),
            "confidence": candidate.get("confidence", "medium"),
        }
    _merge_currency_tax_field(fields, ranked_candidates)
    if any(region.get("type") == "signature_candidate" for region in visual_regions):
        fields.setdefault(
            "signature_or_authorized_signatory",
            {
                "field": "signature_or_authorized_signatory",
                "value": "visual signature/signatory candidate",
                "status": "unclear",
                "source_quote": "; ".join(str(note) for note in item.get("visual_notes") or [])[:220],
                "locator": "visual_region:signature_candidate",
                "confidence": "medium",
            },
        )
    return list(fields.values())


def _merge_currency_tax_field(fields: dict[str, dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
    current = fields.get("currency_tax")
    if not current:
        return
    currency_rows = [
        row
        for row in candidates
        if str(row.get("field") or "") == "currency_tax" and _has_currency_marker(str(row.get("source_quote") or row.get("value") or ""))
    ]
    if not currency_rows:
        return
    current_text = str(current.get("source_quote") or current.get("value") or "")
    if _has_currency_marker(current_text):
        return
    currency_text = str(currency_rows[0].get("source_quote") or currency_rows[0].get("value") or "").strip()
    if not currency_text:
        return
    currency_value = _currency_marker_summary(f"{current_text} {currency_text}")
    current["value"] = currency_value or "; ".join(_unique_strings([str(current.get("value") or ""), currency_text]))
    current["source_quote"] = "; ".join(_unique_strings([current_text, currency_text]))
    current["locator"] = currency_rows[0].get("locator") or current.get("locator", "")
    current["confidence"] = "high" if current.get("locator") else current.get("confidence", "medium")


def _has_currency_marker(text: str) -> bool:
    lower = str(text or "").lower()
    return bool(re.search(r"\b(inr|rs\.?|usd|eur|gbp|cny|rmb)\b", lower)) or any(
        symbol in text for symbol in ("₹", "$", "€", "£", "¥", "￥")
    )


def _currency_marker_summary(text: str) -> str:
    raw = str(text or "")
    lower = raw.lower()
    values: list[str] = []
    code_map = {
        "usd": "USD",
        "eur": "EUR",
        "gbp": "GBP",
        "inr": "INR",
        "cny": "CNY",
        "rmb": "RMB",
    }
    for token, label in code_map.items():
        if re.search(rf"\b{re.escape(token)}\b", lower):
            values.append(label)
    if re.search(r"\brs\.?\b", lower):
        values.append("INR/Rs")
    symbol_map = {
        "€": "EUR/€",
        "â‚¬": "EUR/€",
        "₹": "INR/₹",
        "â‚¹": "INR/₹",
        "$": "$",
        "£": "GBP/£",
        "Â£": "GBP/£",
        "¥": "CNY/¥",
        "￥": "CNY/￥",
    }
    for symbol, label in symbol_map.items():
        if symbol in raw:
            values.append(label)
    return ", ".join(_unique_strings(values))


def _build_block_crops(
    store: Any,
    case_id: str,
    item: dict[str, Any],
    blocks: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    preview_paths: list[str],
    visual_regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    attachment_id = str(item.get("attachment_id") or "attachment")
    page_by_no = {int(page.get("page") or 0): page for page in pages if isinstance(page, dict)}
    crops: list[dict[str, Any]] = []
    crops.extend(_build_field_context_crops(store, case_id, attachment_id, blocks, page_by_no, preview_paths))
    crops.extend(_build_signature_context_crops(store, case_id, attachment_id, blocks, page_by_no, preview_paths))
    for block in blocks[:300]:
        crop = _crop_block(store, case_id, attachment_id, block, page_by_no, preview_paths)
        if crop:
            crops.append(crop)
    for region in visual_regions[:40]:
        crop = _crop_visual_region(store, case_id, attachment_id, region, preview_paths)
        if crop:
            crops.append(crop)
    return crops


def _build_signature_context_crops(
    store: Any,
    case_id: str,
    attachment_id: str,
    blocks: list[dict[str, Any]],
    page_by_no: dict[int, dict[str, Any]],
    preview_paths: list[str],
) -> list[dict[str, Any]]:
    crops: list[dict[str, Any]] = []
    for block in blocks[:300]:
        text = str(block.get("text") or "")
        lower = text.lower()
        if "authorized signatory" not in lower and "signature" not in lower and "signatory" not in lower:
            continue
        crop = _crop_signature_context(store, case_id, attachment_id, block, page_by_no, preview_paths)
        if crop:
            crops.append(crop)
            break
    return crops


def _crop_signature_context(
    store: Any,
    case_id: str,
    attachment_id: str,
    block: dict[str, Any],
    page_by_no: dict[int, dict[str, Any]],
    preview_paths: list[str],
) -> dict[str, Any] | None:
    page_no = int(block.get("page") or 1)
    preview_rel = _preview_for_page(page_no, page_by_no, preview_paths)
    bbox = _numeric_bbox(block.get("bbox"))
    if not preview_rel or not bbox:
        return None
    preview_abs = store.resolve_case_path(case_id, preview_rel)
    if not preview_abs.exists():
        return None
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(preview_abs) as image:
            image = image.convert("RGB")
            label_box = _preview_crop_box(block, bbox, page_by_no.get(page_no) or {}, image.size)
            if not label_box:
                return None
            left, top, right, bottom = label_box
            width, height = image.size
            box_w = max(right - left, 1)
            crop_box = (
                max(0, left - int(box_w * 0.65)),
                max(0, top - 145),
                min(width, right + int(box_w * 0.45)),
                min(height, bottom + 32),
            )
            target_rel = _crop_relative_path(attachment_id, f"p{page_no}_signature_context")
            target_abs = store.resolve_case_path(case_id, target_rel)
            target_abs.parent.mkdir(parents=True, exist_ok=True)
            image.crop(crop_box).save(target_abs)
    except Exception:
        return None
    return {
        "crop_id": f"p{page_no}_signature_context",
        "kind": "signature_context",
        "page": page_no,
        "bbox": [round(float(value), 2) for value in bbox],
        "text": _brief(block.get("text") or "", 260),
        "locator": f"page {page_no} signature_context",
        "crop_path": target_rel,
        "preview_path": preview_rel,
        "confidence": "medium",
        "proves": "证明签名/授权签章区域与授权签署文字在同一页相邻区域可见",
    }


def _build_field_context_crops(
    store: Any,
    case_id: str,
    attachment_id: str,
    blocks: list[dict[str, Any]],
    page_by_no: dict[int, dict[str, Any]],
    preview_paths: list[str],
) -> list[dict[str, Any]]:
    crops: list[dict[str, Any]] = []
    for index, block in enumerate(blocks[:300]):
        text = str(block.get("text") or "")
        lower = text.lower()
        if not any(token in lower for token in ("grand total", "amount due", "total amount")):
            continue
        neighbors = [block]
        page_no = int(block.get("page") or 1)
        if not re.search(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", text):
            for neighbor in blocks[index + 1 : index + 4]:
                if int(neighbor.get("page") or 0) != page_no:
                    continue
                if re.search(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", str(neighbor.get("text") or "")):
                    neighbors.append(neighbor)
                    break
        if len(neighbors) < 2 and not re.search(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", text):
            continue
        crop = _crop_combined_blocks(
            store,
            case_id,
            attachment_id,
            neighbors,
            page_by_no,
            preview_paths,
            crop_id=f"p{page_no}_amount_total_context",
            proves="证明总金额标签和金额值位于同一页相邻区域",
        )
        if crop:
            crops.append(crop)
    return crops


def _crop_combined_blocks(
    store: Any,
    case_id: str,
    attachment_id: str,
    blocks: list[dict[str, Any]],
    page_by_no: dict[int, dict[str, Any]],
    preview_paths: list[str],
    *,
    crop_id: str,
    proves: str,
) -> dict[str, Any] | None:
    if not blocks:
        return None
    page_no = int(blocks[0].get("page") or 1)
    preview_rel = _preview_for_page(page_no, page_by_no, preview_paths)
    if not preview_rel:
        return None
    bboxes = [_numeric_bbox(block.get("bbox")) for block in blocks]
    bboxes = [bbox for bbox in bboxes if bbox]
    if not bboxes:
        return None
    preview_abs = store.resolve_case_path(case_id, preview_rel)
    if not preview_abs.exists():
        return None
    bbox = [min(row[0] for row in bboxes), min(row[1] for row in bboxes), max(row[2] for row in bboxes), max(row[3] for row in bboxes)]
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(preview_abs) as image:
            image = image.convert("RGB")
            crop_box = _preview_crop_box(blocks[0], bbox, page_by_no.get(page_no) or {}, image.size)
            if not crop_box:
                return None
            target_rel = _crop_relative_path(attachment_id, crop_id)
            target_abs = store.resolve_case_path(case_id, target_rel)
            target_abs.parent.mkdir(parents=True, exist_ok=True)
            image.crop(crop_box).save(target_abs)
    except Exception:
        return None
    text = " ".join(str(block.get("text") or "").strip() for block in blocks if str(block.get("text") or "").strip())
    return {
        "crop_id": crop_id,
        "kind": "field_context",
        "page": page_no,
        "bbox": [round(float(value), 2) for value in bbox],
        "text": _brief(text, 260),
        "locator": f"page {page_no} field_context {crop_id}",
        "crop_path": target_rel,
        "preview_path": preview_rel,
        "confidence": "medium",
        "proves": proves,
    }


def _crop_block(
    store: Any,
    case_id: str,
    attachment_id: str,
    block: dict[str, Any],
    page_by_no: dict[int, dict[str, Any]],
    preview_paths: list[str],
) -> dict[str, Any] | None:
    page_no = int(block.get("page") or 1)
    preview_rel = _preview_for_page(page_no, page_by_no, preview_paths)
    if not preview_rel:
        return None
    bbox = _numeric_bbox(block.get("bbox"))
    if not bbox:
        return None
    preview_abs = store.resolve_case_path(case_id, preview_rel)
    if not preview_abs.exists():
        return None
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(preview_abs) as image:
            image = image.convert("RGB")
            crop_box = _preview_crop_box(block, bbox, page_by_no.get(page_no) or {}, image.size)
            if not crop_box:
                return None
            target_rel = _crop_relative_path(attachment_id, str(block.get("id") or f"p{page_no}_block"))
            target_abs = store.resolve_case_path(case_id, target_rel)
            target_abs.parent.mkdir(parents=True, exist_ok=True)
            image.crop(crop_box).save(target_abs)
    except Exception:
        return None
    block_id = str(block.get("id") or "")
    locator = f"page {page_no} block {block_id}" if block_id else f"page {page_no} block"
    text = str(block.get("text") or "")
    return {
        "crop_id": block_id or Path(target_rel).stem,
        "kind": str(block.get("type") or "text_block"),
        "page": page_no,
        "bbox": [round(float(value), 2) for value in bbox],
        "text": _brief(text, 260),
        "locator": locator,
        "crop_path": target_rel,
        "preview_path": preview_rel,
        "confidence": str(block.get("confidence") or "medium"),
        "proves": _proof_label_from_text(text),
    }


def _crop_visual_region(
    store: Any,
    case_id: str,
    attachment_id: str,
    region: dict[str, Any],
    preview_paths: list[str],
) -> dict[str, Any] | None:
    preview_rel = str(region.get("preview_path") or (preview_paths[0] if preview_paths else ""))
    if not preview_rel:
        return None
    preview_abs = store.resolve_case_path(case_id, preview_rel)
    if not preview_abs.exists():
        return None
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(preview_abs) as image:
            image = image.convert("RGB")
            crop_box = _visual_region_box(region.get("bbox"), image.size)
            if not crop_box:
                return None
            region_id = str(region.get("id") or "visual_region")
            target_rel = _crop_relative_path(attachment_id, region_id)
            target_abs = store.resolve_case_path(case_id, target_rel)
            target_abs.parent.mkdir(parents=True, exist_ok=True)
            image.crop(crop_box).save(target_abs)
    except Exception:
        return None
    page_no = int(region.get("page") or 1)
    region_id = str(region.get("id") or Path(target_rel).stem)
    return {
        "crop_id": region_id,
        "kind": str(region.get("type") or "visual_region"),
        "page": page_no,
        "bbox": list(crop_box),
        "text": _brief(region.get("note") or region.get("type") or "", 260),
        "locator": f"visual_region:{region_id}",
        "crop_path": target_rel,
        "preview_path": preview_rel,
        "confidence": str(region.get("confidence") or "medium"),
        "proves": _proof_label_from_text(str(region.get("note") or region.get("type") or "")),
    }


def _attach_field_crop_refs(
    field_inventory: list[dict[str, Any]],
    block_crops: list[dict[str, Any]],
    preview_paths: list[str],
) -> list[dict[str, Any]]:
    by_locator = {str(crop.get("locator") or ""): crop for crop in block_crops if isinstance(crop, dict)}
    visual_signature = next(
        (
            crop
            for crop in block_crops
            if str(crop.get("kind") or "") in {"signature_context", "signature_candidate"}
        ),
        None,
    )
    result: list[dict[str, Any]] = []
    for field in field_inventory:
        if not isinstance(field, dict):
            continue
        enriched = dict(field)
        crop = by_locator.get(str(field.get("locator") or ""))
        if field.get("field") == "signature_or_authorized_signatory" and visual_signature:
            crop = visual_signature
        if field.get("field") == "amount_total" and crop:
            quote = _compact_match_text(field.get("source_quote") or "")
            crop_text = _compact_match_text(crop.get("text") or "")
            if "total" in quote and "total" not in crop_text:
                context_crop = next(
                    (
                        row
                        for row in block_crops
                        if str(row.get("kind") or "") == "field_context"
                        and "total" in _compact_match_text(row.get("text") or "")
                        and _compact_match_text(field.get("value") or "") in _compact_match_text(row.get("text") or "")
                    ),
                    None,
                )
                if context_crop:
                    crop = context_crop
        if crop is None:
            crop = _match_crop_by_text(field, block_crops)
        if crop:
            enriched["crop_path"] = crop.get("crop_path", "")
            enriched["block_id"] = crop.get("crop_id", "")
            enriched["preview_path"] = crop.get("preview_path", "")
            enriched["proof_label"] = _field_proof_label(enriched)
            enriched["crop_status"] = "cropped"
            if str(enriched.get("locator") or "") == "dossier_text_regex":
                enriched["locator"] = crop.get("locator") or enriched.get("locator")
        else:
            enriched["preview_path"] = enriched.get("preview_path") or (preview_paths[0] if preview_paths else "")
            enriched["proof_label"] = _field_proof_label(enriched)
            enriched["crop_status"] = "full_page_fallback" if enriched.get("preview_path") else "missing_crop"
        result.append(enriched)
    return result


def _match_crop_by_text(field: dict[str, Any], block_crops: list[dict[str, Any]]) -> dict[str, Any] | None:
    values = [
        _compact_match_text(field.get("source_quote")),
        _compact_match_text(field.get("value")),
    ]
    values = [value for value in values if len(value) >= 4]
    if not values:
        return None
    preferred = [crop for crop in block_crops if str(crop.get("kind") or "") != "field_context"]
    fallback = [crop for crop in block_crops if str(crop.get("kind") or "") == "field_context"]
    for crop in [*preferred, *fallback]:
        text = _compact_match_text(crop.get("text"))
        if not text:
            continue
        if any(value in text or (len(text) >= 18 and text in value) for value in values):
            return crop
    return None


def _preview_for_page(page_no: int, page_by_no: dict[int, dict[str, Any]], preview_paths: list[str]) -> str:
    page = page_by_no.get(page_no) or {}
    preview = str(page.get("preview_path") or "")
    if preview:
        return preview
    index = page_no - 1
    if 0 <= index < len(preview_paths):
        return preview_paths[index]
    return preview_paths[0] if preview_paths else ""


def _preview_crop_box(
    block: dict[str, Any],
    bbox: list[float],
    page: dict[str, Any],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    img_w, img_h = image_size
    if str(block.get("source") or "") == "pymupdf":
        page_w = float(page.get("width") or 0)
        page_h = float(page.get("height") or 0)
        if page_w <= 0 or page_h <= 0:
            return None
        x0, y0, x1, y1 = bbox
        box = (x0 * img_w / page_w, y0 * img_h / page_h, x1 * img_w / page_w, y1 * img_h / page_h)
    else:
        box = tuple(bbox[:4])
    return _padded_box(box, image_size)


def _visual_region_box(value: Any, image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    numeric = _numeric_bbox(value)
    if numeric:
        return _padded_box(tuple(numeric), image_size)
    tokens = [str(item).lower() for item in value] if isinstance(value, list) else [str(value).lower()]
    img_w, img_h = image_size
    if "right_half" in tokens and "middle_to_lower_page" in tokens:
        return _padded_box((img_w * 0.66, img_h * 0.38, img_w * 0.98, img_h * 0.56), image_size)
    return None


def _numeric_bbox(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or not value:
        return []
    if len(value) >= 4 and all(isinstance(item, (int, float)) for item in value[:4]):
        x0, y0, x1, y1 = [float(item) for item in value[:4]]
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
    points: list[tuple[float, float]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and all(isinstance(part, (int, float)) for part in item[:2]):
            points.append((float(item[0]), float(item[1])))
    if not points:
        return []
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _padded_box(box: tuple[float, float, float, float], image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    img_w, img_h = image_size
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return None
    pad_x = max(8, int((x1 - x0) * 0.12))
    pad_y = max(8, int((y1 - y0) * 0.35))
    left = max(0, int(x0) - pad_x)
    top = max(0, int(y0) - pad_y)
    right = min(img_w, int(x1) + pad_x)
    bottom = min(img_h, int(y1) + pad_y)
    if right - left < 12 or bottom - top < 10:
        return None
    return (left, top, right, bottom)


def _crop_relative_path(attachment_id: str, crop_id: str) -> str:
    safe_attachment = _safe_name(attachment_id or "attachment")
    safe_crop = _safe_name(crop_id or "crop")
    return f"evidence/crops/{safe_attachment}/{safe_crop}.png"


def _proof_label_from_text(text: str) -> str:
    lower = str(text or "").lower()
    if "invoice no" in lower or "invoice number" in lower:
        return "证明发票编号字段可见"
    if "sold by" in lower or "supplier" in lower or "vendor" in lower:
        return "证明供应商字段可见"
    if "billing address" in lower or "buyer" in lower or "customer" in lower:
        return "证明购买方字段可见"
    if "invoice date" in lower or re.search(r"\bdate\b", lower):
        return "证明发票日期字段可见"
    if "grand total" in lower or "amount due" in lower or "total amount" in lower:
        return "证明总金额字段可见"
    if "tax" in lower or "vat" in lower or "gst" in lower or "%" in lower:
        return "证明税额或币种字段可见"
    if "authorized signatory" in lower or "signature" in lower or "signatory" in lower:
        return "证明签名/授权签署区域可见"
    if "iban" in lower or "bic" in lower or "swift" in lower or "bank" in lower or "account" in lower:
        return "证明银行账户/付款信息字段可见"
    if any(token in lower for token in ("product", "title", "qty", "price", "memory", "service")):
        return "证明商品/服务行项目字段可见"
    return "证明原文区块可追溯"


def _field_proof_label(field: dict[str, Any]) -> str:
    field_name = str(field.get("field") or "")
    if field_name == "bank_details":
        return "证明银行账户/付款信息字段可见"
    labels = {
        "invoice_number": "证明发票编号字段可见",
        "supplier": "证明供应商字段可见",
        "buyer": "证明购买方字段可见",
        "invoice_date": "证明发票日期字段可见",
        "amount_total": "证明总金额字段可见",
        "currency_tax": "证明币种/税额字段可见",
        "line_items_product_title": "证明商品/服务行项目字段可见",
        "signature_or_authorized_signatory": "证明签名/授权签署区域可见",
    }
    return labels.get(field_name) or _proof_label_from_text(str(field.get("source_quote") or field.get("value") or ""))


def _compact_match_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return clean[:80] or "item"


def _visual_regions(item: dict[str, Any], preview_paths: list[str]) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    notes = [str(note) for note in item.get("visual_notes") or [] if str(note)]
    for index, note in enumerate(notes, start=1):
        lower = note.lower()
        if "signature" in lower or "signatory" in lower:
            regions.append(
                {
                    "id": f"visual_sig_{index:03d}",
                    "page": 1,
                    "type": "signature_candidate",
                    "bbox": ["right_half", "middle_to_lower_page"],
                    "preview_path": preview_paths[0] if preview_paths else "",
                    "confidence": "medium" if "present" in lower else "low",
                    "note": _brief(note, 260),
                    "source": "local_preview_analysis",
                }
            )
    return regions


def _page_summaries(
    pages: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for page in pages[:12]:
        page_no = int(page.get("page") or 1)
        page_blocks = [block for block in blocks if int(block.get("page") or 0) == page_no]
        page_tables = [table for table in tables if int(table.get("page") or 0) == page_no]
        summaries.append(
            {
                "page": page_no,
                "text_preview": _brief(page.get("text"), 420),
                "block_count": len(page_blocks),
                "table_count": len(page_tables),
                "preview_path": page.get("preview_path", ""),
                "quality_notes": list(page.get("quality_notes") or [])[:6],
            }
        )
    return summaries


def _visual_check(
    *,
    store: Any,
    case_id: str,
    item: dict[str, Any],
    content_kind: str,
    full_text: str,
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    field_inventory: list[dict[str, Any]],
    page_summaries: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
    preview_paths: list[str],
    extraction_methods: list[str],
    quality_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    fields = {str(field.get("field") or ""): field for field in field_inventory if isinstance(field, dict)}
    text = str(full_text or "")
    lower = text.lower()
    preview_integrity = _preview_integrity_notes(store, case_id, preview_paths)
    section = {
        "invoice_title_or_header": _section_visible(
            bool(re.search(r"\b(invoice|tax invoice|retail invoices?/bill|bill)\b", lower)) or "发票" in text
        ),
        "supplier_block": _section_visible("supplier" in fields or _has_any(lower, ["sold by", "supplier", "vendor", "bill from"])),
        "buyer_block": _section_visible("buyer" in fields or _has_any(lower, ["buyer", "bill to", "billing address", "sold to", "customer"])),
        "invoice_number": _section_visible("invoice_number" in fields),
        "invoice_date": _section_visible("invoice_date" in fields),
        "amount_or_total_block": _section_visible("amount_total" in fields or _has_any(lower, ["grand total", "total amount", "amount due"])),
        "currency_or_tax": _section_visible("currency_tax" in fields or _has_any(lower, [" tax", "vat", "gst", "cgst", "sgst", "usd", "eur", "gbp", "inr", "cny", "rmb"])),
        "line_table": _line_table_visibility(fields, tables, lower),
        "signature_or_signatory": _signature_visibility(fields, visual_regions, lower),
    }
    visible_core = sum(
        1
        for key in ("invoice_number", "supplier_block", "buyer_block", "invoice_date", "amount_or_total_block")
        if section.get(key) == "yes"
    )
    if section["invoice_title_or_header"] == "yes" and visible_core >= 3:
        looks_like_invoice = "yes"
    elif visible_core >= 3:
        looks_like_invoice = "unclear"
    elif _has_any(lower, ["purchase requisition", "approval request", "clear invoice", "process log", "goods receipt"]) and visible_core < 2:
        looks_like_invoice = "no"
    else:
        looks_like_invoice = "unclear" if visible_core else "no"

    ocr_quality = _ocr_quality(content_kind, text, field_inventory, extraction_methods, quality_notes, warnings)
    page_integrity = _page_integrity(page_summaries, preview_paths, quality_notes, warnings, preview_integrity)
    layout_check = _layout_check(fields, tables, lower)
    same_source = _same_source_check(item, fields)
    return {
        "looks_like_invoice": looks_like_invoice,
        "visible_sections": section,
        "field_visibility": _field_visibility(field_inventory, preview_paths),
        "same_source_check": same_source,
        "ocr_quality": ocr_quality,
        "page_integrity": page_integrity,
        "layout_check": layout_check,
        "limitations": [
            "visual_check_does_not_authenticate_supplier",
            "visual_check_does_not_rule_out_forgery",
            "visual_check_does_not_rule_out_duplicate_payment",
            "visual_check_does_not_authorize_payment",
        ],
    }


def _section_visible(condition: bool) -> str:
    return "yes" if condition else "no"


def _has_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _line_table_visibility(fields: dict[str, dict[str, Any]], tables: list[dict[str, Any]], lower: str) -> str:
    if "line_items_product_title" in fields or tables or re.search(r"\b(qty|quantity)\b.*\b(price|rate|tax|total)\b", lower, re.DOTALL):
        return "yes"
    if _has_any(lower, ["service fee", "subscription", "professional services"]):
        return "not_applicable"
    return "unclear"


def _signature_visibility(fields: dict[str, dict[str, Any]], visual_regions: list[dict[str, Any]], lower: str) -> str:
    if "signature_or_authorized_signatory" in fields or visual_regions or _has_any(lower, ["authorized signatory", "signature", "signed by"]):
        return "yes"
    return "unclear"


def _field_visibility(field_inventory: list[dict[str, Any]], preview_paths: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for field in field_inventory[:24]:
        if not isinstance(field, dict):
            continue
        preview_ref = field.get("preview_path") or (preview_paths[0] if preview_paths else "")
        value = str(field.get("value") or "")
        quote = str(field.get("source_quote") or value)
        records.append(
            {
                "field": str(field.get("field") or ""),
                "visible": "yes" if value or quote else "unclear",
                "ocr_text": _brief(value or quote, 160),
                "locator": str(field.get("locator") or ""),
                "preview_ref": str(preview_ref),
                "crop_path": str(field.get("crop_path") or ""),
                "crop_status": str(field.get("crop_status") or ""),
                "proof_label": str(field.get("proof_label") or ""),
                "confidence": str(field.get("confidence") or "medium"),
            }
        )
    return records


def _same_source_check(item: dict[str, Any], fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
    critical = ["invoice_number", "supplier", "buyer", "invoice_date", "amount_total"]
    present = [field for field in critical if field in fields]
    missing = [field for field in critical if field not in fields]
    original_ref = str(item.get("original_ref") or "")
    if original_ref and len(present) >= 4:
        status = "pass"
    elif original_ref and present:
        status = "partial"
    else:
        status = "unclear"
    return {
        "status": status,
        "source_doc_id": str(item.get("attachment_id") or item.get("name") or ""),
        "original_ref": original_ref,
        "critical_fields_present": present,
        "critical_fields_missing": missing,
        "mixed_sources": [],
    }


def _ocr_quality(
    content_kind: str,
    text: str,
    field_inventory: list[dict[str, Any]],
    extraction_methods: list[str],
    quality_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    issue_text = " ".join(str(value).lower() for value in [*quality_notes, *warnings])
    source_issue_text = issue_text.replace("attachment_text_was_truncated_in_tool_response", " ")
    source_issue_text = source_issue_text.replace("content_preview_truncated", " ")
    field_count = len(field_inventory)
    if not text.strip() or any(marker in source_issue_text for marker in ["ocr_empty", "ocr_failed", "no_extracted_text_available"]):
        status = "failed"
    elif any(marker in source_issue_text for marker in ["sparse", "weak", "truncated", "table_extraction_failed"]) or field_count < 3:
        status = "weak"
    elif "ocr" in " ".join(extraction_methods).lower() or content_kind == "image":
        status = "usable"
    else:
        status = "good"
    issues = _unique_strings([note for note in [*quality_notes, *warnings] if _is_quality_issue(note)])[:12]
    return {
        "status": status,
        "issues": issues,
        "methods": extraction_methods[:8],
    }


def _is_quality_issue(note: Any) -> bool:
    lower = str(note or "").lower()
    if "attachment_text_was_truncated_in_tool_response" in lower or "content_preview_truncated" in lower:
        return False
    return any(marker in lower for marker in ["ocr", "sparse", "failed", "truncated", "empty", "unreadable", "crop", "edge"])


def _page_integrity(
    page_summaries: list[dict[str, Any]],
    preview_paths: list[str],
    quality_notes: list[str],
    warnings: list[str],
    preview_integrity: list[str],
) -> dict[str, Any]:
    issue_text = " ".join(str(value).lower() for value in [*quality_notes, *warnings, *preview_integrity])
    source_issue_text = issue_text.replace("attachment_text_was_truncated_in_tool_response", " ")
    source_issue_text = source_issue_text.replace("content_preview_truncated", " ")
    integrity_warnings = _unique_strings(
        [note for note in [*quality_notes, *warnings, *preview_integrity] if _is_quality_issue(note)]
    )[:12]
    if not preview_paths:
        status = "warning"
        integrity_warnings.append("preview_missing")
    elif any(marker in source_issue_text for marker in ["crop", "edge", "truncated", "failed", "unreadable"]):
        status = "warning"
    elif page_summaries:
        status = "ok"
    else:
        status = "unclear"
    return {
        "status": status,
        "preview_available": bool(preview_paths),
        "page_count": len(page_summaries),
        "warnings": _unique_strings(integrity_warnings)[:12],
    }


def _preview_integrity_notes(store: Any, case_id: str, preview_paths: list[str]) -> list[str]:
    if not preview_paths:
        return []
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except Exception:
        return []
    notes: list[str] = []
    for preview in preview_paths[:3]:
        try:
            path = store.resolve_case_path(case_id, preview)
            image = Image.open(path).convert("L")
            width, height = image.size
            bbox = image.point(lambda value: 255 if value < 245 else 0).getbbox()
            if not bbox:
                notes.append(f"preview_blank_or_too_light:{Path(preview).name}")
                continue
            left, top, right, bottom = bbox
            edge_px = 1
            touches_left = left <= edge_px
            touches_right = width - right <= edge_px
            touches_top = top <= edge_px
            touches_bottom = height - bottom <= edge_px
            if (touches_left and touches_right) or (touches_top and touches_bottom):
                notes.append(f"possible_page_crop:{Path(preview).name}")
        except Exception as exc:
            notes.append(f"preview_integrity_unavailable:{type(exc).__name__}")
    return notes


def _layout_check(fields: dict[str, dict[str, Any]], tables: list[dict[str, Any]], lower: str) -> dict[str, Any]:
    line_table = _line_table_visibility(fields, tables, lower)
    if "amount_total" in fields and _has_any(lower, ["grand total", "total amount", "amount due", "total"]):
        total_block = "present"
    elif "amount_total" in fields:
        total_block = "unclear"
    else:
        total_block = "absent"
    notes: list[str] = []
    if line_table == "unclear":
        notes.append("line_table_not_clearly_detected")
    if total_block != "present":
        notes.append("total_block_not_clearly_detected")
    return {
        "line_table_structure": line_table,
        "total_block_structure": total_block,
        "notes": notes,
    }


def _compact_visual_check(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "looks_like_invoice": value.get("looks_like_invoice", ""),
        "visible_sections": value.get("visible_sections") or {},
        "field_visibility": list(value.get("field_visibility") or [])[:16],
        "same_source_check": value.get("same_source_check") or {},
        "ocr_quality": value.get("ocr_quality") or {},
        "page_integrity": value.get("page_integrity") or {},
        "layout_check": value.get("layout_check") or {},
        "limitations": list(value.get("limitations") or [])[:6],
    }


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows[:40]]
    header = padded[0]
    body = padded[1:]
    lines = [
        "| " + " | ".join(_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def _rows_to_csv(rows: list[list[str]]) -> str:
    return "\n".join(",".join(_csv_cell(cell) for cell in row) for row in rows[:80])


def _pages_to_markdown(pages: list[dict[str, Any]], tables: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    for table in tables:
        if not isinstance(table, dict):
            continue
        try:
            page_no = int(table.get("page") or 0)
        except (TypeError, ValueError):
            page_no = 0
        tables_by_page.setdefault(page_no, []).append(table)
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_no = page.get("page") or ""
        text = str(page.get("text") or "").strip()
        if text:
            parts.append(f"## Page {page_no}\n\n{text}")
        for table in tables_by_page.get(int(page_no or 0), []):
            markdown = str(table.get("markdown") or "").strip()
            if markdown:
                parts.append(f"### Page {page_no} table\n\n{markdown}")
    return "\n\n".join(parts).strip()


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


def _csv_cell(value: Any) -> str:
    text = str(value or "").replace('"', '""')
    if any(char in text for char in [",", "\n", '"']):
        return f'"{text}"'
    return text


def _merge_texts(parts: list[str]) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text[:1200]).lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(text)
    return "\n\n".join(merged).strip()


def _clean_value(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text.strip(" :-#\t\r\n")


def _valid_candidate(field: str, value: str, quote: str) -> bool:
    lower_value = value.lower()
    lower_quote = quote.lower()
    if field in {"invoice_number", "po_ref", "grn_ref"}:
        if not any(char.isdigit() for char in value):
            return False
        if lower_value in {"oice", "licy", "number", "date"}:
            return False
    if field == "amount_total":
        number = _number_value(value)
        if number is None:
            return False
        if number < 10 and "grand" not in lower_quote and "amount" not in lower_quote:
            return False
    if field == "line_items_product_title" and lower_value in {"title", "product", "description", "item"}:
        return False
    if field == "bank_details" and not _looks_like_bank_details(f"{value} {quote}"):
        return False
    return True


def _candidate_score(field: str, value: str, quote: str) -> int:
    score = 10
    lower_quote = quote.lower()
    if field == "invoice_number":
        if "invoice no" in lower_quote or "invoice number" in lower_quote or "invoice #" in lower_quote:
            score += 50
        if any(sep in value for sep in ("-", "_", "/")):
            score += 15
        score += min(len(value), 20)
    elif field == "amount_total":
        number = _number_value(value) or 0
        if "grand total" in lower_quote:
            score += 80
        elif "amount due" in lower_quote or "total amount" in lower_quote:
            score += 55
        elif "total" in lower_quote:
            score += 20
        score += min(int(number // 10), 60)
    elif field in {"po_ref", "grn_ref"}:
        if field == "po_ref" and "purchase order" in lower_quote:
            score += 40
        if field == "grn_ref" and "goods receipt" in lower_quote:
            score += 40
        score += min(len(value), 20)
    elif field == "currency_tax":
        if any(symbol in quote for symbol in ("₹", "$", "€", "£", "¥", "￥")):
            score += 45
        if any(token in lower_quote for token in (" inr", "rs", "usd", "eur", "gbp", "cny", "rmb")):
            score += 35
        if any(token in lower_quote for token in ("tax", "vat", "gst", "cst", "cgst", "sgst")):
            score += 25
        if "%" in quote:
            score += 20
    elif field == "signature_or_authorized_signatory":
        if "authorized signatory" in lower_quote:
            score += 30
    elif field == "invoice_date":
        if "invoice date" in lower_quote:
            score += 70
        elif "date" in lower_quote:
            score += 45
    elif field == "supplier":
        if any(token in lower_quote for token in ("supplier", "vendor", "sold by", "bill from")):
            score += 70
        elif any(token in lower_quote for token in ("installations", "services", "ltd", "gmbh")):
            score += 55
    elif field == "buyer":
        if any(token in lower_quote for token in ("buyer", "bill to", "billing address", "sold to", "customer")):
            score += 70
        elif "sap se" in lower_quote:
            score += 65
    elif field == "bank_details":
        if "iban" in lower_quote:
            score += 50
        if "bic" in lower_quote or "swift" in lower_quote:
            score += 35
        if "bank" in lower_quote:
            score += 25
    return score


def _number_value(value: str) -> float | None:
    match = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _brief(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 14].rstrip() + "...[truncated]"


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
