# -*- coding: utf-8 -*-


import logging
import os
import threading

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

import fitz  # PyMuPDF

logger = logging.getLogger("ocr_paddle_layer")
logging.basicConfig(level=logging.INFO, format="%(message)s")

PADDLE_MIN_CONF = 0.50


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


OCR_MAX_WORKERS = max(1, _env_int("OCR_MAX_WORKERS", 1))

# Синглтон-движок (дорогая модель; создаём один раз).
_ocr_engine = None
_ocr_load_attempted = False

# Защита инициализации движка от гонки (несколько потоков одновременно).
_ENGINE_INIT_LOCK = threading.Lock()


def get_ocr_engine():
    global _ocr_engine, _ocr_load_attempted
    if _ocr_load_attempted:
        return _ocr_engine
    with _ENGINE_INIT_LOCK:
        if _ocr_load_attempted:
            return _ocr_engine
        _ocr_load_attempted = True
        try:
            from paddleocr import PaddleOCR
            # enable_mkldnn=False: на этом билде paddle/oneDNN даёт
            # NotImplementedError (см. модульный docstring).
            _ocr_engine = PaddleOCR(lang="ru", enable_mkldnn=False)
            logger.info("[PADDLE] PaddleOCR(lang='ru') инициализирован")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PADDLE] не удалось инициализировать PaddleOCR: {exc}")
            _ocr_engine = None
    return _ocr_engine


# ============================ A. Детекция ============================

def run_paddle_detection(img_path: str, min_confidence: float = PADDLE_MIN_CONF) -> list:

    engine = get_ocr_engine()
    if engine is None:
        logger.warning("[PADDLE] движок отсутствует - возвращаю пустой список")
        return []


    with threading.Semaphore(OCR_MAX_WORKERS):
        # Читаем изображение в память (numpy array), чтобы PaddleOCR физически
        # не имел доступа к пути файла и не мог пересохранить туда отладочные красные рамки!
        from PIL import Image as _PILImg
        with _PILImg.open(img_path) as _img:
            img_arr = np.array(_img.convert("RGB"))

        res = engine.predict(
            img_arr,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_det_limit_side_len=1440,
            text_det_limit_type="max",
        )
        # res - генератор; первый результат - распознанная страница
        try:
            page_result = next(iter(res))
        except StopIteration:
            return []

    rec_texts = page_result.get("rec_texts") or []
    rec_scores = page_result.get("rec_scores") or []
    rec_polys = page_result.get("rec_polys") or []

    n = len(rec_texts)
    elements = []
    for i in range(n):
        text = rec_texts[i] or ""
        score = float(rec_scores[i]) if i < len(rec_scores) else 0.0
        if score < min_confidence:
            continue
        if not text.strip():
            continue

        poly = None
        if i < len(rec_polys) and rec_polys[i] is not None:
            arr = np.asarray(rec_polys[i]) if np is not None else None
            if arr is not None and arr.ndim == 2 and arr.shape[0] >= 4:
                poly = [[float(float(x)), float(float(y))] for x, y in arr[:4]]

        if poly is None:
            # fallback: dt_polys[i]
            dt = page_result.get("dt_polys") or []
            if i < len(dt) and dt[i] is not None:
                arr = np.asarray(dt[i])
                if arr.ndim == 2 and arr.shape[0] >= 4:
                    poly = [[float(x), float(y)] for x, y in arr[:4]]

        if poly is None:
            continue

        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        bbox = [min(xs), min(ys), max(xs), max(ys)]

        elements.append({
            "text": text,
            "confidence": round(score, 4),
            "polygon_px": poly,
            "bbox_px": bbox,
        })

    # Порядок чтения: сверху вниз, слева направо (со стабильной привязкой
    # по y с небольшой толерантностью, чтобы колонки на одной строке не
    # перетыкались в произвольном порядке).
    _sort_reading_order(elements)
    return elements


def _sort_reading_order(elements: list) -> None:
    """Стабильная сортировка в порядке чтения: группируем по "полосе" по y
    (округление к 6 пикселей), внутри полосы по x слева-направо."""
    def key_fn(el):
        bbox = el["bbox_px"]
        band = round(bbox[1] / 6.0)
        return (band, bbox[0], bbox[1])
    elements.sort(key=key_fn)


# ============================ Б. Сборка Searchable PDF ============================


def build_paddle_searchable_pdf(
    rendered_img_path: str,
    text_items: list,
    out_pdf_path: str,
    page_w_pt: float = None,
    page_h_pt: float = None,
) -> dict:

    pix = fitz.Pixmap(rendered_img_path)
    img_w_px = float(pix.width)
    img_h_px = float(pix.height)

    if page_w_pt and page_h_pt:
        page_w_pt = float(page_w_pt)
        page_h_pt = float(page_h_pt)
    else:
        is_landscape = img_w_px > img_h_px
        page_w_pt = 841.92 if is_landscape else 595.32
        page_h_pt = 595.32 if is_landscape else 841.92

    doc = fitz.open()
    page = doc.new_page(width=page_w_pt, height=page_h_pt)

    # Фоновый растр на всю страницу
    page.insert_image(page.rect, pixmap=pix)

    tw = fitz.TextWriter(page.rect)
    font = fitz.Font("helv")

    stats = {"lines": 0, "skipped": 0}

    for item in text_items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        bbox_pt = item.get("bbox_pt")
        if not bbox_pt or len(bbox_pt) != 4:
            stats["skipped"] += 1
            continue

        x0, y0, x1, y1 = [float(v) for v in bbox_pt]
        box_h = max(1.0, y1 - y0)
        box_w = max(1.0, x1 - x0)

        # Если кегль уже был рассчитан в Typography Engine / UDM, используем его
        custom_fs = item.get("font_size_pt")
        if custom_fs and float(custom_fs) > 0:
            fontsize = max(4.0, min(24.0, float(custom_fs)))
        else:
            fontsize = max(4.0, min(24.0, box_h * 0.82))

        baseline_y = y0 + (fontsize * 0.85)

        try:
            text_len = font.text_length(text, fontsize=fontsize)
        except Exception:  # noqa: BLE001
            text_len = 0.0

        if text_len > box_w and text_len > 0:
            fontsize = max(4.0, fontsize * (box_w / text_len) * 0.98)
            baseline_y = y0 + (fontsize * 0.85)

        tw.append(fitz.Point(x0, baseline_y), text, font=font, fontsize=fontsize)
        stats["lines"] += 1

    # Невидимый слой текста (render_mode=3) для выделения и поиска
    tw.write_text(page, render_mode=3)
    doc.save(out_pdf_path, deflate=True)
    doc.close()
    return stats