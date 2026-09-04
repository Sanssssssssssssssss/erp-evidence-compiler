from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from app.state.attachment_manifest import EXCLUDED_STATUSES, load_attachment_manifest, manifest_status_for_ref
from app.state.case_store import CaseStore


TEXT_SNAPSHOT_SUFFIXES = {".csv", ".json", ".log", ".md", ".txt", ".xml", ".yaml", ".yml"}
IMAGE_SNAPSHOT_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
RAW_MATERIAL_APPENDIX_HEADING = "原始材料附录"
RAW_MATERIAL_APPENDIX_DISCLAIMER = "仅供人工核对，不构成系统结论；以正文 canonical Proof 为准。"


def render_pdf(case_id: str, markdown_path: str, pdf_path: str | None = None) -> dict[str, Any]:
    return render_pdf_from_store(CaseStore(), case_id, markdown_path, pdf_path)


def render_pdf_from_store(
    store: CaseStore,
    case_id: str,
    markdown_path: str,
    pdf_path: str | None = None,
) -> dict[str, Any]:
    source = store.resolve_case_path(case_id, markdown_path)
    target_rel = pdf_path or markdown_path.rsplit(".", 1)[0] + ".pdf"
    target = store.resolve_case_path(case_id, target_rel)
    target.parent.mkdir(parents=True, exist_ok=True)

    markdown = source.read_text(encoding="utf-8")

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus.tableofcontents import TableOfContents
    from reportlab.platypus import (
        HRFlowable,
        Image as FlowableImage,
        LongTable,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        TableStyle,
    )

    font_name = _register_report_font(pdfmetrics, TTFont)
    styles = _build_styles(font_name, getSampleStyleSheet(), ParagraphStyle, TA_CENTER)
    toc = _build_table_of_contents(TableOfContents, ParagraphStyle, styles, font_name)

    report_doc_template = _report_doc_template(SimpleDocTemplate)
    doc = report_doc_template(
        str(target),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title="发票付款材料审查报告",
    )
    story = _markdown_to_flowables(
        markdown,
        styles,
        doc.width,
        LongTable,
        TableStyle,
        Paragraph,
        Spacer,
        HRFlowable,
        colors,
        PageBreak,
        toc,
    )
    snapshots = _build_evidence_snapshots(store, case_id)
    _append_snapshot_section(story, snapshots, styles, doc.width, FlowableImage, Paragraph, Spacer, PageBreak)

    def draw_page(canvas: Any, _: Any) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#222222"))
        canvas.drawString(doc.leftMargin, A4[1] - 12 * mm, "发票付款材料审查报告")
        canvas.drawRightString(A4[0] - doc.rightMargin, 10 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.setStrokeColor(colors.HexColor("#888888"))
        canvas.line(doc.leftMargin, A4[1] - 15 * mm, A4[0] - doc.rightMargin, A4[1] - 15 * mm)
        canvas.restoreState()

    doc.multiBuild(story, onFirstPage=draw_page, onLaterPages=draw_page)
    rendered_snapshots = [item for item in snapshots if item.get("status") == "rendered"]
    rendered_field_crops = [item for item in rendered_snapshots if item.get("snapshot_kind") == "field_crop"]
    return {
        "case_id": case_id,
        "markdown_path": markdown_path,
        "pdf_path": target_rel,
        "status": "success",
        "page_count": int(getattr(doc, "page", 0) or 0),
        "evidence_snapshot_count": len(rendered_snapshots),
        "field_crop_count": len(rendered_field_crops),
        "snapshot_paths": [str(item.get("snapshot_path", "")) for item in rendered_snapshots if item.get("snapshot_path")],
    }


def _report_doc_template(simple_doc_template: Any) -> Any:
    class ReportDocTemplate(simple_doc_template):
        def afterFlowable(self, flowable: Any) -> None:  # noqa: N802 - ReportLab callback name
            key = getattr(flowable, "_bookmarkName", "")
            if not key:
                return
            text = str(getattr(flowable, "_plainText", "") or "")
            if not text:
                return
            level = int(getattr(flowable, "_tocLevel", 0) or 0)
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=False)
            self.notify("TOCEntry", (level, text, self.page, key))

    return ReportDocTemplate


def _register_report_font(pdfmetrics: Any, tt_font: Any) -> str:
    for font_path in (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    ):
        if not font_path.exists():
            continue
        try:
            pdfmetrics.registerFont(tt_font("ReportCJK", str(font_path)))
            return "ReportCJK"
        except Exception:
            continue
    return "Helvetica"


def _build_styles(font_name: str, sample: Any, paragraph_style: Any, align_center: int) -> dict[str, Any]:
    base = sample["BodyText"]
    return {
        "title": paragraph_style(
            "ReportTitle",
            parent=base,
            fontName=font_name,
            fontSize=28,
            leading=36,
            textColor="#111111",
            alignment=align_center,
            spaceAfter=22,
            wordWrap="CJK",
        ),
        "h2": paragraph_style(
            "ReportH2",
            parent=base,
            fontName=font_name,
            fontSize=17,
            leading=22,
            textColor="#111111",
            spaceBefore=16,
            spaceAfter=10,
            wordWrap="CJK",
        ),
        "risk_h2": paragraph_style(
            "RiskH2",
            parent=base,
            fontName=font_name,
            fontSize=17,
            leading=22,
            textColor="#111111",
            spaceBefore=16,
            spaceAfter=10,
            wordWrap="CJK",
        ),
        "h3": paragraph_style(
            "ReportH3",
            parent=base,
            fontName=font_name,
            fontSize=12,
            leading=16,
            textColor="#111111",
            spaceBefore=10,
            spaceAfter=6,
            wordWrap="CJK",
        ),
        "h4": paragraph_style(
            "ReportH4",
            parent=base,
            fontName=font_name,
            fontSize=10,
            leading=14,
            textColor="#111111",
            spaceBefore=8,
            spaceAfter=4,
            wordWrap="CJK",
        ),
        "body": paragraph_style(
            "ReportBody",
            parent=base,
            fontName=font_name,
            fontSize=9,
            leading=13,
            textColor="#111827",
            spaceAfter=5,
            wordWrap="CJK",
        ),
        "bullet": paragraph_style(
            "ReportBullet",
            parent=base,
            fontName=font_name,
            fontSize=9,
            leading=13,
            leftIndent=14,
            bulletIndent=4,
            textColor="#111827",
            spaceAfter=3,
            wordWrap="CJK",
        ),
        "cell": paragraph_style(
            "ReportCell",
            parent=base,
            fontName=font_name,
            fontSize=7,
            leading=9,
            textColor="#111827",
            wordWrap="CJK",
        ),
        "small": paragraph_style(
            "ReportSmall",
            parent=base,
            fontName=font_name,
            fontSize=7,
            leading=9,
            textColor="#333333",
            wordWrap="CJK",
        ),
    }


def _build_table_of_contents(table_of_contents: Any, paragraph_style: Any, styles: dict[str, Any], font_name: str) -> Any:
    toc = table_of_contents()
    toc.levelStyles = [
        paragraph_style(
            "TOCChapter",
            parent=styles["body"],
            fontName=font_name,
            fontSize=10,
            leading=14,
            leftIndent=0,
            firstLineIndent=0,
            spaceBefore=4,
            wordWrap="CJK",
        ),
        paragraph_style(
            "TOCSection",
            parent=styles["body"],
            fontName=font_name,
            fontSize=9,
            leading=12,
            leftIndent=14,
            firstLineIndent=0,
            spaceBefore=2,
            wordWrap="CJK",
        ),
    ]
    return toc


def _markdown_to_flowables(
    markdown: str,
    styles: dict[str, Any],
    width: float,
    long_table: Any,
    table_style: Any,
    paragraph: Any,
    spacer: Any,
    hr_flowable: Any,
    colors: Any,
    page_break: Any,
    toc: Any,
) -> list[Any]:
    story: list[Any] = []
    lines = markdown.splitlines()
    index = 0
    toc_inserted = False
    heading_index = 0
    skip_generated_toc = False
    skip_renderer_snapshot_section = False
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if skip_renderer_snapshot_section:
            if _is_renderer_owned_snapshot_heading(stripped) or not stripped.startswith("#"):
                index += 1
                continue
            skip_renderer_snapshot_section = False
        if skip_generated_toc and not stripped:
            index += 1
            continue
        if skip_generated_toc and not stripped.startswith("#"):
            index += 1
            continue
        if skip_generated_toc and stripped.startswith("#"):
            skip_generated_toc = False
        if not stripped:
            if not _last_significant_is_page_break(story):
                story.append(spacer(1, 5))
            index += 1
            continue
        if _is_toc_heading(stripped):
            skip_generated_toc = True
            index += 1
            continue
        if _is_renderer_owned_snapshot_heading(stripped):
            skip_renderer_snapshot_section = True
            index += 1
            continue
        if _is_table_line(stripped):
            table_lines: list[str] = []
            while index < len(lines) and _is_table_line(lines[index].strip()):
                table_lines.append(lines[index].strip())
                index += 1
            table = _table_from_markdown(table_lines, styles, width, long_table, table_style, paragraph, colors)
            if table is not None:
                story.append(table)
                story.append(spacer(1, 8))
            continue
        if stripped in {"---", "***", "___"}:
            story.append(spacer(1, 4))
            story.append(hr_flowable(width="100%", thickness=0.7, color=colors.HexColor("#888888")))
            story.append(spacer(1, 6))
            index += 1
            continue
        if stripped.startswith("# "):
            story.append(paragraph(_inline_text(stripped[2:]), styles["title"]))
            if not toc_inserted:
                _append_generated_toc(story, styles, paragraph, spacer, page_break, toc)
                toc_inserted = True
        elif stripped.startswith("## "):
            text = stripped[3:]
            if not toc_inserted:
                _append_generated_toc(story, styles, paragraph, spacer, page_break, toc)
                toc_inserted = True
            if _is_chapter_heading(text) and _needs_page_break(story):
                story.append(page_break())
            style = styles["risk_h2"] if "风险" in text else styles["h2"]
            heading_index += 1
            story.append(_heading_paragraph(text, style, paragraph, level=0, index=heading_index))
        elif stripped.startswith("### "):
            text = stripped[4:]
            heading_index += 1
            story.append(_heading_paragraph(text, styles["h3"], paragraph, level=1, index=heading_index))
        elif stripped.startswith("#### "):
            text = stripped[5:]
            story.append(paragraph(_inline_text(text), styles["h4"]))
        elif stripped.startswith("##### "):
            text = stripped.lstrip("#").strip()
            story.append(paragraph(_inline_text(text), styles["h4"]))
        elif stripped.startswith("- "):
            story.append(paragraph(_inline_text(stripped[2:]), styles["bullet"], bulletText="•"))
        elif re.match(r"^\d+\.\s+", stripped):
            story.append(paragraph(_inline_text(stripped), styles["body"]))
        else:
            story.append(paragraph(_inline_text(stripped), styles["body"]))
        index += 1
    return story or [paragraph("空报告", styles["body"])]


def _append_generated_toc(
    story: list[Any],
    styles: dict[str, Any],
    paragraph: Any,
    spacer: Any,
    page_break: Any,
    toc: Any,
) -> None:
    story.append(paragraph("目录", styles["h2"]))
    story.append(spacer(1, 6))
    story.append(toc)
    story.append(page_break())


def _heading_paragraph(text: str, style: Any, paragraph: Any, *, level: int, index: int) -> Any:
    flowable = paragraph(_inline_text(text), style)
    flowable._bookmarkName = _bookmark_key(text, index)
    flowable._tocLevel = level
    flowable._plainText = text
    return flowable


def _bookmark_key(text: str, index: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", text).strip("_")
    return f"heading_{index}_{slug[:32] or 'section'}"


def _is_toc_heading(stripped: str) -> bool:
    return bool(re.fullmatch(r"#{2,6}\s*目录\s*", stripped))


def _is_renderer_owned_snapshot_heading(stripped: str) -> bool:
    headings = (
        "字段截图",
        "字段截图与证明点",
        "原始附件截图",
        "原始附件预览",
        "证据截图索引",
        RAW_MATERIAL_APPENDIX_HEADING,
    )
    match = re.fullmatch(r"#{2,6}\s*(.*?)\s*", stripped)
    return bool(match and match.group(1) in headings)


def _is_chapter_heading(text: str) -> bool:
    return bool(re.match(r"^第[一二三四五六七八九十百0-9]+章\b", text.strip()))


def _needs_page_break(story: list[Any]) -> bool:
    return bool(story) and not _last_significant_is_page_break(story)


def _last_significant_is_page_break(story: list[Any]) -> bool:
    for item in reversed(story):
        if item.__class__.__name__ == "Spacer":
            continue
        return item.__class__.__name__ == "PageBreak"
    return False


def _is_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _table_from_markdown(
    table_lines: list[str],
    styles: dict[str, Any],
    width: float,
    long_table: Any,
    table_style: Any,
    paragraph: Any,
    colors: Any,
) -> Any | None:
    rows: list[list[str]] = []
    for line in table_lines:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        return None
    columns = max(len(row) for row in rows)
    normalized = [row + [""] * (columns - len(row)) for row in rows]
    cell_style = styles["cell"]
    data = [[paragraph(_inline_text(_short_cell(cell)), cell_style) for cell in row] for row in normalized]
    table = long_table(data, colWidths=_column_widths(normalized, width), repeatRows=1)
    table.setStyle(
        table_style(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111111")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#111111")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#777777")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _column_widths(rows: list[list[str]], available_width: float) -> list[float]:
    columns = max(len(row) for row in rows)
    if columns == 1:
        return [available_width]
    if columns == 2:
        ratios = [0.28, 0.72]
    elif columns == 3:
        ratios = [0.22, 0.28, 0.50]
    elif columns == 4:
        ratios = [0.20, 0.25, 0.25, 0.30]
    elif columns == 5:
        ratios = [0.16, 0.18, 0.20, 0.20, 0.26]
    elif columns == 8:
        ratios = [0.09, 0.24, 0.12, 0.10, 0.12, 0.11, 0.10, 0.12]
    else:
        ratios = [1 / columns] * columns
    total = sum(ratios)
    return [available_width * ratio / total for ratio in ratios[:columns]]


def _inline_text(text: str) -> str:
    clean = text.replace("**", "").replace("__", "")
    exact = {
        "full": "完整支持",
        "partial": "部分支持",
        "none": "不支持",
        "high": "高",
        "medium": "中",
        "low": "低",
    }
    stripped = clean.strip().lower()
    if stripped in exact:
        clean = exact[stripped]
    replacements = {
        "Claim-to-Evidence Matrix": "结论与证据矩阵",
        "Evidence ID": "证据编号",
        "evidence id": "证据编号",
        "Requirements": "支持要求",
        "Reviewer conclusion": "审核结论",
        "Reviewer结论": "审核结论",
        "Claim": "主张",
        "line_item_count": "行项目数量",
        "crop_path": "截图路径",
    }
    for old, new in replacements.items():
        clean = clean.replace(old, new)
    clean = clean.replace("✓ ", "").replace("✓", "是")
    clean = clean.replace("✗", "否")
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    return html.escape(clean)


def _short_cell(text: str, limit: int = 360) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 12].rstrip() + " ...[截断]"


def _build_evidence_snapshots(store: CaseStore, case_id: str) -> list[dict[str, Any]]:
    case_root = store.ensure_case_dirs(case_id)
    evidence_snapshots = _snapshots_from_case_evidence(store, case_id)
    seen_paths = {str(item.get("snapshot_path") or "") for item in evidence_snapshots}
    evidence_snapshots.extend(_manifest_original_snapshots(store, case_id, seen_paths=seen_paths))
    evidence_snapshots = _dedupe_snapshots(evidence_snapshots)
    if evidence_snapshots:
        return evidence_snapshots
    attachments = _latest_attachment_payload(store, case_id, case_root)
    if not attachments:
        return []
    output_dir = store.resolve_case_path(case_id, "reports/assets/evidence_snapshots")
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshots: list[dict[str, Any]] = []
    for index, item in enumerate(attachments, start=1):
        name = str(item.get("name") or f"attachment_{index}")
        source_path = Path(str(item.get("path") or ""))
        original_ref = str(item.get("original_ref") or "")
        original_path = store.resolve_case_path(case_id, original_ref) if original_ref else source_path
        suffix = source_path.suffix.lower() or Path(name).suffix.lower()
        snapshot_rel = f"reports/assets/evidence_snapshots/{index:02d}_{_safe_name(Path(name).stem)}.png"
        snapshot_abs = store.resolve_case_path(case_id, snapshot_rel)
        record = {
            "evidence_id": f"ev_{index:03d}",
            "name": name,
            "source_path": str(source_path) if str(source_path) else str(original_path),
            "original_ref": original_ref,
            "snapshot_path": snapshot_rel,
            "_absolute_snapshot_path": str(snapshot_abs),
            "status": "unsupported",
        }
        try:
            preview_paths = [str(path) for path in (item.get("preview_paths") or []) if str(path)]
            if suffix == ".pdf" and preview_paths:
                preview_rel = preview_paths[0]
                preview_abs = store.resolve_case_path(case_id, preview_rel)
                if preview_abs.exists():
                    record["snapshot_path"] = preview_rel
                    record["_absolute_snapshot_path"] = str(preview_abs)
                    record["status"] = "rendered"
                else:
                    record["status"] = "pdf_preview_missing"
            elif suffix in IMAGE_SNAPSHOT_SUFFIXES and original_path.exists():
                from PIL import Image

                with Image.open(original_path) as image:
                    image.convert("RGB").save(snapshot_abs)
                record["status"] = "rendered"
            elif suffix in TEXT_SNAPSHOT_SUFFIXES:
                content = _attachment_content(item, original_path)
                _render_text_snapshot(name, content, snapshot_abs)
                record["status"] = "rendered"
            elif suffix == ".pdf":
                record["status"] = "pdf_preview_not_available"
            else:
                record["status"] = f"unsupported_source_type:{suffix or '<none>'}"
        except Exception as exc:
            record["status"] = "snapshot_error"
            record["error"] = f"{type(exc).__name__}: {exc}"
        snapshots.append(record)
    return snapshots


def _snapshots_from_case_evidence(store: CaseStore, case_id: str, max_items: int = 12) -> list[dict[str, Any]]:
    output_dir = store.resolve_case_path(case_id, "reports/assets/evidence_snapshots")
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshots: list[dict[str, Any]] = []
    field_crop_count = 0
    state = store.load(case_id)
    state_evidence_ids = {str(item.id) for item in state.evidence_items}
    for item in list(state.evidence_items)[:max_items]:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        original_ref = str(metadata.get("original_ref") or "")
        preview_paths = [str(path) for path in (metadata.get("preview_paths") or []) if str(path)]
        if not original_ref and not preview_paths:
            continue
        if manifest_status_for_ref(store, case_id, original_ref) in EXCLUDED_STATUSES:
            continue
        if field_crop_count < 18:
            crop_records = _field_crop_snapshots(store, case_id, item, metadata, original_ref, limit=18 - field_crop_count)
            snapshots.extend(crop_records)
            field_crop_count += len(crop_records)
        source_name = Path(original_ref or preview_paths[0]).name
        snapshots.extend(
            _original_snapshot_records(
                store,
                case_id,
                evidence_id=str(item.id),
                name=source_name or item.summary or item.type,
                original_ref=original_ref,
                preview_paths=preview_paths,
                content=item.content,
            )
        )
    if state_evidence_ids and field_crop_count < 18:
        manifest_records = _manifest_field_crop_snapshots(
            store,
            case_id,
            state_evidence_ids=state_evidence_ids,
            seen_paths={str(item.get("snapshot_path") or "") for item in snapshots},
            limit=18 - field_crop_count,
        )
        snapshots.extend(manifest_records)
    return _dedupe_snapshots(snapshots)


def _manifest_original_snapshots(
    store: CaseStore,
    case_id: str,
    *,
    seen_paths: set[str],
    max_items: int = 40,
) -> list[dict[str, Any]]:
    manifest = load_attachment_manifest(store, case_id)
    snapshots: list[dict[str, Any]] = []
    for entry in (manifest.get("attachments") or [])[:max_items]:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "") in EXCLUDED_STATUSES:
            continue
        evidence_ids = [str(value) for value in entry.get("evidence_ids") or [] if str(value)]
        evidence_id = evidence_ids[0] if evidence_ids else str(entry.get("attachment_id") or "")
        records = _original_snapshot_records(
            store,
            case_id,
            evidence_id=evidence_id,
            name=str(entry.get("name") or entry.get("attachment_id") or "attachment"),
            original_ref=str(entry.get("original_ref") or ""),
            preview_paths=[str(path) for path in entry.get("preview_paths") or [] if str(path)],
            content="",
        )
        for record in records:
            snapshot_path = str(record.get("snapshot_path") or "")
            if snapshot_path and snapshot_path in seen_paths:
                continue
            if snapshot_path:
                seen_paths.add(snapshot_path)
            snapshots.append(record)
    return snapshots


def _original_snapshot_records(
    store: CaseStore,
    case_id: str,
    *,
    evidence_id: str,
    name: str,
    original_ref: str,
    preview_paths: list[str],
    content: str,
) -> list[dict[str, Any]]:
    source_name = Path(original_ref or name or "attachment").name
    suffix = Path(original_ref or source_name).suffix.lower()
    output_dir = store.resolve_case_path(case_id, "reports/assets/evidence_snapshots")
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    if suffix == ".pdf" and preview_paths:
        for page_index, preview_rel in enumerate(preview_paths, start=1):
            preview_abs = store.resolve_case_path(case_id, preview_rel)
            status = "rendered" if preview_abs.exists() else "pdf_preview_missing"
            records.append(
                {
                    "evidence_id": evidence_id,
                    "name": f"{source_name} page {page_index}",
                    "source_path": original_ref or preview_rel,
                    "original_ref": original_ref,
                    "snapshot_path": preview_rel,
                    "_absolute_snapshot_path": str(preview_abs),
                    "status": status,
                    "snapshot_kind": "original_snapshot",
                    "locator": f"page {page_index}",
                }
            )
        return records

    snapshot_rel = f"reports/assets/evidence_snapshots/{evidence_id}_{_safe_name(Path(source_name).stem)}.png"
    snapshot_abs = store.resolve_case_path(case_id, snapshot_rel)
    record = {
        "evidence_id": evidence_id,
        "name": source_name,
        "source_path": original_ref or "source_path_not_available",
        "original_ref": original_ref,
        "snapshot_path": snapshot_rel,
        "_absolute_snapshot_path": str(snapshot_abs),
        "status": "unsupported",
        "snapshot_kind": "original_snapshot",
    }
    try:
        if suffix in IMAGE_SNAPSHOT_SUFFIXES and original_ref:
            original_path = store.resolve_case_path(case_id, original_ref)
            if original_path.exists():
                from PIL import Image

                with Image.open(original_path) as image:
                    image.convert("RGB").save(snapshot_abs)
                record["status"] = "rendered"
            else:
                record["status"] = "original_missing"
        elif suffix in TEXT_SNAPSHOT_SUFFIXES and original_ref:
            original_path = store.resolve_case_path(case_id, original_ref)
            _render_text_snapshot(source_name, _attachment_content({"content": content}, original_path), snapshot_abs)
            record["status"] = "rendered"
        elif suffix == ".pdf":
            record["status"] = "pdf_preview_not_available"
        else:
            record["status"] = f"unsupported_source_type:{suffix or '<none>'}"
    except Exception as exc:
        record["status"] = "snapshot_error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    records.append(record)
    return records


def _field_crop_snapshots(
    store: CaseStore,
    case_id: str,
    evidence: Any,
    metadata: dict[str, Any],
    original_ref: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _metadata_crop_rows(metadata):
        record = _field_crop_record(
            store,
            case_id,
            evidence_id=str(getattr(evidence, "id", "") or ""),
            row=row,
            original_ref=original_ref,
        )
        if not record:
            continue
        crop_rel = str(record.get("snapshot_path") or "")
        if not crop_rel or crop_rel in seen:
            continue
        seen.add(crop_rel)
        records.append(record)
        if len(records) >= limit:
            break
    return records


def _manifest_field_crop_snapshots(
    store: CaseStore,
    case_id: str,
    *,
    state_evidence_ids: set[str],
    seen_paths: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    manifest = load_attachment_manifest(store, case_id)
    records: list[dict[str, Any]] = []
    for entry in manifest.get("attachments") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "") in EXCLUDED_STATUSES:
            continue
        evidence_ids = [str(value) for value in entry.get("evidence_ids") or [] if str(value)]
        if state_evidence_ids and evidence_ids and not any(value in state_evidence_ids for value in evidence_ids):
            continue
        if state_evidence_ids and not evidence_ids:
            continue
        evidence_id = next((value for value in evidence_ids if value in state_evidence_ids), "") or (
            evidence_ids[0] if evidence_ids else str(entry.get("attachment_id") or "")
        )
        original_ref = str(entry.get("original_ref") or "")
        for row in _metadata_crop_rows(entry):
            record = _field_crop_record(
                store,
                case_id,
                evidence_id=evidence_id,
                row=row,
                original_ref=original_ref,
            )
            if not record:
                continue
            crop_rel = str(record.get("snapshot_path") or "")
            if crop_rel in seen_paths:
                continue
            seen_paths.add(crop_rel)
            records.append(record)
            if len(records) >= limit:
                return records
    return records


def _metadata_crop_rows(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_fields: set[str] = set()

    def add(value: Any, *, field: str = "", secondary: bool = False) -> None:
        if not isinstance(value, dict):
            return
        data = dict(value)
        if field and not data.get("field"):
            data["field"] = field
        field_key = _normalize_field_key(str(data.get("field") or data.get("requirement") or ""))
        if secondary and field_key and _field_already_covered(field_key, seen_fields):
            return
        if data.get("crop_path") and _is_report_proof_crop(data):
            rows.append(data)
            if field_key:
                seen_fields.add(field_key)

    inventory = metadata.get("field_inventory")
    if isinstance(inventory, list):
        for row in inventory:
            add(row)

    extracted = metadata.get("extracted_fields")
    if isinstance(extracted, dict):
        for field, value in extracted.items():
            add(value, field=str(field))

    for key in ("evidence_chain", "claim_to_source_refs"):
        values = metadata.get(key)
        if not isinstance(values, list):
            continue
        for row in values:
            add(row, secondary=True)
    block_crops = metadata.get("block_crops")
    if isinstance(block_crops, list):
        for row in block_crops:
            bank_row = _bank_details_crop_row(row)
            if bank_row:
                add(bank_row, secondary=True)
    return rows


def _bank_details_crop_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row.get("crop_path"):
        return {}
    text = str(row.get("text") or row.get("source_quote") or "")
    lower = text.lower()
    if not any(token in lower for token in ("iban", "bic", "swift", "bank", "account")):
        return {}
    if not re.search(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{8,}|\b[A-Z0-9]{6,}\b", text, re.I):
        return {}
    result = dict(row)
    result["field"] = "bank_details"
    result["value"] = text
    result["source_quote"] = text
    result["proof_label"] = "证明银行账户/付款信息字段可见"
    result["confidence"] = "high"
    return result


def _is_report_proof_crop(row: dict[str, Any]) -> bool:
    field = str(row.get("field") or row.get("requirement") or row.get("claim") or "").strip()
    crop_id = str(row.get("crop_id") or "").strip()
    name = f"{field} {crop_id}".lower()
    if "_context" in name or name.endswith(" context"):
        return False
    if re.fullmatch(r"p\d+_b\d+", crop_id.lower()) and not field:
        return False
    if "page_number" in name or name in {"page", "页码"}:
        return False
    if "[truncated]" in name or "[截断]" in name:
        return False
    normalized = _normalize_field_key(field)
    if normalized in {
        "invoice_number",
        "supplier",
        "buyer",
        "invoice_date",
        "amount_total",
        "currency_tax",
        "currency",
        "tax_amount",
        "tax_details",
        "line_items_product_title",
        "signature_or_authorized_signatory",
        "purchase_order",
        "goods_receipt_or_service_acceptance",
        "vendor_identity",
        "duplicate_payment_screen",
        "source_traceability",
        "template_match",
        "bank_details",
    }:
        return True
    proof_text = str(row.get("proof_label") or row.get("proves") or row.get("proof") or row.get("source_quote") or "")
    if not proof_text.strip():
        return False
    generic = ("source crop", "ocr block", "text block", "context")
    return not any(token in proof_text.lower() for token in generic)


def _normalize_field_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _field_already_covered(field_key: str, seen_fields: set[str]) -> bool:
    if field_key in seen_fields:
        return True
    aliases = {
        "currency": {"currency_tax"},
        "tax_amount": {"currency_tax"},
        "tax_details": {"currency_tax"},
        "visual_signature_mark": {"signature_or_authorized_signatory"},
    }
    return bool(aliases.get(field_key, set()).intersection(seen_fields))


def _field_crop_record(
    store: CaseStore,
    case_id: str,
    *,
    evidence_id: str,
    row: dict[str, Any],
    original_ref: str,
) -> dict[str, Any] | None:
    crop_rel = str(row.get("crop_path") or "").strip()
    if not crop_rel:
        return None
    crop_abs = store.resolve_case_path(case_id, crop_rel)
    if not crop_abs.exists():
        return None
    field = str(row.get("field") or row.get("requirement") or row.get("claim") or row.get("crop_id") or "field")
    locator = str(
        row.get("locator")
        or row.get("source_locator")
        or row.get("block_or_table_or_region")
        or (f"page {row.get('page')}" if row.get("page") else "")
    )
    return {
        "evidence_id": evidence_id,
        "name": _field_label(field),
        "source_path": original_ref or str(row.get("preview_path") or row.get("preview_ref") or ""),
        "original_ref": original_ref,
        "snapshot_path": crop_rel,
        "_absolute_snapshot_path": str(crop_abs),
        "status": "rendered",
        "snapshot_kind": "field_crop",
        "field": field,
        "locator": locator,
        "limitation": str(row.get("limitation") or row.get("crop_status") or ""),
    }


def _dedupe_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in snapshots:
        key = str(item.get("snapshot_path") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(item)
    return result


def _latest_attachment_payload(store: CaseStore, case_id: str, case_root: Path, max_items: int = 12) -> list[dict[str, Any]]:
    artifact_root = case_root / "traces" / "artifacts"
    if not artifact_root.exists():
        return []
    candidates = sorted(
        artifact_root.glob("*/art_*_attachment_batch_read_attachment.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        payload = data.get("payload") if isinstance(data, dict) else {}
        attachments = payload.get("attachments") if isinstance(payload, dict) else None
        if isinstance(attachments, list) and attachments:
            for item in attachments:
                if not isinstance(item, dict) or item.get("status") == "error":
                    continue
                if not _snapshot_candidate(item):
                    continue
                original_ref = str(item.get("original_ref") or "")
                if original_ref and manifest_status_for_ref(store, case_id, original_ref) in EXCLUDED_STATUSES:
                    continue
                key = str(item.get("original_ref") or item.get("path") or item.get("name") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                collected.append(item)
    return sorted(collected, key=_snapshot_priority)[:max_items]


def _snapshot_candidate(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "").lower()
    if "irrelevant_batch" in name:
        return False
    if "prompt_injection" in name:
        return False
    source_path = Path(str(item.get("path") or name))
    suffix = source_path.suffix.lower() or Path(name).suffix.lower()
    if suffix == ".pdf" or suffix in IMAGE_SNAPSHOT_SUFFIXES:
        return True
    if suffix not in TEXT_SNAPSHOT_SUFFIXES:
        return False
    core_tokens = (
        "invoice",
        "purchase_order",
        "goods_receipt",
        "vendor_record",
        "duplicate_payment",
        "duplicate_check",
        "clear_invoice",
        "wrong_workflow",
        "policy_conflict",
    )
    return any(token in name for token in core_tokens)


def _snapshot_priority(item: dict[str, Any]) -> tuple[int, str]:
    name = str(item.get("name") or "").lower()
    source_name = Path(str(item.get("path") or item.get("source_path") or "")).name.lower()
    original_name = Path(str(item.get("original_ref") or "")).name.lower()
    file_hint = f"{name} {source_name} {original_name}"
    content_kind = str(item.get("content_kind") or "").lower()
    content = str(item.get("content") or "")[:3000].lower()
    if any(token in file_hint for token in ("clear_invoice", "clear invoice", "process_log", "process log", "bpi")):
        return (5, name)
    if any(token in file_hint for token in ("invoice", "facture", "factu")):
        return (0, name)
    if content_kind in {"image", "pdf"} and any(token in content for token in ("invoice", "facture", "factu")):
        return (0, name)
    haystack = f"{file_hint} {content}"
    if any(token in haystack for token in ("purchase_order", "purchase order", "po ", "po-")):
        return (1, name)
    if any(token in haystack for token in ("goods_receipt", "goods receipt", "grn", "receipt")):
        return (2, name)
    if any(token in haystack for token in ("vendor_record", "vendor record", "supplier", "bank")):
        return (3, name)
    if any(token in haystack for token in ("duplicate_payment", "duplicate payment", "duplicate_check", "payment history")):
        return (4, name)
    if any(token in haystack for token in ("clear_invoice", "clear invoice", "process_log", "bpi")):
        return (5, name)
    if any(token in haystack for token in ("prompt_injection", "wrong_workflow", "policy_conflict")):
        return (6, name)
    return (9, name)


def _attachment_content(item: dict[str, Any], source_path: Path) -> str:
    if source_path.exists() and source_path.is_file():
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                return source_path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
            except OSError:
                break
    return str(item.get("content") or "")


def _render_text_snapshot(name: str, content: str, target: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width = 1100
    margin = 46
    title_font = _pil_font(ImageFont, 27)
    body_font = _pil_font(ImageFont, 19)
    line_font = _pil_font(ImageFont, 17, mono=True)
    small_font = _pil_font(ImageFont, 18)
    number_width = 66
    text_x = margin + number_width + 18
    content_top = 146
    line_height = 28
    max_text_width = width - text_x - margin
    rows = _source_snapshot_rows(content, body_font, max_text_width)
    height = max(420, content_top + line_height * len(rows) + 72)
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    outer = (24, 24, width - 24, height - 28)
    draw.rectangle(outer, fill="#FFFFFF", outline="#111111", width=2)
    draw.line((24, 110, width - 24, 110), fill="#111111", width=2)
    draw.text((margin, 40), name, fill="#111111", font=title_font)
    draw.text((margin, 78), "原始附件截图", fill="#333333", font=small_font)
    draw.line((margin + number_width + 4, 110, margin + number_width + 4, height - 28), fill="#777777", width=1)
    y = content_top
    bottom_limit = height - 54
    for line_no, line in rows:
        if y > bottom_limit:
            break
        if line_no:
            draw.text((margin + 8, y), line_no.rjust(4), fill="#444444", font=line_font)
        draw.text((text_x, y), line, fill="#111111", font=body_font)
        y += line_height
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)


def _source_snapshot_rows(content: str, font: Any, max_width: int, max_rows: int = 84) -> list[tuple[str, str]]:
    text = content.rstrip("\n") or "(empty file)"
    rows: list[tuple[str, str]] = []
    truncated = False
    for line_no, raw_line in enumerate(text.splitlines() or [text], start=1):
        wrapped = _wrap_pixels(raw_line if raw_line else " ", font, max_width) or [""]
        for index, segment in enumerate(wrapped):
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append((str(line_no) if index == 0 else "", segment))
        if truncated:
            break
    if truncated:
        if rows:
            rows[-1] = ("", "[content truncated for PDF snapshot]")
        else:
            rows.append(("", "[content truncated for PDF snapshot]"))
    return rows


def _pil_font(image_font: Any, size: int, mono: bool = False) -> Any:
    mono_fonts = (
        Path(r"C:\Windows\Fonts\consola.ttf"),
        Path(r"C:\Windows\Fonts\consolab.ttf"),
    )
    cjk_fonts = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    )
    for font_path in ((*mono_fonts, *cjk_fonts) if mono else cjk_fonts):
        if not font_path.exists():
            continue
        try:
            return image_font.truetype(str(font_path), size=size)
        except Exception:
            continue
    return image_font.load_default()


def _wrap_pixels(text: str, font: Any, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for char in paragraph:
            candidate = current + char
            if _text_width(candidate, font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
    return lines


def _text_width(text: str, font: Any) -> int:
    try:
        return int(font.getlength(text))
    except Exception:
        bbox = font.getbbox(text)
        return int(bbox[2] - bbox[0])


def _append_snapshot_section(
    story: list[Any],
    snapshots: list[dict[str, Any]],
    styles: dict[str, Any],
    width: float,
    flowable_image: Any,
    paragraph: Any,
    spacer: Any,
    page_break: Any,
) -> None:
    if not snapshots:
        return
    field_crops = [item for item in snapshots if item.get("snapshot_kind") == "field_crop"]
    originals = [item for item in snapshots if item.get("snapshot_kind") != "field_crop"]
    if _needs_page_break(story):
        story.append(page_break())
    story.append(paragraph(_inline_text(RAW_MATERIAL_APPENDIX_HEADING), styles["h2"]))
    story.append(paragraph(_inline_text(RAW_MATERIAL_APPENDIX_DISCLAIMER), styles["body"]))
    if field_crops:
        story.append(paragraph(_inline_text("字段截图"), styles["h3"]))
        for index, item in enumerate(field_crops, start=1):
            item["_display_index"] = index
            _append_snapshot_item(story, item, styles, width, flowable_image, paragraph, spacer)
    if originals:
        if field_crops and _needs_page_break(story):
            story.append(page_break())
        story.append(paragraph(_inline_text("原始附件预览"), styles["h3"]))
        for index, item in enumerate(originals, start=1):
            item["_display_index"] = index
            _append_snapshot_item(story, item, styles, width, flowable_image, paragraph, spacer)


def _append_snapshot_item(
    story: list[Any],
    item: dict[str, Any],
    styles: dict[str, Any],
    width: float,
    flowable_image: Any,
    paragraph: Any,
    spacer: Any,
) -> None:
    prefix = "字段截图" if item.get("snapshot_kind") == "field_crop" else "原始附件"
    label = f"{prefix} {item.get('_display_index')}. {item.get('name')}（{item.get('evidence_id')}）"
    story.append(paragraph(_inline_text(label), styles["h4"]))
    if item.get("evidence_id"):
        story.append(paragraph(_inline_text(f"来源证据编号：{item.get('evidence_id')}"), styles["small"]))
    if item.get("locator"):
        story.append(paragraph(_inline_text(f"定位：{item.get('locator')}"), styles["small"]))
    if item.get("limitation") and item.get("limitation") != "cropped":
        story.append(paragraph(_inline_text(f"限制：{item.get('limitation')}"), styles["small"]))
    if item.get("status") == "rendered" and item.get("snapshot_path"):
        abs_path = Path(str(item.get("_absolute_snapshot_path") or ""))
        if abs_path.exists():
            img = flowable_image(str(abs_path))
            max_height = 160 if item.get("snapshot_kind") == "field_crop" else 500
            img.drawWidth, img.drawHeight = _scaled_image_size(str(abs_path), width, max_height)
            story.append(img)
        else:
            story.append(paragraph("截图文件未找到；请查看 source path。", styles["body"]))
    else:
        story.append(paragraph(_inline_text(f"未生成截图：{item.get('status')}"), styles["body"]))
    story.append(spacer(1, 10))


def _scaled_image_size(path: str, max_width: float, max_height: float) -> tuple[float, float]:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    ratio = min(max_width / max(width, 1), max_height / max(height, 1), 1.0)
    return width * ratio, height * ratio


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return clean[:80] or "attachment"


def _field_label(field: str) -> str:
    if field == "bank_details":
        return "银行账户/付款信息"
    labels = {
        "invoice_number": "发票编号",
        "supplier": "供应商",
        "buyer": "购买方",
        "invoice_date": "发票日期",
        "amount_total": "总金额",
        "currency_tax": "币种/税额",
        "currency": "币种",
        "tax_amount": "税额",
        "tax_details": "税额明细",
        "line_items_product_title": "商品/服务行项目",
        "signature_or_authorized_signatory": "签名/授权签章",
        "visual_signature_mark": "签名/授权签章",
        "source_traceability": "来源可追溯性",
        "template_match": "模板匹配",
    }
    return labels.get(field, field)
