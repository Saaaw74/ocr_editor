# -*- coding: utf-8 -*-


import json
import logging
import os
import tempfile
import time
from typing import Optional

import fitz  # PyMuPDF

from ocr_paddle_layer import run_paddle_detection, build_paddle_searchable_pdf
from adapters import parse_paddle_to_udm
from ocr_typography import build_page_typography
from table_column_snap import apply_table_column_snap, ENABLE_TABLE_X_SNAP

logger = logging.getLogger("ocr_pipeline")
logging.basicConfig(level=logging.INFO, format="%(message)s")


RENDER_DPI = 150
OCR_RENDER_DPI = 300

DEBUG_ROOT = os.path.join(os.getcwd(), "debug")
DEBUG_RAW_DIR = os.path.join(DEBUG_ROOT, "raw_paddle")
DEBUG_SEARCHABLE_DIR = os.path.join(DEBUG_ROOT, "searchable_pdf")

# Создаем только две папки, rendered и normalized не создаются
for _d in (DEBUG_RAW_DIR, DEBUG_SEARCHABLE_DIR):
    os.makedirs(_d, exist_ok=True)


def _rotate_folder_max_items(folder_path: str, max_items: int = 2) -> None:
    """Оставляет в папке не более max_items файлов, удаляя самые старые."""
    try:
        entries = [
            os.path.join(folder_path, f)
            for f in os.listdir(folder_path)
            if os.path.isfile(os.path.join(folder_path, f))
        ]
        if len(entries) > max_items:
            entries.sort(key=os.path.getmtime)
            while len(entries) > max_items:
                oldest_file = entries.pop(0)
                try:
                    os.remove(oldest_file)
                except OSError:
                    pass
    except Exception:
        logger.warning(f"[ROTATE] Ошибка ротации папки {folder_path}", exc_info=True)


class OcrPipelineError(Exception):
    pass


# ==================== РЕНДЕР СТРАНИЦЫ ====================

def render_page_for_ocr(pdf_source, page_number: int, out_png_path: Optional[str] = None, dpi: int = RENDER_DPI) -> dict:
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    else:
        if not os.path.exists(pdf_source):
            raise OcrPipelineError(f"Файл не найден: {pdf_source}")
        doc = fitz.open(pdf_source)

    try:
        if page_number < 1 or page_number > len(doc):
            raise OcrPipelineError(f"Страница {page_number} вне диапазона (1..{len(doc)})")

        page = doc[page_number - 1]
        page_w_pt = float(page.rect.width)
        page_h_pt = float(page.rect.height)

        pix = page.get_pixmap(dpi=dpi, alpha=False)
        if out_png_path:
            pix.save(out_png_path)

        return {
            "page_w_pt": page_w_pt,
            "page_h_pt": page_h_pt,
            "img_w_px": float(pix.width),
            "img_h_px": float(pix.height),
        }
    finally:
        doc.close()


# ==================== ОРКЕСТРАЦИЯ СТРАНИЦЫ ====================

def process_single_page(pdf_source, page_number: int, job_id: str, on_progress=None) -> dict:
    t_start = time.time()

    if on_progress:
        on_progress(0, "Рендеринг страницы...")

    # Временный PNG исключительно на время распознавания (не засоряет диск)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
        tmp_rendered_path = tmp_file.name

    try:
        dims = render_page_for_ocr(pdf_source, page_number, tmp_rendered_path, dpi=OCR_RENDER_DPI)

        page_w_pt = dims["page_w_pt"]
        page_h_pt = dims["page_h_pt"]
        img_w_px = dims["img_w_px"]
        img_h_px = dims["img_h_px"]

        if on_progress:
            on_progress(25, "Детекция и распознавание текста (PaddleOCR)...")

        t_ocr_start = time.time()
        paddle_data = run_paddle_detection(tmp_rendered_path)
        ocr_time = round(time.time() - t_ocr_start, 3)

        if on_progress:
            on_progress(40, "Подбор шрифта и размера (Typography Engine)...")

        t_typo_start = time.time()
        typography = build_page_typography(tmp_rendered_path, paddle_data)
        typography_time = round(time.time() - t_typo_start, 3)

        typo_debug = {
            "page_profile": typography.page_profile.to_dict(),
            "line_styles": {
                str(k): v.to_dict() for k, v in typography.line_styles.items()
            },
            "time_sec": typography_time,
        }

        # Сохраняем raw_paddle и держим максимум 2 файла
        raw_payload = {"page": page_number, "items": paddle_data}
        raw_out_path = os.path.join(DEBUG_RAW_DIR, f"{job_id}_p{page_number}.json")
        with open(raw_out_path, "w", encoding="utf-8") as f:
            json.dump(raw_payload, f, ensure_ascii=False, indent=2)
        _rotate_folder_max_items(DEBUG_RAW_DIR, max_items=2)

        udm_page = parse_paddle_to_udm(
            paddle_data=paddle_data,
            page_w_pt=page_w_pt,
            page_h_pt=page_h_pt,
            img_w_px=img_w_px,
            img_h_px=img_h_px,
            page_num=page_number,
            page_style=typography.page_profile,
            line_styles=typography.line_styles,
        )

        table_snap_debug = None
        if ENABLE_TABLE_X_SNAP:
            if on_progress:
                on_progress(65, "Выравнивание колонок таблиц...")
            try:
                table_snap_debug = apply_table_column_snap(udm_page)
            except Exception:
                logger.warning("[OCR] table_column_snap failed, skipping", exc_info=True)
                table_snap_debug = None

        if on_progress:
            on_progress(70, "Генерация Searchable PDF...")

        searchable_items = []
        for blk in (udm_page.blocks or []):
            if getattr(blk, "type", None) != "text":
                continue
            blk_lines = getattr(blk, "lines", [])
            blk_font_size = getattr(getattr(blk, "font", None), "size_pt", None)

            if blk_lines:
                for ln in blk_lines:
                    b = ln.bbox
                    searchable_items.append({
                        "text": ln.text,
                        "bbox_pt": [b.x1, b.y1, b.x2, b.y2],
                        "font_size_pt": blk_font_size,
                    })
            else:
                b = blk.bbox
                searchable_items.append({
                    "text": blk.raw_text,
                    "bbox_pt": [b.x1, b.y1, b.x2, b.y2],
                    "font_size_pt": blk_font_size,
                })

        searchable_pdf_filename = f"{job_id}_p{page_number}_searchable.pdf"
        searchable_pdf_path = os.path.join(DEBUG_SEARCHABLE_DIR, searchable_pdf_filename)

        build_paddle_searchable_pdf(
            rendered_img_path=tmp_rendered_path,
            text_items=searchable_items,
            out_pdf_path=searchable_pdf_path,
            page_w_pt=page_w_pt,
            page_h_pt=page_h_pt,
        )
        # Держим максимум 2 файла в searchable_pdf
        _rotate_folder_max_items(DEBUG_SEARCHABLE_DIR, max_items=2)

    finally:
        # Всегда удаляем временный PNG
        if os.path.exists(tmp_rendered_path):
            try:
                os.remove(tmp_rendered_path)
            except OSError:
                pass

    total_time = round(time.time() - t_start, 3)
    confidences = [b.confidence for b in udm_page.blocks] if udm_page.blocks else []
    avg_conf = round(sum(confidences) / len(confidences), 3) if confidences else 1.0

    if on_progress:
        on_progress(100, "Готово")

    return {
        "page": udm_page.page_num,
        "width_pt": udm_page.width_pt,
        "height_pt": udm_page.height_pt,
        "coordinate_system": udm_page.coordinate_system,
        "image_width_px": img_w_px,
        "image_height_px": img_h_px,
        "searchable_pdf_url": f"/api/document/{job_id}/searchable/{page_number}",
        "blocks": json.loads(udm_page.model_dump_json())["blocks"],
        "structured_blocks": None,
        "structured_layout": None,
        "processing_time_sec": total_time,
        "ocr_time_sec": ocr_time,
        "typography_time_sec": typography_time,
        "typography": typo_debug,
        "average_confidence": avg_conf,
        "table_snap_debug": table_snap_debug,
    }