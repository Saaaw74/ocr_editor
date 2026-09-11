# -*- coding: utf-8 -*-


from __future__ import annotations

import logging
import os
import platform
import re
import threading
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageFilter, ImageFont, ImageOps

logger = logging.getLogger("ocr_typography")

# ============================ ОС определяется один раз при старте ============================
# Линукс/Docker никогда не должен трогать C:/Windows/... - лишние os.path.exists()
# на несуществующий диск на каждую строку скана заметно тормозят подбор шрифта.
_IS_WINDOWS = platform.system() == "Windows"

# ============================ Константы ============================

# Порог уверенности: ниже него не делаем агрессивных выводов -> fallback.
CONFIDENCE_THRESHOLD = 0.70

# Диапазон размера шрифта (pt).
MIN_FONT_PT = 4.0
MAX_FONT_PT = 24.0

# Начальная геометрическая оценка размера — высота * 0.82 (см. ТЗ §10).
# Это ТОЛЬКО стартовая точка; уточняется пер-фоновым сопоставлением ink-высоты.
SIZE_HEURISTIC = 0.82

# Шаг поиска размера (pt): initial-0.75, initial, initial+0.75.
SIZE_STEP_PT = 0.75

# PPI изображения, на котором работает PaddleOCR (см. ocr_pipeline.OCR_RENDER_DPI).
# Перевод "px шрифта" <-> "pt": pt = px * 72 / PPI.
PIXELS_PER_INCH = 300.0

# Веса скоринга (легко менять).
W_SHAPE = 0.40
W_WIDTH = 0.30
W_HEIGHT = 0.15
W_STROKE = 0.15

# Диапазон scale_x — ТОЛЬКО тонкая коррекция (см. ТЗ §11/§14).
# Если требуется < SCALE_X_MIN или > SCALE_X_MAX — сначала пересчитываем font_size.
SCALE_X_MIN = 0.95
SCALE_X_MAX = 1.05

# Ширина "гонкой" коррекции при пересчёте кегля: если текст не влезает по ширине
# и needed выходит за SCALE_X_MIN..MAX, корректируем font_size на этот множитель.
SCALE_X_RECOMPUTE_THRESHOLD = 0.10  # |needed - 1.0| выше этой границы → пересчёт размера

# Допуск группы близких высот (pt) для Page Style Profile (см. §7 "группы высот").
HEIGHT_GROUP_TOL_PT = 1.5

# Защита от аномально высокого bbox (§6): строка выше median * ABNORMAL_HEIGHT_FACTOR
# не выбирается как representative и получает размер своей группы, а не растягивается.
ABNORMAL_HEIGHT_FACTOR = 1.8

# Bold выбирается только при явном преимуществе над Regular (иначе шумный жирный).
WEIGHT_BOLD_MARGIN = 0.03

# Кэш: (font_path, size_px, weight, italic, text) -> binarized mask (0..255 grayscale)
_MASK_CACHE: Dict[tuple, "object"] = {}
# Кэш найденного стиля страницы: key = hash пути к изображению страницы.
_PAGE_STYLE_CACHE: Dict[tuple, "TypographyStyle"] = {}

# ============================ Потокобезопасный кэш FreeType-шрифтов ============================
# ImageFont.truetype() дергает libfreetype напрямую; параллельная загрузка/использование
# одного и того же файла шрифта из нескольких потоков (несколько страниц обрабатываются
# одновременно) может уронить процесс или потечь по памяти на Linux. Поэтому: (1) любое
# открытие и любое использование FT_Face идёт под одним локом, (2) уже открытые шрифты
# кэшируются по (путь, размер_px), чтобы не открывать файл повторно на каждый вызов.
_FONT_CACHE_LOCK = threading.RLock()
_FONT_OBJ_CACHE: Dict[Tuple[str, int], "ImageFont.FreeTypeFont"] = {}


def _get_cached_font(font_path: str, size_px: int) -> "ImageFont.FreeTypeFont":
    """Возвращает уже открытый объект шрифта из кэша либо открывает и кэширует новый.
    Вызывать ТОЛЬКО под _FONT_CACHE_LOCK (см. использование ниже) - сам открытый
    объект FT_Face не потокобезопасен при параллельном рендере глифов."""
    key = (font_path, int(size_px))
    font = _FONT_OBJ_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(font_path, size=int(size_px))
        _FONT_OBJ_CACHE[key] = font
    return font


# ============================ Данные ============================

@dataclass
class TypographyStyle:
    """Визуальный стиль OCR-текста. source: auto | inherited | user | fallback."""
    family: str = "Times New Roman"
    font_size: float = 11.0          # pt
    weight: int = 400                # 400 = Regular, 700 = Bold
    italic: bool = False
    scale_x: float = 1.0
    baseline_offset: float = 0.0     # pt, небольшая вертикальная поправка
    confidence: float = 0.0          # 0.0..1.0
    source: str = "auto"
    # Debug (не обязательная часть контракта): топ-3 кандидата.
    candidates: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    family: str
    font_path: str
    size_px: float
    weight: int
    italic: bool
    score: float
    scale_x: float
    conf: float
    detail: Dict = field(default_factory=dict)


# ============================ Font Registry ============================

# ОГРАНИЧЕННЫЙ пул MVP: основной + fallback'и в каждой категории.
FONT_POOL = [
    # (семейство, категория)
    ("Times New Roman", "serif"),
    ("Liberation Serif", "serif"),
    ("DejaVu Serif", "serif"),
    ("Arial", "sans"),
    ("Liberation Sans", "sans"),
    ("DejaVu Sans", "sans"),
    ("Calibri", "sans"),
    ("Courier New", "mono"),
    ("Liberation Mono", "mono"),
    ("Consolas", "mono"),
]

# Fallback по категории на случай низкого confidence / неизвестной категории.
CATEGORY_FALLBACK = {
    "serif": "Times New Roman",
    "sans": "Arial",
    "mono": "Courier New",
}


def resolve_system_font(family_name: str, is_bold: bool = False, is_italic: bool = False) -> Optional[str]:
    """Возвращает реальный путь к .ttf для семейства, либо None.

    Поддерживает ограниченный пул MVP. Не сканирует все системные шрифты.
    Сигнатура совместима со старой resolve_system_font() из document_routes.py.
    """
    fam = (family_name or "").lower().replace(" ", "").replace("-", "")
    cands: List[str] = []

    if "times" in fam or "serif" in fam:
        if is_bold and is_italic:
            cands = ["C:/Windows/Fonts/timesbi.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"]
        elif is_bold:
            cands = ["C:/Windows/Fonts/timesbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"]
        elif is_italic:
            cands = ["C:/Windows/Fonts/timesi.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"]
        else:
            cands = ["C:/Windows/Fonts/times.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
    elif "courier" in fam or "mono" in fam:
        if is_bold:
            cands = ["C:/Windows/Fonts/courbd.ttf", "C:/Windows/Fonts/consolab.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"]
        elif is_italic:
            cands = ["C:/Windows/Fonts/couri.ttf", "C:/Windows/Fonts/consolai.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationMono-Italic.ttf"]
        else:
            cands = ["C:/Windows/Fonts/cour.ttf", "C:/Windows/Fonts/consola.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"]
    elif "calibri" in fam:
        if is_bold and is_italic:
            cands = ["C:/Windows/Fonts/calibrii.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"]
        elif is_bold:
            cands = ["C:/Windows/Fonts/calibrib.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
        elif is_italic:
            cands = ["C:/Windows/Fonts/calibrii.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"]
        else:
            cands = ["C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/arial.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    elif "liberation" in fam:
        if "serif" in fam:
            cands = ["/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"]
        elif "mono" in fam:
            cands = ["/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"]
        else:
            cands = ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
    elif "dejavu" in fam:
        if "serif" in fam:
            cands = ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
        elif "mono" in fam:
            cands = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
        else:
            cands = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    elif "arial" in fam:
        if is_bold and is_italic:
            cands = ["C:/Windows/Fonts/arialbi.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        elif is_bold:
            cands = ["C:/Windows/Fonts/arialbd.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        elif is_italic:
            cands = ["C:/Windows/Fonts/ariali.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"]
        else:
            cands = ["C:/Windows/Fonts/arial.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    else:
        # Sans-serif по умолчанию (старое поведение: default -> Arial).
        if is_bold and is_italic:
            cands = ["C:/Windows/Fonts/arialbi.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"]
        elif is_bold:
            cands = ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
        elif is_italic:
            cands = ["C:/Windows/Fonts/ariali.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"]
        else:
            cands = ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]

    if not _IS_WINDOWS:
        # Linux/Docker: путь "C:/Windows/..." никогда не существует - не тратим
        # на него os.path.exists() (это то самое обращение к несуществующему
        # диску C:, которое тормозило подбор шрифта на каждой строке скана).
        cands = [p for p in cands if not p.startswith("C:")]

    for p in cands:
        if os.path.exists(p):
            return p
    return None


def available_pool_fonts() -> List[dict]:
    """Доступные (реально существующие) шрифты пула с путями для Regular."""
    out = []
    for family, category in FONT_POOL:
        path = resolve_system_font(family)
        if path:
            out.append({"family": family, "category": category, "path": path})
    return out


def _candidate_fonts(category_hint: Optional[str]) -> List[dict]:
    pool = available_pool_fonts()
    if not pool:
        return []

    if category_hint in ("serif", "sans", "mono"):
        # Берем всех кандидатов этой категории
        primary = [f for f in pool if f["category"] == category_hint]
        if primary:
            return primary

    # Если категория не ясна — берем главных представителей всех категорий
    return _category_mains(pool)


def _category_mains(pool: List[dict]) -> List[dict]:
    """Главный шрифт каждой категории (первый доступный)."""
    mains = {}
    for f in pool:
        if f["category"] not in mains:
            mains[f["category"]] = f
    return list(mains.values())


# ============================ Preprocessing ============================

def _preprocess_crop(crop_rgb):
    """crop RGB -> grayscale -> контраст -> бинаризация -> mask.

    Возвращает: mask (PIL grayscale, text pixels ~ 255), stats.
    Порог бинаризации фиксирован после контрастного растяжения и СОГЛАСОВАН
    с рендером кандидатов (тот же _binarize)."""
    gray = ImageOps.grayscale(crop_rgb)
    # Лёгкое удаление шума: median 3x3 (небольшое, не размывает глифы).
    gray = gray.filter(ImageFilter.MedianFilter(3))
    arr_stretched = _contrast_stretch(gray)
    mask = _binarize(arr_stretched)
    return mask


def _contrast_stretch(gray_img):
    """Нормализация контраста: p5..p95 перцентили -> 0..255."""
    import numpy as np
    arr = np.asarray(gray_img, dtype=np.uint8).astype(np.float32)
    if arr.size == 0:
        return arr
    lo = np.percentile(arr, 5)
    hi = np.percentile(arr, 95)
    if hi - lo < 1.0:
        return arr
    stretched = (arr - lo) * (255.0 / (hi - lo))
    return np.clip(stretched, 0, 255)


def _binarize(arr_float):
    """Бинаризация по фиксированному порогу 128 после контрастного растяжения.
    Возвращает np.uint8 массив: ТЕКСТ (тёмные пиксели) = 255, фон = 0."""
    import numpy as np
    return (arr_float < 128).astype(np.uint8) * 255


def _ink_mask_stats(mask):
    """Статистика ink-области маски (текст=255): bbox, площадь, плотность."""
    import numpy as np
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return {"x0": 0, "y0": 0, "x1": 0, "y1": 0, "width": 0, "height": 0,
                "area": 0, "density": 0.0}
    h, w = mask.shape[0], mask.shape[1]
    return {
        "x0": int(xs.min()), "y0": int(ys.min()),
        "x1": int(xs.max()), "y1": int(ys.max()),
        "width": int(xs.max() - xs.min() + 1),
        "height": int(ys.max() - ys.min() + 1),
        "area": int(ys.size),
        "density": float(ys.size) / float(h * w),
    }


# ============================ Candidate Rendering (Pillow only) ============================

def render_text_mask(font_path: str, text: str, size_px: float,
                     weight: int = 400, italic: bool = False):
    """Рендер строки белым фоном + чёрным текстом, grayscale, бинаризация.

    Возвращает mask (текст=255), ink_stats и ширину строки в px."""
    key = (font_path, round(size_px, 2), weight, italic, text)
    cached = _MASK_CACHE.get(key)
    if cached is not None:
        return cached

    from PIL import ImageDraw
    size_px_int = int(round(size_px))

    # Вся работа с FT_Face (получение из кэша/открытие + измерение + рисование)
    # выполняется под одним локом - это защищает FreeType от параллельного
    # доступа из нескольких потоков-обработчиков страниц.
    with _FONT_CACHE_LOCK:
        font = _get_cached_font(font_path, size_px_int)
        # Оценка размера холста через textlength/getbbox.
        try:
            w = int(font.getlength(text)) + 4
        except Exception:  # noqa: BLE001
            w = int(size_px * len(text) * 0.6) + 4
        h = int(size_px * 1.6) + 4
        img = Image.new("L", (max(2, w), max(2, h)), 255)
        draw = ImageDraw.Draw(img)
        draw.text((2, 2), text, font=font, fill=0)
    mask = _binarize(np_float(img))
    stats = _ink_mask_stats(mask)
    _MASK_CACHE[key] = (mask, stats, w)
    return _MASK_CACHE[key]


def np_float(img):
    import numpy as np
    arr = np.asarray(img, dtype=np.float32)
    if arr.size == 0:
        return arr
    return arr


# ============================ Scoring ============================

def _sim_ratio(measured: float, target: float) -> float:
    """Сходство размеров: 1.0 = совпадение, падает линейно от расхождения."""
    if target <= 0 or measured <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(measured - target) / target)


def _norm_to_height(mask):
    
    import numpy as np
    from PIL import Image
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return mask > 0
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    trimmed = mask[y0:y1, x0:x1]
    h, w = trimmed.shape[:2]
    if h <= 0 or w <= 0:
        return mask > 0
    target_h = 64
    tw = max(1, int(round(w * target_h / h)))
    img = Image.fromarray(trimmed, "L").resize((tw, target_h), Image.BILINEAR)
    return (np.asarray(img) > 128)


def _mask_iou_aligned(mask_a, mask_b, max_shift: int = 3):
    
    import numpy as np
    a = _norm_to_height(mask_a)
    b = _norm_to_height(mask_b)
    if a.shape[0] != b.shape[0]:
        return 0.0
    area_a = int(a.sum())
    area_b = int(b.sum())
    if area_a == 0 or area_b == 0:
        return 0.0
    W = a.shape[1] + b.shape[1] + 2 * max_shift
    H = a.shape[0]
    # Размещаем A зафиксированно, B — по левому краю с перебором сдвигов.
    canvas_a = np.zeros((H, W), dtype=bool)
    ax0 = max_shift
    canvas_a[:, ax0:ax0 + a.shape[1]] = a
    best = 0.0
    for shift in range(-max_shift, max_shift + 1):
        bx0 = ax0 + shift
        bx0 = max(0, min(W - b.shape[1], bx0))
        combined = canvas_a.copy()
        combined[:, bx0:bx0 + b.shape[1]] |= b
        union = int((combined).sum())
        if union == 0:
            continue
        inter = area_a + area_b - union
        iou = inter / union
        if iou > best:
            best = iou
    return best


def score_candidate(src_mask, src_stats, text, font_path, size_px, weight, italic):
    """Итоговый score (0..1):
        shape*W_SHAPE + width*W_WIDTH + height*W_HEIGHT + stroke*W_STROKE."""
    cand_mask, cand_stats, _ = render_text_mask(font_path, text, size_px, weight, italic)

    # height: сравниваем ink-высоты.
    h_sim = _sim_ratio(cand_stats["height"], src_stats["height"])

    # width: средне-символьная ширина (1/число символов) — различает mono/sans/serif.
    n = max(1, len(text))
    src_wch = src_stats["width"] / n
    cand_wch = cand_stats["width"] / n
    w_sim = _sim_ratio(cand_wch, src_wch)

    # stroke: близость плотности штриха (относительно занятой ink-области).
    if src_stats["area"] > 0 and cand_stats["area"] > 0:
        src_den = src_stats["area"] / src_stats["width"]
        cand_den = cand_stats["area"] / cand_stats["width"]
        stroke_sim = 1.0 - abs(cand_den - src_den) / max(src_den, 1e-6)
    else:
        stroke_sim = 0.0

    # shape: IoU масок (одной высоты, выравнивание по сдвигу).
    shape_sim = _mask_iou_aligned(src_mask, cand_mask)

    score = (shape_sim * W_SHAPE + w_sim * W_WIDTH +
             h_sim * W_HEIGHT + max(0.0, stroke_sim) * W_STROKE)
    return {
        "score": float(score),
        "shape": float(shape_sim),
        "width": float(w_sim),
        "height": float(h_sim),
        "stroke": float(max(0.0, stroke_sim)),
    }


# ============================ Подбор строки ============================

def _crop_line(image_rgb, bbox_px, pad_px: int = 6):
    """Crop вокруг bbox с небольшим pad (4–8 px на 300DPI)."""
    W, H = image_rgb.size
    x0, y0, x1, y1 = [int(v) for v in bbox_px]
    cx0 = max(0, x0 - pad_px)
    cy0 = max(0, y0 - pad_px)
    cx1 = min(W, x1 + pad_px)
    cy1 = min(H, y1 + pad_px)
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    return image_rgb.crop((cx0, cy0, cx1, cy1))

def _guess_category(text: str, crop_mask=None) -> Optional[str]:
    
    if not text:
        return "sans"
    digits = sum(c.isdigit() for c in text)
    if len(text) >= 5 and digits / len(text) >= 0.75:
        return "mono"
    if crop_mask is not None:
        return _detect_serif_and_contrast(crop_mask)
    return "sans"

def _representative_lines(paddle_data: List[dict], image_rgb, max_lines: int = 5) -> List[dict]:
    
    from PIL import Image
    heights = []
    for item in paddle_data:
        b = item.get("bbox_px")
        if b and len(b) == 4:
            heights.append(b[3] - b[1])
    if heights:
        import statistics
        median_h = statistics.median(heights)
    else:
        median_h = 0.0

    selected = []
    candidates = []
    for i, item in enumerate(paddle_data):
        text = (item.get("text") or "").strip()
        bbox_px = item.get("bbox_px")
        conf = float(item.get("confidence", 0.0))
        if not text or not bbox_px or len(bbox_px) != 4:
            continue
        # Не строка из 1–2 символов, не только цифры.
        if len(text) < 4:
            continue
        if sum(c.isdigit() for c in text) == len(text):
            continue
        if conf < 0.70:
            continue
        bh = bbox_px[3] - bbox_px[1]
        if bh < 8 or bh > 160:
            continue
        # §6: аномально высокий bbox (слишком high для типичной строки):
        # не относительно 160px, а относительно МЕДИАНЫ страницы.
        if median_h > 0 and bh > median_h * ABNORMAL_HEIGHT_FACTOR:
            continue
        crop = _crop_line(image_rgb, bbox_px)
        if crop is None:
            continue
        ink = _ink_mask_stats(_binarize(np_float(_contrast_stretch(ImageOps.grayscale(crop)))))
        contrast_ok = ink["area"] > 0
        if not contrast_ok:
            continue
        candidates.append((conf, len(text), i, text, list(bbox_px), ink))

    # Приоритет: длина строки (главное), затем confidence.
    candidates.sort(key=lambda c: (c[1], c[0]), reverse=True)
    for conf, ln, i, text, bbox_px, ink in candidates[:max_lines]:
        selected.append({
            "index": i,
            "text": text,
            "bbox_px": bbox_px,
            "confidence": conf,
            "ink_stats": ink,
        })
    return selected


def _adjust_scalex(rendered_width_px: float, src_width_px: float) -> tuple:
    
    if rendered_width_px <= 0 or src_width_px <= 0:
        return 1.0, 1.0
    needed = src_width_px / rendered_width_px
    if SCALE_X_MIN <= needed <= SCALE_X_MAX:
        return round(needed, 3), 1.0
    # За пределами тонкой коррекции: вернуть лимит и флаг пересчёта размера.
    sx = min(SCALE_X_MAX, max(SCALE_X_MIN, needed))
    return round(sx, 3), max(0.55, 1.0 - abs(needed - 1.0))


def _resolve_width(size_px: float, src_width_px: float, src_height_px: float,
                   text: str, font_path: str, weight: int):
    """Согласует визуальную ширину: если тонкого scale_x (0.95..1.05) не хватает —
    пересчитываем font_size пропорционально нужному множителю (§8)."""
    _, cand_stats, _ = render_text_mask(font_path, text, size_px, weight, False)
    needed = src_width_px / max(1.0, cand_stats["width"])
    sx, _penalty = _adjust_scalex(cand_stats["width"], src_width_px)

    if SCALE_X_MIN <= needed <= SCALE_X_MAX:
        return size_px, sx, 1.0

    # Экстремальный needed: корректируем кегль, чтобы не сплющивать текст.
    adjusted = max(1.0, size_px * needed)
    # Кегль не выходим за разумные пределы (4–24 pt).
    max_px = MAX_FONT_PT * (PIXELS_PER_INCH / 72.0)
    min_px = MIN_FONT_PT * (PIXELS_PER_INCH / 72.0)
    adjusted = min(max_px, max(min_px, adjusted))
    # После пересчёта размера scale_x уже почти 1.0 (тонкая поправка остаётся).
    _, new_stats, _ = render_text_mask(font_path, text, adjusted, weight, False)
    new_sx, _ = _adjust_scalex(new_stats["width"], src_width_px)
    # Если даже после пересчёта размера требуется экстремальный scale_x — 
    # строка не соответствует (короткая/широкая), понижаем уверенность.
    pen = 1.0
    if abs(new_sx - 1.0) > SCALE_X_RECOMPUTE_THRESHOLD:
        pen = 0.55
    return adjusted, new_sx, pen


def _fit_size_px_for_font(src_ink_h_px: float, text: str, font_path: str,
                          probe_size_px: float = 24.0) -> float:
    """Оценка эм-размера (px), при котором ink-высота кандидата совпадает с
    высотой исходной строки. Учитывает разницу метрик шрифтов (cap/x-height)."""
    _, cs, _ = render_text_mask(font_path, text, probe_size_px, 400, False)
    if cs["height"] <= 0:
        return probe_size_px
    return src_ink_h_px * probe_size_px / cs["height"]


def _detect_serif_and_contrast(crop_mask) -> dict:
    
    import numpy as np

    ys, xs = np.nonzero(crop_mask)
    if ys.size < 40:
        return {"category": "sans", "confidence": 0.5}

    h = int(ys.max() - ys.min() + 1)
    if h < 10:
        return {"category": "sans", "confidence": 0.5}

    box = crop_mask[int(ys.min()):int(ys.max()) + 1, int(xs.min()):int(xs.max()) + 1] > 0
    H, W = box.shape

    # 1. Замер плотности на крайних 15% высоты (где живут засечки) против центральных 40%
    pad_h = max(2, int(H * 0.15))
    top_band = box[:pad_h, :]
    mid_band = box[int(H * 0.30):int(H * 0.70), :]
    bot_band = box[-pad_h:, :]

    top_d = float(top_band.sum()) / float(top_band.size or 1)
    mid_d = float(mid_band.sum()) / float(mid_band.size or 1)
    bot_d = float(bot_band.sum()) / float(bot_band.size or 1)

    # Засечки всегда создают утолщения сверху и снизу строки
    serif_ratio = (top_d + bot_d) / (2.0 * max(mid_d, 1e-4))

    # 2. Контраст вертикаль / горизонталь (у Times вертикальные штрихи толще горизонтальных)
    v_prof = box.sum(axis=0)
    h_prof = box.sum(axis=1)
    v_med = float(np.median(v_prof[v_prof > 0])) if (v_prof > 0).any() else 1.0
    h_med = float(np.median(h_prof[h_prof > 0])) if (h_prof > 0).any() else 1.0
    contrast_ratio = v_med / max(h_med, 1.0)

    # Честное правило: засечки есть ТОЛЬКО если края заметно шире середины И есть контраст штриха
    is_serif = (serif_ratio > 1.22) and (contrast_ratio > 1.15)

    return {
        "category": "serif" if is_serif else "sans",
        "serif_ratio": round(serif_ratio, 3),
        "contrast_ratio": round(contrast_ratio, 3)
    }

def fit_line_typography(line: dict, image_rgb, hint_category: Optional[str]) -> TypographyStyle:
    """Универсальный подбор стиля для ОДНОЙ строки."""
    text = line["text"]
    bbox_px = line["bbox_px"]
    src_crop = _crop_line(image_rgb, bbox_px)
    if src_crop is None:
        return _fallback_style("Arial", 0.0)

    src_mask = _preprocess_crop(src_crop)
    src_stats = _ink_mask_stats(src_mask)
    if src_stats["height"] <= 0:
        return _fallback_style("Arial", 0.0)

    # 1. Авто-анализ категории строки по растру (если не задан явный mono)
    auto_cat = "sans"
    if hint_category == "mono":
        auto_cat = "mono"
    else:
        analysis = _detect_serif_and_contrast(src_mask)
        auto_cat = analysis["category"]

    bh_px = max(1.0, bbox_px[3] - bbox_px[1])
    geometric_px = bh_px * SIZE_HEURISTIC
    step_px = SIZE_STEP_PT * (PIXELS_PER_INCH / 72.0)

    font_candidates = _candidate_fonts(auto_cat)
    best = None

    # 2. Оценка кандидатов
    for fc in font_candidates:
        fam = fc["family"]
        path = fc["path"]
        em_fit = _fit_size_px_for_font(src_stats["height"], text, path)
        sizes_px = [em_fit - step_px, em_fit, em_fit + step_px]
        if abs(geometric_px - em_fit) <= 2.0 * step_px:
            sizes_px.append(geometric_px)
        sizes_px = list(dict.fromkeys(round(s, 2) for s in sizes_px))

        bold_path = resolve_system_font(fam, is_bold=True) if _has_variant(fam, weight=700) else None
        regular_path = path

        for size_px in sizes_px:
            size_px = max(1.0, min(size_px, int(MAX_FONT_PT * (PIXELS_PER_INCH / 72.0))))
            reg_score = score_candidate(src_mask, src_stats, text, regular_path, size_px, 400, False)["score"]
            bold_score = None
            if bold_path:
                bold_score = score_candidate(src_mask, src_stats, text, bold_path, size_px, 700, False)["score"]

            choose_bold = bold_score is not None and (bold_score - reg_score) >= WEIGHT_BOLD_MARGIN
            render_path = bold_path if choose_bold else regular_path
            weight = 700 if choose_bold else 400
            details = score_candidate(src_mask, src_stats, text, render_path, size_px, weight, False)

            if best is None or details["score"] > best.score:
                best = Candidate(
                    family=fam, font_path=render_path, size_px=size_px, weight=weight,
                    italic=False, score=details["score"], scale_x=1.0,
                    conf=details["score"], detail=details,
                )

    if best is None:
        return _fallback_style("Arial", 0.0)

    adj_size_px, sx, sx_penalty = _resolve_width(
        best.size_px, src_stats["width"], src_stats["height"],
        text, best.font_path, best.weight)

    conf = round(best.score * sx_penalty, 3)
    conf = max(0.0, min(1.0, conf))
    size_pt = round(adj_size_px * (72.0 / PIXELS_PER_INCH), 1)
    size_pt = max(MIN_FONT_PT, min(MAX_FONT_PT, size_pt))

    logger.info(f"[TYPO LINE] '{text[:22]:<22}' | Категория: {auto_cat:<5} | Подобрано: {best.family:<15} {size_pt}pt (w={best.weight})")

    return TypographyStyle(
        family=best.family,
        font_size=size_pt,
        weight=best.weight,
        italic=best.italic,
        scale_x=round(sx, 3),
        baseline_offset=0.0,
        confidence=conf,
        source="auto" if conf >= CONFIDENCE_THRESHOLD else "fallback",
        candidates=[],
    )


def _has_variant(family: str, weight: int = 700) -> bool:
    if weight == 700:
        return resolve_system_font(family, is_bold=True) is not None
    return True


def _fallback_style(family: Optional[str], conf: float) -> TypographyStyle:
    size = 11.0
    # Arial как основной дефолт для документов и счетов вместо Times New Roman
    fam = family or "Arial"
    return TypographyStyle(
        family=fam, font_size=size, weight=400, italic=False,
        scale_x=1.0, baseline_offset=0.0, confidence=min(conf, 1.0),
        source="fallback",
    )


# ============================ Page Style Profile ============================

def _most_common(styles: List[TypographyStyle], attr: str, default):
    counts = {}
    for s in styles:
        v = getattr(s, attr)
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return default
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def compose_page_profile(line_styles: List[TypographyStyle]) -> TypographyStyle:
    
    if not line_styles:
        return _fallback_style("Times New Roman", 0.0)

    fam = _most_common(line_styles, "family", "Times New Roman")
    weight = _most_common(line_styles, "weight", 400)
    italic = _most_common(line_styles, "italic", False)
    sizes = sorted(s.font_size for s in line_styles if s.font_size > 0)
    med_size = sizes[len(sizes) // 2] if sizes else 11.0
    sx = [s.scale_x for s in line_styles if s.scale_x]
    avg_sx = round(sum(sx) / len(sx), 3) if sx else 1.0
    confs = [s.confidence for s in line_styles]
    avg_conf = round(sum(confs) / len(confs), 3) if confs else 0.0

    return TypographyStyle(
        family=fam,
        font_size=round(med_size, 1),
        weight=weight,
        italic=italic,
        scale_x=avg_sx,
        baseline_offset=0.0,
        confidence=avg_conf,
        source="auto" if avg_conf >= CONFIDENCE_THRESHOLD else "fallback",
    )


def _height_groups(paddle_data: list) -> List[dict]:
    
    items = []
    for i, it in enumerate(paddle_data):
        b = it.get("bbox_px")
        if not b or len(b) != 4:
            continue
        h_px = b[3] - b[1]
        h_pt = h_px * (72.0 / PIXELS_PER_INCH)
        items.append({"idx": i, "h_pt": round(h_pt, 1)})

    items.sort(key=lambda x: x["h_pt"])
    groups = []
    cur = None
    for it in items:
        if cur is None or abs(it["h_pt"] - cur["h_pt"]) > HEIGHT_GROUP_TOL_PT:
            cur = {"h_pt": it["h_pt"], "idx": []}
            groups.append(cur)
        cur["idx"].append(it["idx"])
    return groups


def _resolve_width_for_line(idx: int, paddle_data: list, image_rgb,
                             family: str, weight: int, group_size_pt: float) -> tuple:
    
    item = paddle_data[idx]
    text = (item.get("text") or "").strip()
    bbox_px = item.get("bbox_px")
    if not text or not bbox_px or len(bbox_px) != 4:
        return group_size_pt, 1.0

    
    if re.search(r"\s{3,}", text):
        return group_size_pt, 1.0

    
    if len(text) <= 2:
        font_path = resolve_system_font(family, is_bold=(weight >= 700))
        if not font_path:
            return group_size_pt, 1.0
        size_px = max(1.0, group_size_pt * (PIXELS_PER_INCH / 72.0))
        _, cand_stats, _ = render_text_mask(font_path, text, size_px, weight, False)
        bbox_w_px = bbox_px[2] - bbox_px[0]
        sx, _penalty = _adjust_scalex(cand_stats["width"], bbox_w_px)
        return group_size_pt, round(sx, 3)

    crop = _crop_line(image_rgb, bbox_px)
    if crop is None:
        return group_size_pt, 1.0

    mask = _preprocess_crop(crop)
    stats = _ink_mask_stats(mask)
    if stats["height"] <= 0 or stats["width"] <= 0:
        return group_size_pt, 1.0

    font_path = resolve_system_font(family, is_bold=(weight >= 700))
    if not font_path:
        return group_size_pt, 1.0

    size_px = max(1.0, group_size_pt * (PIXELS_PER_INCH / 72.0))
    adj_px, sx, _penalty = _resolve_width(size_px, stats["width"], stats["height"],
                                          text, font_path, weight)

    size_pt = round(adj_px * (72.0 / PIXELS_PER_INCH), 1)
    size_pt = max(MIN_FONT_PT, min(MAX_FONT_PT, size_pt))
    return size_pt, round(sx, 3)


def _group_median_height(idxs: List[int], paddle_data: list) -> float:
    hs = []
    for i in idxs:
        b = paddle_data[i].get("bbox_px")
        if b and len(b) == 4:
            hs.append(b[3] - b[1])
    if not hs:
        return 0.0
    hs.sort()
    return hs[len(hs) // 2]


# ============================ Публичный фасад ============================

@dataclass
class PageTypography:
    page_profile: TypographyStyle
    line_styles: Dict[int, TypographyStyle]  # индекс строки в paddle_data
    groups: List[dict] = field(default_factory=list)  # диагностика (высоты групп)


def build_page_typography(image_path: str, paddle_data: list,
                          representative_lines: Optional[int] = 5) -> PageTypography:
    """Главная точка входа: подбирает стиль страницы по реальному снимку скана."""
    try:
        cache_key = (os.path.abspath(image_path), os.path.getsize(image_path),
                     len(paddle_data))
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _PAGE_STYLE_CACHE:
        return _PAGE_STYLE_CACHE[cache_key]

    try:
        from PIL import Image
        image_rgb = Image.open(image_path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[TYPO] не удалось открыть изображение страницы: {exc}")
        fb = _fallback_style("Arial", 0.0)
        res = PageTypography(page_profile=fb, line_styles={})
        if cache_key is not None:
            _PAGE_STYLE_CACHE[cache_key] = res
        return res

    reps = _representative_lines(paddle_data, image_rgb, max_lines=representative_lines)
    if not reps:
        fb = _fallback_style("Arial", 0.0)
        res = PageTypography(page_profile=fb, line_styles={})
        if cache_key is not None:
            _PAGE_STYLE_CACHE[cache_key] = res
        return res

    # 1. Fit стили представительных строк.
    rep_styles = {}
    for rep in reps:
        
        crop = _crop_line(image_rgb, rep["bbox_px"])
        mask = _preprocess_crop(crop) if crop is not None else None
        hint = _guess_category(rep["text"], crop_mask=mask)
        st = fit_line_typography(rep, image_rgb, hint)
        st.family = _final_family(st.family)
        rep_styles[rep["index"]] = st

    # 2. Единый "страничный" профиль (family / начертание / размер-медиана).
    profile = compose_page_profile(list(rep_styles.values()))
    profile.family = _final_family(profile.family)

    # 3. Группы близких высот -> свой style на каждую строку.
    groups = _height_groups(paddle_data)
    line_styles = {}
    for g in groups:

        med_h_px = _group_median_height(g["idx"], paddle_data)
        group_size_pt = max(MIN_FONT_PT, min(24.0, med_h_px * SIZE_HEURISTIC * (72.0 / PIXELS_PER_INCH)))

        rep_idx_in_group = [i for i in g["idx"] if i in rep_styles]
        if rep_idx_in_group:
            i0 = rep_idx_in_group[0]
            base = rep_styles[i0]
            group_size_pt = base.font_size
            fam = base.family
            weight = base.weight
            italic = base.italic
            conf = base.confidence
            src = base.source
        else:
            fam = profile.family
            weight = profile.weight
            italic = profile.italic
            conf = profile.confidence
            src = "inherited" if profile.confidence >= CONFIDENCE_THRESHOLD else profile.source


        for idx in g["idx"]:
            line_size_pt, line_scale_x = _resolve_width_for_line(
                idx, paddle_data, image_rgb, fam, weight, group_size_pt
            )
            line_styles[idx] = TypographyStyle(
                family=fam,
                font_size=line_size_pt,
                weight=weight,
                italic=italic,
                scale_x=line_scale_x,
                baseline_offset=0.0,
                confidence=conf,
                source=src,
            )

    res = PageTypography(page_profile=profile, line_styles=line_styles, groups=groups)
    if cache_key is not None:
        _PAGE_STYLE_CACHE[cache_key] = res
    return res


def _final_family(family: str) -> str:

    fam = (family or "").lower()
    if "libera" in fam:  # Liberation Serif/Sans/Mono
        if "serif" in fam:
            return "Times New Roman"
        if "mono" in fam:
            return "Courier New"
        return "Arial"
    if "dejavu" in fam:
        if "serif" in fam:
            return "Times New Roman"
        if "mono" in fam:
            return "Courier New"
        return "Arial"
    return family or "Times New Roman"


def clear_caches() -> None:
    _MASK_CACHE.clear()
    _PAGE_STYLE_CACHE.clear()
    with _FONT_CACHE_LOCK:
        _FONT_OBJ_CACHE.clear()