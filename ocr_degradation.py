# -*- coding: utf-8 -*-


from __future__ import annotations

import io
import logging
import math
import os
import threading
from typing import Any, Dict, Tuple, Optional
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFilter = ImageFont = None

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

logger = logging.getLogger("ocr_degradation")

# ============================ Потокобезопасный кэш FreeType-шрифтов ============================
# Аналогично ocr_typography.py: render_text_patch тоже дергает ImageFont.truetype
# на каждую строку. При параллельной обработке нескольких страниц (несколько
# потоков) конкурентное открытие/использование FreeType-шрифта может уронить
# процесс на Linux - поэтому открытие и использование FT_Face идут под общим
# локом, а уже открытые шрифты кэшируются по (путь, размер_px).
_FONT_CACHE_LOCK = threading.RLock()
_FONT_OBJ_CACHE: Dict[Tuple[str, int], Any] = {}


def _get_cached_font(font_path: str, size_px: int) -> Any:
    """Возвращает уже открытый шрифт из кэша либо открывает и кэширует новый.
    Вызывать ТОЛЬКО под _FONT_CACHE_LOCK."""
    key = (font_path, int(size_px))
    font = _FONT_OBJ_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(font_path, size=int(size_px))
        _FONT_OBJ_CACHE[key] = font
    return font

# ============================ Параметры деградации текста ============================

# Включение деградации OCR-текста
ENABLE_OCR_TEXT_NOISE = True

# Суперсэмплинг маски (рендер глифа в 4 раза выше размера в pt)
OCR_TEXT_SUPERSAMPLE = 4

# Минимальное оптическое размытие: четкий текст без мыла
OCR_TEXT_BLUR_RADIUS_PT = 0.08

# Микрошум краев: убирает только пиксельную лесенку
OCR_TEXT_EDGE_NOISE_STEP = 0.05

# Сплошная сердцевина штриха
OCR_TEXT_CORE_THRESHOLD = 240.0

# Границы переходной зоны края (по alpha)
OCR_TEXT_EDGE_LOW = 15.0
OCR_TEXT_EDGE_HIGH = 235.0

# ---- Анализ чернил (Ink Sampling) ----
OCR_TEXT_INK_CT_DELTA = 26.0
OCR_TEXT_BG_PERCENTILE = 0.90
OCR_TEXT_INK_PERCENTILE = 0.15
OCR_TEXT_SUPPRESS_CT = 90.0
OCR_TEXT_INK_NOISE = 2

# Fallback-цвет чернил: плотный черный тон скана
OCR_TEXT_INK_FALLBACK_RGB = (15, 15, 18)

# Гарнитура по умолчанию
OCR_TEXT_DEFAULT_FAMILY = "Arial"

# Полное отключение искусственного утолщения: исключает слипание букв
OCR_TEXT_MAX_THICKEN_PT = 0.0

# Длина линии для отсечения табличной сетки
OCR_TEXT_LINE_REMOVE_LEN = 40
OCR_TEXT_STROKE_BINARY_THRESH = 127.0


INK_SAMPLE_PAD_PT = 12.0
# DPI снятия региона для Ink Sampling.
INK_SAMPLE_DPI = 150

# Ограничение размера встраиваемого PNG (максимальная сторона в пикселях).
_MAX_PATCH_PX = 6000


class OcrTextDegradeParams:
    """Параметры деградации для одного вызова (детерминированные поля)."""

    def __init__(self, text: str):
        # Детерминированное сид-зерно из текста.
        self.seed = len(text) * 131 + (sum(ord(c) for c in text) % 100000)
        self.enabled = bool(ENABLE_OCR_TEXT_NOISE)
        self.sup = max(1, int(OCR_TEXT_SUPERSAMPLE))
        self.blur_pt = max(0.0, float(OCR_TEXT_BLUR_RADIUS_PT))
        self.edge_noise_step = max(0.0, float(OCR_TEXT_EDGE_NOISE_STEP))
        self.core_threshold = float(OCR_TEXT_CORE_THRESHOLD)
        self.edge_low = max(0.0, float(OCR_TEXT_EDGE_LOW))
        self.edge_high = max(self.edge_low + 1.0, min(255.0, float(OCR_TEXT_EDGE_HIGH)))
        if self.edge_high >= self.core_threshold:
            self.edge_high = max(self.edge_low + 1.0, self.core_threshold - 1.0)
        self.ct_delta = max(1.0, float(OCR_TEXT_INK_CT_DELTA))
        self.bg_percentile = min(0.999, max(0.5, float(OCR_TEXT_BG_PERCENTILE)))
        self.ink_percentile = min(0.5, max(0.01, float(OCR_TEXT_INK_PERCENTILE)))
        self.suppress_ct = max(10.0, float(OCR_TEXT_SUPPRESS_CT))
        self.ink_noise = max(0, int(OCR_TEXT_INK_NOISE))
        self.ink_fallback = tuple(int(v) for v in OCR_TEXT_INK_FALLBACK_RGB)
        self.max_thicken_pt = max(0.0, float(OCR_TEXT_MAX_THICKEN_PT))
        self.line_remove_len = max(3, int(OCR_TEXT_LINE_REMOVE_LEN))
        self.stroke_binary_thresh = min(245.0, max(30.0, float(OCR_TEXT_STROKE_BINARY_THRESH)))


def _rng(seed: int):
    if np is not None:
        return np.random.default_rng(seed)
    return None


# ============================ 3.1 Ink Sampling ============================

def _remove_long_lines(ink_mask, line_remove_len: int):

    if np is None or ink_mask is None or int(np.asarray(ink_mask).sum()) == 0:
        return ink_mask
    try:
        from scipy import ndimage
        H, W = ink_mask.shape[:2]
        L_h = max(int(line_remove_len), max(2, int(round(0.85 * W))))
        L_v = max(int(line_remove_len), max(2, int(round(0.85 * H))))
        hl = np.ones((1, L_h), dtype=bool)
        vl = np.ones((L_v, 1), dtype=bool)
        h_open = ndimage.binary_opening(ink_mask, structure=hl)
        v_open = ndimage.binary_opening(ink_mask, structure=vl)
        return ink_mask & ~(h_open | v_open)
    except Exception:  # noqa: BLE001
        return ink_mask


def _stroke_width_pt(binary_mask, px_per_pt: float) -> float:

    if np is None or binary_mask is None or int(binary_mask.sum()) == 0:
        return 0.0
    try:
        from scipy import ndimage
        dt = ndimage.distance_transform_edt(binary_mask)
        ridge = (ndimage.maximum_filter(dt, size=3) == dt) & binary_mask & (dt > 0.5)
        rv = dt[ridge]
        if rv.size == 0:
            rv = dt[binary_mask & (dt > 0)]
        if rv.size == 0:
            return 0.0
        half_width_px = float(np.median(rv))
        stroke_px = 2.0 * half_width_px
        return max(0.0, stroke_px * px_per_pt)
    except Exception:  # noqa: BLE001
        return 0.0


def sample_ink_rgb(region_rgb, region_rect, bbox_disp, p: OcrTextDegradeParams):

    rgbf = p.ink_fallback
    if np is None or region_rgb is None:
        return rgbf, 0.0, 0.0
    try:
        arr = np.asarray(region_rgb, dtype=np.float32)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return p.ink_fallback, 0.0
        arr = arr[..., :3]
        H, W = arr.shape[:2]
        if H < 3 or W < 3:
            return p.ink_fallback, 0.0

        lum = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        bg_lum = float(np.percentile(lum, max(0.0, min(100.0, p.bg_percentile * 100.0))))
        bg_rgb = np.percentile(arr.reshape(-1, 3), max(0.0, min(100.0, p.bg_percentile * 100.0)), axis=0)

        def _candidates(lum_sel, arr_sel):
            dark = (bg_lum - lum_sel) > p.ct_delta
            if arr_sel.ndim == 3:
                dist = np.sqrt(np.sum((arr_sel - bg_rgb[None, None, :]) ** 2, axis=-1))
            else:
                dist = np.sqrt(np.sum((arr_sel - bg_rgb[None, :]) ** 2, axis=-1))
            return (dark | (dist > p.ct_delta * 1.4)), dist

        # Целевой бокс текста внутри региона (в пикселях растра региона).
        rx = 0.0 if region_rect is None else float(region_rect[0])
        ry = 0.0 if region_rect is None else float(region_rect[1])
        rw = 1.0 if region_rect is None else max(1e-6, float(region_rect[2] - region_rect[0]))
        rh = 1.0 if region_rect is None else max(1e-6, float(region_rect[3] - region_rect[1]))
        sx = W / rw
        sy = H / rh
        bx0 = max(0, min(W, int(round((bbox_disp[0] - rx) * sx))))
        by0 = max(0, min(H, int(round((bbox_disp[1] - ry) * sy))))
        bx1 = max(bx0 + 1, min(W, int(round((bbox_disp[2] - rx) * sx))))
        by1 = max(by0 + 1, min(H, int(round((bbox_disp[3] - ry) * sy))))

        inner_lum = lum[by0:by1, bx0:bx1]
        inner_arr = arr[by0:by1, bx0:bx1]
        ink_mask, _ = _candidates(inner_lum, inner_arr)
        cand = inner_arr[ink_mask]
        cand_lum = inner_lum[ink_mask]


        used_mask = None


        if cand.shape[0] < 12 and (bx1 - bx0 < W or by1 - by0 < H):
            ink_mask_all, _ = _candidates(lum, arr)
            cand = arr[ink_mask_all]
            cand_lum = lum[ink_mask_all]
            used_mask = ink_mask_all

        if cand.shape[0] < 6:
            return p.ink_fallback, max(0.0, bg_lum - float(np.median(lum))), 0.0

        if used_mask is None:
            used_mask = ink_mask


        valid_dark = cand[cand_lum < 110.0]
        if valid_dark.shape[0] >= 6:
            p10_lum = np.percentile(cand_lum[cand_lum < 110.0], 15.0)
            core_pixels = cand[cand_lum <= p10_lum]
            base = np.median(core_pixels, axis=0) if core_pixels.shape[0] else np.median(valid_dark, axis=0)
        else:
            base = np.array(p.ink_fallback, dtype=np.float32)


        base = np.minimum(base, [42.0, 42.0, 46.0])
        base = np.clip(base, 0, 255)
        contrast = max(0.0, float(bg_lum - np.median(cand_lum)))

        # Замер толщины штриха на ОЧИЩЕННОЙ маске (убраны линии таблиц/сетки).
        orig_stroke_pt = 0.0
        if used_mask is not None:
            px_per_pt = 72.0 / INK_SAMPLE_DPI
            clean_mask = _remove_long_lines(used_mask, p.line_remove_len)
            orig_stroke_pt = _stroke_width_pt(clean_mask, px_per_pt)

        return (int(round(base[0])), int(round(base[1])), int(round(base[2]))), contrast, orig_stroke_pt
    except Exception:  # noqa: BLE001
        return p.ink_fallback, 0.0, 0.0


# ============================ 3.2 / 3.3 Deградация alpha ============================

# Стало:
def _degrade_alpha(alpha: Any, p: OcrTextDegradeParams, activity: float = 1.0) -> Any:

    if p.sup <= 0:
        return alpha
    rng = _rng(p.seed)

    a = np.asarray(alpha, dtype=np.float32).copy()
    core = a >= float(p.core_threshold)
    band = (a > float(p.edge_low)) & (a < float(p.edge_high))


    blur_px = p.blur_pt * p.sup * (0.5 + 0.5 * max(0.0, min(1.0, activity)))
    if blur_px > 0.05:
        from PIL import Image as _PIL
        img = _PIL.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L")
        blurred = np.asarray(img.filter(ImageFilter.GaussianBlur(radius=blur_px)), dtype=np.float32)
    else:
        blurred = a.copy()
    out = blurred

    # Микрошум/субпиксельный джиттер ТОЛЬКО в переходной зоне (строго не в теле).
    if p.edge_noise_step > 0.0 and rng is not None:
        sigma = p.edge_noise_step * 70.0 * (0.15 + 0.85 * max(0.0, min(1.0, activity)))
        if sigma > 0.5:
            noise = rng.normal(0, sigma, size=a.shape).astype(np.float32)
            out = out + noise * band.astype(np.float32)

    out = np.clip(out, 0, 255)

    solid_core = a >= 200.0
    out[solid_core] = 255.0
    return out


# ============================ Публичный рендер патча ============================

# Стало:
def render_text_patch(text: str, font_path: str, font_size_pt: float,
                      scale_x: float = 1.0, baseline_offset: float = 0.0,
                      dx0: float = 0.0, dy0: float = 0.0,
                      ink_context: Optional[dict] = None) -> Tuple[Optional[bytes], Optional[dict]]:

    if not (ENABLE_OCR_TEXT_NOISE and Image is not None and np is not None):
        return None, None
    if not font_path or os.path.exists(font_path) is False:
        return None, None
    text = (text or "")
    if not text.strip():
        return None, None

    p = OcrTextDegradeParams(text)
    sup = max(1, p.sup)
    size_px = max(4, int(round(font_size_pt * sup)))

    # Вся работа с FT_Face (получение из кэша/открытие + метрики + рисование)
    # выполняется под одним локом - защищает FreeType от параллельного доступа
    # из нескольких потоков, обрабатывающих разные страницы одновременно.
    with _FONT_CACHE_LOCK:
        try:
            font = _get_cached_font(font_path, size_px)
            ascent, descent = font.getmetrics()
        except Exception:  # noqa: BLE001
            return None, None

        try:
            text_w_px = int(math.ceil(font.getlength(text)))
        except Exception:  # noqa: BLE001
            text_w_px = int(size_px * len(text) * 0.6)

        margin = max(2, int(round(sup)))
        canvas_w = text_w_px + 2 * margin
        canvas_h = int(ascent + descent) + 2 * margin
        baseline_y = margin + ascent

        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        try:
            draw = ImageDraw.Draw(img)
            draw.text((margin, baseline_y), text, font=font, fill=(0, 0, 0, 255), anchor="ls")
        except Exception:  # noqa: BLE001
            return None, None

    alpha_arr = np.asarray(img.split()[3], dtype=np.float32)

    # Инк-bbox маски (где реальные чернила).
    ys, xs = np.nonzero(alpha_arr > 1)
    if ys.size == 0:
        return None, None
    ix0, iy0, ix1, iy1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1

    crop_alpha = alpha_arr[iy0:iy1, ix0:ix1].copy()


    max_a = float(crop_alpha.max())
    if max_a > 10.0 and max_a < 254.0:
        # Нелинейное усиление непрозрачности ядра штриха
        scale = 255.0 / max_a
        crop_alpha = np.clip(crop_alpha * scale, 0.0, 255.0)
        # Прижимаем полутона ближе к полной плотности
        crop_alpha = np.where(crop_alpha > 120.0, 
                              120.0 + (crop_alpha - 120.0) * (135.0 / max(1.0, 255.0 - 120.0)), 
                              crop_alpha)

    # ---- Цвет чернил и толщина штриха: Ink Sampling (3.1/3.2) или fallback ----
    base_ink_rgb = p.ink_fallback
    ink_contrast = 0.0
    orig_stroke_pt = 0.0
    if ink_context is not None:
        try:
            region = ink_context.get("region_rgb")
            rrect = ink_context.get("region_rect")
            obb = ink_context.get("bbox_disp")
            if region is not None and obb is not None:
                base_ink_rgb, ink_contrast, orig_stroke_pt = sample_ink_rgb(region, rrect, obb, p)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OCR] не удалось определить цвет чернил: {exc}")
            base_ink_rgb = p.ink_fallback
            ink_contrast = 0.0
            orig_stroke_pt = 0.0

    # Автоподавление пост-обработки на низком контрасте чернил (3.3).
    _activity = max(0.0, min(1.0, (ink_contrast - 20.0) / max(1.0, p.suppress_ct - 20.0)))

    # Горизонтальный масштаб (scale_x) через resize маски (геометрия 3.4).
    crop_w = ix1 - ix0
    if abs(scale_x - 1.0) > 1e-4 and scale_x > 0.0:
        new_w = max(1, int(round(crop_w * scale_x)))
        from PIL import Image as _PIL
        m_img = _PIL.fromarray(crop_alpha.astype(np.uint8), "L")
        m_img = m_img.resize((new_w, crop_alpha.shape[0]), Image.BILINEAR)
        crop_alpha = np.asarray(m_img, dtype=np.float32)


    stroke_thickened = False

    # Локализованная деградация alpha (3.2/3.3).
    crop_alpha = _degrade_alpha(crop_alpha, p, activity=_activity)

    # RGB чернил: base_ink_rgb + небольшой разброс яркости (гран сканера).
    rng = _rng(p.seed + 1)
    ink_rgb = np.full(crop_alpha.shape[:2] + (3,), base_ink_rgb, dtype=np.float32)
    if rng is not None and p.ink_noise > 0:
        ink_rgb = ink_rgb + rng.normal(0, p.ink_noise, size=ink_rgb.shape)
    ink_rgb = np.clip(ink_rgb, 0, 255).astype(np.uint8)

    # Фон патча полностью прозрачный (alpha=0) - НЕ перекрываем скан.
    final = np.zeros(crop_alpha.shape[:2] + (4,), dtype=np.uint8)
    final[..., :3] = ink_rgb
    final[..., 3] = crop_alpha.astype(np.uint8)
    final = np.ascontiguousarray(final)

    patch_img = Image.fromarray(final, "RGBA")

    # Ограничение размера встраиваемого растра для очень длинных строк.
    if max(patch_img.size) > _MAX_PATCH_PX and _MAX_PATCH_PX > 0:
        k = _MAX_PATCH_PX / float(max(patch_img.size))
        new_size = (max(1, int(round(patch_img.size[0] * k))),
                    max(1, int(round(patch_img.size[1] * k))))
        patch_img = patch_img.resize(new_size, Image.BILINEAR)


    pts_x0 = max(dx0, dx0 + float(ix0 - margin) / sup)
    
    # Синхронный расчет базовой линии: центрирование по высоте бокса глифа
    box_mid_y = dy0 + (font_size_pt * 0.55)
    baseline_disp_y = box_mid_y + (font_size_pt * 0.35) + baseline_offset
    pts_y_top = baseline_disp_y + float(iy0 - baseline_y) / sup
    w_pt = float(crop_w) / sup * scale_x
    h_pt = float(iy1 - iy0) / sup

    buf = io.BytesIO()
    patch_img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue(), {
        "x": pts_x0,
        "y": pts_y_top,
        "w_pt": w_pt,
        "h_pt": h_pt,
        "base_ink_rgb": base_ink_rgb,
        "ink_contrast": round(ink_contrast, 2),
        "activity": round(_activity, 3),
        "orig_stroke_pt": round(orig_stroke_pt, 3),
        "stroked_thickened": bool(stroke_thickened),
    }