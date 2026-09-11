# -*- coding: utf-8 -*-

import logging
import os
import time

import fitz  # PyMuPDF

import base64

from models import BoundingBox, FontMeta, TextBlock, Line, WordSpan, DocumentPage, PictureBlock
from ocr_pipeline import render_page_for_ocr, OcrPipelineError  # переиспользуем renderer
from pdf_geometry import raw_to_display_bbox, get_page_rotation_and_raw_size

logger = logging.getLogger("native_pipeline")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _span_to_wordspan(span, span_idx, line_id, rotation, raw_w, raw_h):

    (x0, y0, x1, y1), _, _ = raw_to_display_bbox(span["bbox"], rotation, raw_w, raw_h)
    flags = span.get("flags", 0)
    font_name = span.get("font", "Arial")
    
    # Флаги PyMuPDF: бит 4 (16) = bold, бит 1 (2) = italic
    is_bold = bool(flags & (1 << 4)) or ("bold" in font_name.lower()) or ("black" in font_name.lower())
    is_italic = bool(flags & (1 << 1)) or ("italic" in font_name.lower()) or ("oblique" in font_name.lower())
    color_int = span.get("color", 0)
    
    clean_fam = font_name.split("+")[-1] 
    
    return WordSpan(
        id=f"{line_id}_s{span_idx}",
        bbox=BoundingBox(x1=round(x0, 2), y1=round(y0, 2), x2=round(x1, 2), y2=round(y1, 2)),
        text=span.get("text", ""),
        font_family=clean_fam,
        size_pt=round(float(span.get("size", 11.0)), 1),
        is_bold=is_bold,
        is_italic=is_italic,
        color="#%06x" % (color_int & 0xFFFFFF)
    )


def _pymupdf_line_to_udm_line(raw_line, line_id, rotation, raw_w, raw_h):

    spans_raw = raw_line.get("spans", [])
    text_parts = [s.get("text", "") for s in spans_raw]
    line_text = "".join(text_parts)
    if not line_text.strip():
        return None

    (x0, y0, x1, y1), _, _ = raw_to_display_bbox(raw_line["bbox"], rotation, raw_w, raw_h)
    spans = [
        _span_to_wordspan(s, idx, line_id, rotation, raw_w, raw_h)
        for idx, s in enumerate(spans_raw)
        if s.get("text", "").strip()
    ]

    return Line(
        id=line_id,
        bbox=BoundingBox(x1=round(x0, 2), y1=round(y0, 2), x2=round(x1, 2), y2=round(y1, 2)),
        text=line_text,
        spans=spans,
    )


def _dominant_font(raw_block):

    best_span = None
    best_len = -1
    for line in raw_block.get("lines", []):
        for span in line.get("spans", []):
            t = span.get("text", "")
            if len(t) > best_len:
                best_len = len(t)
                best_span = span

    if not best_span:
        return FontMeta()

    flags = best_span.get("flags", 0)
    is_bold = bool(flags & 2 ** 4)
    color_int = best_span.get("color", 0)
    return FontMeta(
        family=best_span.get("font", "Arial"),
        size_pt=round(best_span.get("size", 11.0), 1),
        weight=700 if is_bold else 400,
        color="#%06x" % (color_int & 0xFFFFFF),
    )


def _extract_picture_block(page, raw_block, page_number, pic_idx, rotation, raw_w, raw_h):

    try:
        (x0, y0, x1, y1), _, _ = raw_to_display_bbox(raw_block["bbox"], rotation, raw_w, raw_h)
        if x1 <= x0 or y1 <= y0:
            return None
        rect = fitz.Rect(x0, y0, x1, y1)
        pix = page.get_pixmap(clip=rect, dpi=150)
        if pix.width == 0 or pix.height == 0:
            return None
        png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode("ascii")
        return PictureBlock(
            id=f"native_p{page_number}_pic{pic_idx}",
            bbox=BoundingBox(x1=round(x0, 2), y1=round(y0, 2), x2=round(x1, 2), y2=round(y1, 2)),
            confidence=1.0,
            source="native",
            image_base64=b64,
        )
    except Exception:  # noqa: BLE001
        return None


def extract_native_page_from_doc(doc: fitz.Document, page_number: int) -> DocumentPage:
    """Извлекает реальный текстовый слой напрямую из переданного экземпляра fitz.Document с сохранением порядка чтения."""
    if page_number < 1 or page_number > len(doc):
        raise OcrPipelineError(f"Страница {page_number} вне диапазона (1..{len(doc)})")

    page = doc[page_number - 1]

    page_w_pt = float(page.rect.width)
    page_h_pt = float(page.rect.height)
    rotation, raw_w, raw_h = get_page_rotation_and_raw_size(page)

    text_dict = page.get_text("dict")
    raw_blocks_list = text_dict.get("blocks", [])

    def _block_sort_key(b):
        bbox = b.get("bbox", [0, 0, 0, 0])
        disp_bbox, _, _ = raw_to_display_bbox(bbox, rotation, raw_w, raw_h)
        return (round(disp_bbox[1], 1), round(disp_bbox[0], 1))

    raw_blocks_list.sort(key=_block_sort_key)

    blocks = []
    b_idx = 0

    pic_idx = 0
    for raw_block in text_dict.get("blocks", []):
        if raw_block.get("type") != 0:  # 0 = текстовый блок, 1 = картинка
            if raw_block.get("type") == 1:
                pic_idx += 1
                pic_block = _extract_picture_block(page, raw_block, page_number, pic_idx, rotation, raw_w, raw_h)
                if pic_block is not None:
                    blocks.append(pic_block)
            continue

        lines = []
        for l_idx, raw_line in enumerate(raw_block.get("lines", [])):
            tmp_line_id = f"native_p{page_number}_tmp_l{l_idx}"
            udm_line = _pymupdf_line_to_udm_line(raw_line, tmp_line_id, rotation, raw_w, raw_h)
            if udm_line is not None:
                lines.append(udm_line)

        if not lines:
            continue

        b_idx += 1
        block_id = f"native_p{page_number}_b{b_idx}"
        for li, ln in enumerate(lines):
            ln.id = f"{block_id}_l{li}"
            for si, sp in enumerate(ln.spans):
                sp.id = f"{ln.id}_s{si}"

        (x0, y0, x1, y1), _, _ = raw_to_display_bbox(raw_block["bbox"], rotation, raw_w, raw_h)
        block_text = "\n".join(ln.text for ln in lines)
        font = _dominant_font(raw_block)

        blocks.append(TextBlock(
            id=block_id,
            bbox=BoundingBox(x1=round(x0, 2), y1=round(y0, 2), x2=round(x1, 2), y2=round(y1, 2)),
            confidence=1.0,
            source="native",
            html_content=f"<p>{block_text}</p>",
            raw_text=block_text,
            font=font,
            lines=lines,
        ))

    return DocumentPage(
        page_num=page_number,
        width_pt=page_w_pt,
        height_pt=page_h_pt,
        source_type="native",
        blocks=blocks,
    )


def extract_native_page_to_udm(pdf_source, page_number: int) -> DocumentPage:
    """Извлекает реальный текстовый слой одной страницы PDF из файла или байтов памяти."""
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    else:
        if not os.path.exists(pdf_source):
            raise OcrPipelineError(f"Файл не найден: {pdf_source}")
        doc = fitz.open(pdf_source)

    try:
        return extract_native_page_from_doc(doc, page_number)
    finally:
        doc.close()


def process_native_page(pdf_source, page_number: int, job_id: str, on_progress=None) -> dict:
    t_start = time.time()
    logger.info(f"[PDF] page={page_number}")
    logger.info(f"[PDF] mode=native")

    if on_progress:
        on_progress(0, "Загрузка PDF...")

    # Рендерим размеры без сохранения на диск (out_png_path=None)
    dims = render_page_for_ocr(pdf_source, page_number, out_png_path=None)

    if on_progress:
        on_progress(30, "Извлечение native text (block/line/span)...")

    t_extract_start = time.time()
    udm_page = extract_native_page_to_udm(pdf_source, page_number)
    extract_time = round(time.time() - t_extract_start, 3)

    total_lines = sum(len(b.lines) for b in udm_page.blocks if isinstance(b, TextBlock))
    logger.info(f"[PDF] native_blocks={len(udm_page.blocks)}")
    logger.info(f"[PDF] native_lines={total_lines}")
    logger.info(f"[PDF] OCR skipped")

    if on_progress:
        on_progress(80, "Создание UDM...")

    total_time = round(time.time() - t_start, 3)

    if on_progress:
        on_progress(100, "Готово")

    import json
    return {
        "page": udm_page.page_num,
        "width_pt": udm_page.width_pt,
        "height_pt": udm_page.height_pt,
        "coordinate_system": udm_page.coordinate_system,
        "source_type": udm_page.source_type,
        "image_width_px": dims["img_w_px"],
        "image_height_px": dims["img_h_px"],
        "blocks": json.loads(udm_page.model_dump_json())["blocks"],
        "extraction_time_sec": extract_time,
        "processing_time_sec": total_time,
        "average_confidence": 1.0,
        "native_blocks": len(udm_page.blocks),
        "native_lines": total_lines,
    }