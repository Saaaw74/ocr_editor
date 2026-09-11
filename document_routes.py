# -*- coding: utf-8 -*-
import os
import uuid
import logging
import io
import copy
from flask import Blueprint, request, jsonify, send_from_directory, send_file

import numpy as np
import fitz  # PyMuPDF
from ocr_pipeline import (
    process_single_page,
    DEBUG_SEARCHABLE_DIR,
    OcrPipelineError,
)
from ocr_typography import resolve_system_font, clear_caches
from native_pipeline import process_native_page
from docx_export import build_docx_from_udm_pages
from pipeline_vlm_export import export_modified_pdf_to_docx_vlm
from pdf_geometry import (
    display_to_raw_bbox,
    display_to_raw_point,
    raw_to_display_bbox,
    get_page_rotation_and_raw_size,
)
from ocr_degradation import (
    render_text_patch,
    ENABLE_OCR_TEXT_NOISE,
    INK_SAMPLE_PAD_PT,
    INK_SAMPLE_DPI,
    OCR_TEXT_DEFAULT_FAMILY,
)
from table_grid_protect import build_grid_line_mask, GRID_MASK_DPI

from job_manager import job_manager

logger = logging.getLogger("document_routes")
document_bp = Blueprint("document_bp", __name__, url_prefix="/api/document")

VALID_MODES = {"native", "ocr"}


def sample_bg_color(page, bbox_pt, default_color=(1.0, 1.0, 1.0)):
    """Автопипетка цвета бумаги под редактируемым текстом (СТАРЫЙ метод,
    ОДИН средний цвет по светлым пикселям внутри bbox). Оставлен как
    fallback на случай, если restore_local_background() не смог построить
    заплатку (см. ТЗ Часть 2 / apply_pdf_modifications ниже) — поведение
    не хуже прежнего."""
    try:
        rect = fitz.Rect(bbox_pt)
        pix = page.get_pixmap(clip=rect, dpi=150)
        if pix.width == 0 or pix.height == 0:
            return default_color

        samples = []
        for y in range(pix.height):
            for x in range(pix.width):
                r, g, b = pix.pixel(x, y)[:3]
                brightness = 0.299 * r + 0.587 * g + 0.114 * b
                if brightness > 190:
                    samples.append((r / 255.0, g / 255.0, b / 255.0))

        if samples:
            avg_r = sum(c[0] for c in samples) / len(samples)
            avg_g = sum(c[1] for c in samples) / len(samples)
            avg_b = sum(c[2] for c in samples) / len(samples)
            return (avg_r, avg_g, avg_b)
    except Exception:
        pass
    return default_color


def _nearest_neighbor_top_y(page_blocks, rect, exclude_id=None):
    """Находит нижнюю границу ближайшего соседа сверху с учетом реальных строк (lines)."""
    if not page_blocks:
        return None
    rx0, rx1 = min(rect.x0, rect.x1), max(rect.x0, rect.x1)
    best = None

    # Распаковываем и блоки, и внутренние lines, чтобы не пропускать строки таблиц/многострочных блоков
    flat_items = []
    for b in page_blocks:
        flat_items.append(b)
        for ln in (b.get("lines") or []):
            flat_items.append(ln)

    for b in flat_items:
        bid = b.get("id")
        if exclude_id is not None and bid:
            if bid == exclude_id or str(bid).startswith(f"{exclude_id}_") or str(exclude_id).startswith(f"{bid}_"):
                continue
        bbox = b.get("bbox") or {}
        try:
            bx0 = float(bbox["x1"])
            by0 = float(bbox["y1"])
            bx1 = float(bbox["x2"])
            by1 = float(bbox["y2"])
        except (KeyError, TypeError, ValueError):
            continue
        bx0, bx1 = min(bx0, bx1), max(bx0, bx1)
        by0, by1 = min(by0, by1), max(by0, by1)
        if bx1 <= bx0 or by1 <= by0:
            continue
        # Пересечение по оси X (хотя бы 4 pt перекрытия)
        overlap_x = min(rx1, bx1) - max(rx0, bx0)
        if overlap_x < 4.0:
            continue
        # Сосед строго сверху (допуск +1.5 pt на случай, если детекция PaddleOCR перекрыла строки)
        if by1 <= rect.y0 + 1.5 and by0 < rect.y0:
            if best is None or by1 > best:
                best = by1
    return best


def _neighbor_edge_clamp(eff, target_disp, neighbor_seg_disp, gap=0.3):
    """Обобщённая (rotation-agnostic) защита от наезда на соседний блок.

    И eff (текущий эффективный rect), и target_disp (исходный целевой rect),
    и neighbor_seg_disp (сегмент - вырожденный прямоугольник, обозначающий
    границу соседа) уже приведены В ОДНО (DISPLAY) пространство - см.
    restore_local_background. Раньше (см. историю функции) сравнение делалось
    напрямую в RAW-координатах и жёстко считало соседа "сверху" (по Y) -
    для повёрнутых на 90/270° страниц RAW "верх" на экране может оказаться
    слева/справа, и старая проверка молча не сработала бы. Здесь сторона
    соседа (сверху/снизу/слева/справа) определяется ГЕОМЕТРИЧЕСКИ, по
    фактическому взаимному положению уже в display-пространстве - работает
    одинаково для любой /Rotate страницы, без специальных случаев.
    """
    ex0, ey0, ex1, ey1 = eff
    nx0, ny0, nx1, ny1 = neighbor_seg_disp
    tcx = (target_disp[0] + target_disp[2]) / 2.0
    tcy = (target_disp[1] + target_disp[3]) / 2.0

    is_horizontal_seg = abs(ny1 - ny0) <= abs(nx1 - nx0)
    if is_horizontal_seg:
        ny = (ny0 + ny1) / 2.0
        if ny <= tcy:
            ey0 = max(ey0, ny + gap)
        else:
            ey1 = min(ey1, ny - gap)
        if ey0 >= ey1 - 2.0:
            ey0, ey1 = eff[1], eff[3]
    else:
        nx = (nx0 + nx1) / 2.0
        if nx <= tcx:
            ex0 = max(ex0, nx + gap)
        else:
            ex1 = min(ex1, nx - gap)
        if ex0 >= ex1 - 2.0:
            ex0, ex1 = eff[0], eff[2]
    return (ex0, ey0, ex1, ey1)


def restore_local_background(page, rect, dpi=150, neighbor_top_y=None,
                              rotation=0, raw_w=None, raw_h=None, grid_mask=None):
    """Локальное восстановление фона под удаляемым OCR/native-блоком (см. ТЗ,
    Часть 2), теперь + универсальная защита линий таблицы/бланка (см.
    table_grid_protect.py) и корректная работа на ЛЮБОЙ странице (в т.ч. с
    /Rotate != 0).

    ПРОБЛЕМА старого sample_bg_color(): один средний RGB-цвет по светлым
    пикселям ВНУТРИ самого bbox -> заливка всего прямоугольника этим ОДНИМ
    цветом (add_redact_annot(fill=color)). На неоднородном фоне скана (шум
    сканера, JPEG/PNG-артефакты, лёгкий градиент освещения, текстура бумаги)
    это давало заметную плоскую прямоугольную "заплатку".

    Здесь: берём тонкое кольцо пикселей ВОКРУГ bbox (не внутри — там ещё
    исходный OCR-текст на момент вызова, redact ещё не применён), отдельно
    для каждого из 4 краёв оцениваем средний цвет и разброс яркости фона.
    Из оценки фона ИСКЛЮЧАЮТСЯ тёмные пиксели И хроматические пиксели
    (насыщенность > 0.22: печати/подписи с низкой яркостью — это не фон и
    не обычный чёрно-белый OCR-текст). Затем билинейно интерполируем эти
    4 краевых оценки внутрь bbox — заплатка продолжает локальный
    градиент/неоднородность окружения.

    Шум добавляется АДАПТИВНО: только если фактический локальный разброс
    фона заметен (медиана std по краям >= 0.8 градаций текущего растра
    0..255; это не зависит от DPI). На однородном фоне шум = 0 — иначе он
    создаёт искусственное зерно. Амплитуда шума не превосходит реального
    локального разброса и прежнего максимума (8 градаций).

    Erase-rect может быть точечно расширен на 1–2 px ТЕКУЩЕГО растра (при
    заданном dpi, НЕ в PDF points) на тех гранях, где внутри bbox у самой
    границы есть "обрезанные" чернила OCR-текста (остатки букв), но ТОЛЬКО
    если полоса за гранью — чистый фон/хроматика (т.е. не соседний текст
    и не линия таблицы). Иначе грань не трогаем.

    neighbor_top_y: опциональная жёсткая геометрическая граница (Y ближайшего
    соседнего блока СВЕРХУ в RAW-пространстве, см. _nearest_neighbor_top_y
    выше). Переводится в то же (display) пространство, что и всё остальное
    внутри этой функции, и применяется через _neighbor_edge_clamp - см. ниже.

    rotation/raw_w/raw_h: геометрия страницы (см. pdf_geometry.py). ВАЖНО:
    page.get_pixmap() (используется ниже для сэмплирования пикселей) работает
    в DISPLAY-пространстве, а `rect`, который передаёт вызывающий код
    (apply_pdf_modifications), приходит в RAW-пространстве (то, что нужно
    add_redact_annot/insert_image). Раньше это несоответствие не
    учитывалось: RAW-rect напрямую использовался как clip для get_pixmap,
    что для НЕповёрнутых страниц (raw == display) незаметно, но для
    страниц с /Rotate давало сэмплирование/патч НЕ ТОГО участка скана.
    Здесь весь пиксельный анализ ЦЕЛИКОМ ведётся в display-пространстве,
    а результат (eff_rect) конвертируется обратно в raw только один раз,
    непосредственно перед return - именно в этом пространстве его ожидает
    вызывающий код.

    grid_mask: опциональный table_grid_protect.GridLineMask - глобальная (на
    всю страницу, посчитанная один раз ДО правок) карта пикселей линий
    таблицы/бланка. Если передан, используется как ОСНОВНАЯ (наиболее
    надёжная) защита линий - устойчива к перекосу скана, светло-серым
    линиям и Т-образным стыкам/перекрестиям/углам (см. table_grid_protect.py).
    Прежняя локальная эвристика (по непрерывности пикселей внутри маленького
    padded-клипа) остаётся как ДОПОЛНИТЕЛЬНАЯ подстраховка (объединяется по
    ИЛИ) - на случай отсутствия SciPy или очень коротких линий/засечек.

    Возвращает (fitz.Pixmap | None, fitz.Rect | None): (заплатка размером с
    ЭФФЕКТИВНЫЙ rect в px при заданном dpi, эффективный rect в pt, RAW-
    пространство). Если построить заплатку невозможно — (None, None), и
    вызывающий код откатывается на старый sample_bg_color (поведение не
    хуже прежнего). Сама ничего на страницу не рисует — только строит патч.
    """
    try:
        if raw_w is None or raw_h is None:
            rotation, raw_w, raw_h = get_page_rotation_and_raw_size(page)

        # ---- Приводим ВСЁ к DISPLAY-пространству (см. docstring выше) ----
        raw_rect_in = (rect.x0, rect.y0, rect.x1, rect.y1)
        disp_bbox, _, _ = raw_to_display_bbox(raw_rect_in, rotation, raw_w, raw_h)
        rect = fitz.Rect(*disp_bbox)

        neighbor_seg_disp = None
        if neighbor_top_y is not None:
            # Вырожденный "сегмент" (горизонтальная линия в RAW-пространстве)
            # на высоте neighbor_top_y, шириной в x-диапазон целевого rect'а
            # (в RAW, т.е. raw_rect_in). Переводим ЕГО ЖЕ через ту же функцию
            # raw_to_display_bbox - какой бы ни была /Rotate, получим корректный
            # сегмент уже в display-пространстве (см. _neighbor_edge_clamp).
            nx0, nx1 = raw_rect_in[0], raw_rect_in[2]
            neighbor_seg_disp, _, _ = raw_to_display_bbox(
                (nx0, neighbor_top_y, nx1, neighbor_top_y), rotation, raw_w, raw_h
            )

        pad = max(6.0, min(rect.width, rect.height) * 0.35)
        padded = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
        padded &= page.rect
        if padded.is_empty or padded.width < 1 or padded.height < 1:
            return None, None

        pix = page.get_pixmap(clip=padded, dpi=dpi)
        if pix.width < 3 or pix.height < 3:
            return None, None

        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )[:, :, :3].astype(np.float32)
        H, W = arr.shape[:2]

        sx = pix.width / padded.width
        sy = pix.height / padded.height
        bx0 = int(round((rect.x0 - padded.x0) * sx))
        by0 = int(round((rect.y0 - padded.y0) * sy))
        bx1 = int(round((rect.x1 - padded.x0) * sx))
        by1 = int(round((rect.y1 - padded.y0) * sy))
        bx0, bx1 = max(0, bx0), min(W, bx1)
        by0, by1 = max(0, by0), min(H, by1)
        if bx1 <= bx0 or by1 <= by0:
            return None, None

        # ---- Глобальная карта линий (см. table_grid_protect.py), обрезанная
        # под текущий padded-клип. Используется и для защиты от "экспансии"
        # erase-бокса на линию (safe_out ниже), и позже - для финального
        # "keep" (какие пиксели патча вернуть как есть, не интерполируя). ----
        grid_full = None
        if grid_mask is not None:
            try:
                gcrop = grid_mask.crop(padded)
                if gcrop.size and gcrop.shape != (H, W):
                    from PIL import Image as _PILImg
                    gm_img = _PILImg.fromarray((gcrop.astype(np.uint8) * 255))
                    gm_img = gm_img.resize((W, H), _PILImg.NEAREST)
                    gcrop = np.asarray(gm_img) > 0
                if gcrop.size:
                    grid_full = gcrop
            except Exception:  # noqa: BLE001
                grid_full = None

        # --- хроматические пиксели (синие/цветные печати, подписи, цветные
        # линии): устойчивые признаки настоящей цветной заливки. Простого
        # sat > порога недостаточно: у ореолов антиалиасинга чёрных букв на
        # скане тоже ненулевой sat И разница каналов (диагностика БИК:
        # diff_rgb ореолов 3..47, у настоящих печатей 60..178). Поэтому
        # chroma_mask требует ТРИ условия:
        #   1) diff_rgb = max(RGB)-min(RGB) >= 50  (отсекает антиалиасинг);
        #   2) sat = diff_rgb/max(RGB) >= 0.20    (порог из ТЗ §8);
        #   3) локальная плотность хроматического соседства >= 0.15 в окне
        #      5x5: одиночные изолированные пиксели ореолов (дробь в БИК)
        #      НЕ считаются печатью/подписью.
        # Итог: светлый антиалиасинг букв НЕ возвращается в patch ни через
        # chroma_mask, ни через line_mask; реальные печати сохраняются.
        _r = arr[..., 0]
        _g = arr[..., 1]
        _b = arr[..., 2]
        _mx = np.maximum(np.maximum(_r, _g), _b)
        _mn = np.minimum(np.minimum(_r, _g), _b)
        sat = (_mx - _mn) / (_mx + 1e-5)
        diff_rgb = _mx - _mn
        _strong = (diff_rgb >= 50.0) & (sat >= 0.20)
        if _strong.any():
            # доля "сильных" хроматических пикселей в окне 5x5 вокруг каждого
            _sum = _strong.astype(np.float32)
            _f = np.zeros_like(_sum)
            k = 2
            _padded_s = np.pad(_sum, k, mode="constant")
            _padded_f = np.pad(np.ones_like(_sum), k, mode="constant")
            for _dy in range(-k, k + 1):
                for _dx in range(-k, k + 1):
                    _f += _padded_s[2 + _dy:2 + _dy + H, 2 + _dx:2 + _dx + W]
            _fc = _padded_f[2:2 + H, 2:2 + W]
            strong_density = _f / np.maximum(_fc, 1)
        else:
            strong_density = np.zeros_like(sat)
        chroma_mask = _strong & (strong_density >= 0.15)

        brightness = arr.mean(axis=2)
        dark_mask = brightness < 190
        # обычный "текст"/тёмные нецветные пиксели (их удаляем, не считаем фоном)
        ink_mask = dark_mask & ~chroma_mask
        # фон-кандидат для оценки кольца (светлая нецветная бумага)
        bg_mask = ~dark_mask & ~chroma_mask
        default_c = arr.reshape(-1, 3).mean(axis=0)

        def edge_stats(y0, y1, x0, x1):
            y0, y1 = max(0, y0), min(H, y1)
            x0, x1 = max(0, x0), min(W, x1)
            if y1 <= y0 or x1 <= x0:
                return default_c, 0.0
            region = arr[y0:y1, x0:x1]
            m = bg_mask[y0:y1, x0:x1]
            if int(m.sum()) < 4:
                return default_c, 0.0
            vals = region[m]
            return vals.mean(axis=0), float(vals.std())

        # --- точечное расширение erase-rect по X (влево/вправо) для хвостов букв
        dilate_px = 2
        # Ничего не расширяем поверх пикселей, распознанных глобальной картой
        # линий (grid_full) - экспансия НИКОГДА не должна наезжать на реальную
        # границу ячейки, даже если локальная непрерывность (ink_mask) её не
        # разглядела (перекос скана / светло-серая линия).
        safe_out = bg_mask | chroma_mask
        if grid_full is not None:
            safe_out = safe_out & ~grid_full

        # Защита от наезда на вертикальные линии таблиц: если на границе есть протяженная темная полоса,
        # расширяться в эту сторону категорически нельзя
        left_border_dark_ratio = float(ink_mask[by0:by1, max(0, bx0 - 1):bx0 + 1].mean()) if by1 > by0 else 0.0
        right_border_dark_ratio = float(ink_mask[by0:by1, bx1 - 1:min(W, bx1 + 1)].mean()) if by1 > by0 else 0.0

        expand_l = (
            left_border_dark_ratio < 0.45
            and bx0 - dilate_px >= 0
            and bool(ink_mask[by0:by1, bx0:bx0 + dilate_px].any())
            and bool(np.all(safe_out[by0:by1, bx0 - dilate_px:bx0]))
        )
        expand_r = (
            right_border_dark_ratio < 0.45
            and bx1 + dilate_px <= W
            and bool(ink_mask[by0:by1, bx1 - dilate_px:bx1].any())
            and bool(np.all(safe_out[by0:by1, bx1:bx1 + dilate_px]))
        )
        ex_pt = dilate_px / sx
        eff_x0 = rect.x0 - ex_pt if expand_l else rect.x0
        eff_x1 = rect.x1 + ex_pt if expand_r else rect.x1

        # Верх не трогает шапку таблицы, низ берем строго до границы (без наезда на рамку)
        eff_y0 = max(0.0, rect.y0 - 0.5)
        eff_y1 = min(page.rect.height, rect.y1 + 1.2)

        # Клапан соседа: rotation-agnostic (см. _neighbor_edge_clamp) - пришёл
        # на смену прежней проверке, работавшей только для соседа "сверху" в
        # RAW-координатах (что для /Rotate 90/270 могло означать не Y, а X).
        if neighbor_seg_disp is not None:
            eff_x0, eff_y0, eff_x1, eff_y1 = _neighbor_edge_clamp(
                (eff_x0, eff_y0, eff_x1, eff_y1),
                (rect.x0, rect.y0, rect.x1, rect.y1),
                neighbor_seg_disp,
            )

        eff_rect = fitz.Rect(eff_x0, eff_y0, eff_x1, eff_y1) & page.rect
        if eff_rect.is_empty or eff_rect.width < 1 or eff_rect.height < 1:
            eff_rect = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1)

        # пересчёт пиксельных границ по эффективному rect: патч должен ТОЧНО
        # покрывать область, которую сотрёт redaction.
        bx0 = int(round((eff_rect.x0 - padded.x0) * sx))
        by0 = int(round((eff_rect.y0 - padded.y0) * sy))
        bx1 = int(round((eff_rect.x1 - padded.x0) * sx))
        by1 = int(round((eff_rect.y1 - padded.y0) * sy))
        bx0, bx1 = max(0, bx0), min(W, bx1)
        by0, by1 = max(0, by0), min(H, by1)
        if bx1 <= bx0 or by1 <= by0:
            return None, None

        ring = 10
        top_c, top_s = edge_stats(by0 - ring, by0, bx0, bx1)
        bot_c, bot_s = edge_stats(by1, by1 + ring, bx0, bx1)
        left_c, left_s = edge_stats(by0, by1, bx0 - ring, bx0)
        right_c, right_s = edge_stats(by0, by1, bx1, bx1 + ring)

        bw, bh = bx1 - bx0, by1 - by0
        yy, xx = np.mgrid[0:bh, 0:bw].astype(np.float32)
        tx = (xx / max(1, bw - 1))[..., None]
        ty = (yy / max(1, bh - 1))[..., None]

        vert = top_c[None, None, :] * (1 - ty) + bot_c[None, None, :] * ty
        horiz = left_c[None, None, :] * (1 - tx) + right_c[None, None, :] * tx
        patch = 0.5 * vert + 0.5 * horiz

        # --- АДАПТИВНЫЙ шум (ТЗ, Цель №1): не добавляется безусловно.
        # Оценка локального разброса по ТЕКУЩЕМУ растру (не фиксированный
        # порог на DPI): медиана std краёв. Однородный фон -> шум = 0; иначе —
        # слабый шум, не превосходящий реальный разброс (максимум 8 градаций).
        edge_stds = np.array([top_s, bot_s, left_s, right_s], dtype=np.float64)
        bg_std = float(np.median(edge_stds))
        if bg_std >= 0.8:
            noise_std = float(min(bg_std, 8.0))
            rng = np.random.default_rng(int(abs(rect.x0) * 7 + abs(rect.y0) * 13))
            patch = patch + rng.normal(0, noise_std, patch.shape[:2])[..., None]
        patch = np.clip(patch, 0, 255).astype(np.uint8)

        # --- Защита структурных линий таблиц, перекрестий и углов ---
        # ОСНОВНАЯ защита - глобальная grid_full (см. table_grid_protect.py):
        # устойчива к перекосу скана, светло-серым линиям и корректно
        # покрывает Т-образные стыки/перекрестия/углы, т.к. считается на
        # ВСЮ страницу целиком, а не в маленьком локальном окне.
        # ДОПОЛНИТЕЛЬНО (объединяется по ИЛИ) оставлена прежняя локальная
        # эвристика непрерывности пикселей - подстраховка на случай, если
        # SciPy недоступен (grid_mask=None) или отрезок короче порога
        # table_grid_protect.MIN_LINE_LEN_PT (напр. короткая засечка).
        # Увеличиваем порог выхода наружу и повышаем требование к заполнению (отсекаем десцендеры букв)
        rect_ink = ink_mask[by0:by1, bx0:bx1]
        line_mask = np.zeros_like(rect_ink, dtype=bool)

        # Защищаем исключительно глобально подтвержденную сетку бланка/таблицы.
        # Локальный попиксельный цикл исключен: он ошибочно считал хвостики букв (д, р, у, ц)
        # вертикальными линиями таблицы и переносил их обратно в скан.
        if grid_full is not None:
            line_mask = grid_full[by0:by1, bx0:bx1].copy()
        else:
            line_mask = np.zeros_like(rect_ink, dtype=bool)

        # Исключаем изолированные точки и хвостики букв:
        # Линия таблицы ОБЯЗАНА быть непрерывной полосой толщиной не менее 3 пикселей в маске
        if line_mask.any():
            from scipy import ndimage
            # Очищаем маску линий от тонких одиночных штрихов букв
            struct = np.ones((2, 2), dtype=bool)
            clean_line_mask = ndimage.binary_opening(line_mask, structure=struct)
        else:
            clean_line_mask = line_mask

        # Возвращаем пиксели реального растра ТОЛЬКО для чистой сетки и цветных печатей
        keep = (clean_line_mask & ink_mask[by0:by1, bx0:bx1]) | chroma_mask[by0:by1, bx0:bx1]
        if keep.any():
            patch[keep] = arr[by0:by1, bx0:bx1][keep]

        patch = np.ascontiguousarray(patch)

        patch_pixmap = fitz.Pixmap(fitz.csRGB, patch.shape[1], patch.shape[0], patch.tobytes(), False)

        # ---- Конвертация результата обратно в RAW-пространство ----
        # Всё выше считалось в DISPLAY-пространстве (см. docstring). Но
        # вызывающий код (apply_pdf_modifications) использует eff_rect для
        # add_redact_annot()/insert_image(), которые работают в RAW-
        # пространстве (см. pdf_geometry.py) - конвертируем один раз, здесь.
        raw_eff = display_to_raw_bbox(
            (eff_rect.x0, eff_rect.y0, eff_rect.x1, eff_rect.y1), rotation, raw_w, raw_h
        )
        eff_rect_raw = fitz.Rect(*raw_eff)

        return patch_pixmap, eff_rect_raw
    except Exception:
        logger.warning("[BG] restore_local_background failed, falling back to flat fill", exc_info=True)
        return None, None


def _capture_ink_region(page, bbox_disp):
    """Снимает растр региона вокруг bbox исходного текста (display-координаты)
    ДО page.apply_redactions(), пока оригинальные пиксели ещё на скане.

    Возвращает (region_rgb_np, region_rect) либо (None, None). region_rgb_np —
    (H,W,3) uint8, region_rect — (x0,y0,x1,y1) региона в pt. Контекст передаётся
    в render_text_patch(..., ink_context=...) для автоподстройки цвета чернил
    (Задача: Ink Sampling ДО удаления текста).
    """
    try:
        pad = INK_SAMPLE_PAD_PT
        x0, y0, x1, y1 = [float(v) for v in bbox_disp]
        rect = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        rect &= page.rect
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            return None, None
        pix = page.get_pixmap(clip=rect, dpi=INK_SAMPLE_DPI)
        if pix.width < 3 or pix.height < 3:
            return None, None
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )[:, :, :3].copy()
        return arr, (rect.x0, rect.y0, rect.x1, rect.y1)
    except Exception:  # noqa: BLE001
        return None, None


def apply_pdf_modifications(
    doc: fitz.Document,
    modifications: dict,
    mode: str = "native",
    page_blocks: dict = None,
) -> None:
    for page_str, page_mods in (modifications or {}).items():
        try:
            page_idx = int(page_str) - 1
        except ValueError:
            continue

        if page_idx < 0 or page_idx >= len(doc):
            continue

        if isinstance(page_mods, dict):
            page_mods = list(page_mods.values())

        if not isinstance(page_mods, (list, tuple)):
            continue

        page = doc[page_idx]

        # ВАЖНО: bbox, приходящие с фронтенда (mod["origBbox"], mod["bbox"]),
        # находятся в DISPLAY-пространстве - том же, что page.rect и то, что
        # реально нарисовано в редакторе (см. pdf_geometry.py). А вот
        # add_redact_annot()/insert_text() работают в RAW-пространстве
        # (без учёта /Rotate). Для страниц без поворота эти пространства
        # совпадают, для повёрнутых (типичный альбомный скан) - нет, и без
        # этого преобразования текст вставлялся бы в другое место и/или
        # оказывался повёрнутым на 90/180/270°.
        rotation, raw_w, raw_h = get_page_rotation_and_raw_size(page)

        # Глобальная (на всю страницу) карта пикселей линий таблицы/бланка
        # (см. table_grid_protect.py) - считается ОДИН РАЗ на страницу, ДО
        # каких-либо правок, и передаётся во все вызовы restore_local_background
        # ниже. Используется в ОБОИХ режимах (native и ocr) - restore_local_background
        # вызывается для любого удаляемого текстового блока независимо от
        # источника страницы (см. цикл "1 этап" ниже), поэтому и native-режим
        # с векторными таблицами получает ту же защиту границ ячеек.
        # Если SciPy недоступен или построение не удалось - grid_mask=None,
        # и restore_local_background мягко деградирует к прежней локальной
        # эвристике (поведение не хуже, чем было раньше).
        grid_mask = None
        try:
            grid_mask = build_grid_line_mask(page, dpi=GRID_MASK_DPI)
        except Exception:  # noqa: BLE001
            logger.warning("[GRID] не удалось построить карту линий страницы", exc_info=True)
            grid_mask = None

        # Геометрия соседних OCR-блоков этой страницы, переведённая в ТУ ЖЕ
        # (raw/mixed) систему координат, что и `rect` ниже (display_to_raw_bbox),
        # чтобы _nearest_neighbor_top_y() сравнивала сопоставимые величины.
        # Только для OCR-режима - в native-режиме соседних OCR-блоков нет
        # (там своя, векторная, геометрия текста), поэтому neighbor_top_y там
        # всегда None; restore_local_background при этом вызывается всё
        # равно (защита линий сетки нужна и в native-режиме).
        page_blocks_raw = None
        if mode == "ocr" and page_blocks:
            src_blocks = page_blocks.get(page_str) or []
            page_blocks_raw = []
            for b in src_blocks:
                items_to_add = [b]
                # Добавляем все строки блока для поштучной защиты
                for ln in ((b or {}).get("lines") or []):
                    items_to_add.append(ln)

                for item in items_to_add:
                    bbox = (item or {}).get("bbox") or {}
                    try:
                        bx0 = float(bbox["x1"])
                        by0 = float(bbox["y1"])
                        bx1 = float(bbox["x2"])
                        by1 = float(bbox["y2"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    rbx0, rby0, rbx1, rby1 = display_to_raw_bbox((bx0, by0, bx1, by1), rotation, raw_w, raw_h)
                    page_blocks_raw.append({
                        "id": item.get("id"),
                        "bbox": {
                            "x1": min(rbx0, rbx1), "y1": min(rby0, rby1),
                            "x2": max(rbx0, rbx1), "y2": max(rby0, rby1),
                        },
                    })

        # 1 этап: удаляем оригинальный текст (Redaction)
        #
        # Фон под удаляемым текстом восстанавливается ЛОКАЛЬНО
        # (restore_local_background, см. выше) - билинейная интерполяция
        # цвета/яркости от краёв bbox внутрь + адаптивный шум по фактическому
        # разбросу фона (на однородном фоне шума нет), вместо одной плоской
        # заливки. Патчи строятся ДО apply_redactions() (нужны исходные, ещё
        # не стёртые пиксели), а рисуются НА страницу ПОСЛЕ - иначе
        # add_redact_annot их же и сотрёт.
        pending_patches = []  # [(rect, fitz.Pixmap)]
        # Задача (Ink Sampling): снимаем регион оригинальных чернил ДО
        # apply_redactions() (когда текст ещё на скане) — только OCR-режим.
        ink_contexts = {}  # key -> (region_rgb_np, region_rect, bbox_disp)
        if mode == "ocr":
            for mod in page_mods:
                if not mod.get("isNew") and mod.get("origBbox"):
                    arr, rrect = _capture_ink_region(page, mod["origBbox"])
                    if arr is not None:
                        obb = [float(v) for v in mod["origBbox"]]
                        ink_contexts[("bbox", tuple(round(v, 1) for v in obb))] = (arr, rrect, obb)
                        if mod.get("id"):
                            ink_contexts[("id", str(mod["id"]))] = (arr, rrect, obb)

        # Собираем все области удаления и объединяем смежные/пересекающиеся
        raw_erase_rects = []
        for mod in page_mods:
            if not mod.get("isNew") and mod.get("origBbox"):
                # Если текст очищен или изменен — область подлежит очистке
                x0, y0, x1, y1 = display_to_raw_bbox(mod["origBbox"], rotation, raw_w, raw_h)
                raw_erase_rects.append(fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))

        # Объединяем близкие строки СТРОГО по вертикали (Y), НЕ расширяя по горизонтали (X),
        # чтобы гарантированно не срезать боковые вертикальные разделители колонок таблицы!
        merged_rects = []
        for r in raw_erase_rects:
            merged = False
            for idx, mr in enumerate(merged_rects):
                # Пересечение по вертикали с запасом 4 pt и совпадение по X-колонке
                x_overlap = min(mr.x1, r.x1) - max(mr.x0, r.x0)
                y_dist = max(0.0, max(mr.y0, r.y0) - min(mr.y1, r.y1))
                if x_overlap > 20.0 and y_dist <= 5.0:
                    # X берем с безопасным отступом внутрь, Y объединяем
                    merged_rects[idx] = fitz.Rect(min(mr.x0, r.x0), min(mr.y0, r.y0), max(mr.x1, r.x1), max(mr.y1, r.y1))
                    merged = True
                    break
            if not merged:
                merged_rects.append(r)

        for rect in merged_rects:
            neighbor_top_y = None
            if page_blocks_raw:
                neighbor_top_y = _nearest_neighbor_top_y(page_blocks_raw, rect)
            patch_pix, eff_rect = restore_local_background(
                page, rect, neighbor_top_y=neighbor_top_y,
                rotation=rotation, raw_w=raw_w, raw_h=raw_h, grid_mask=grid_mask,
            )
            if patch_pix is not None and eff_rect is not None:
                page.add_redact_annot(eff_rect)
                pending_patches.append((eff_rect, patch_pix))
            else:
                bg_color = sample_bg_color(page, (rect.x0, rect.y0, rect.x1, rect.y1))
                page.add_redact_annot(rect, fill=bg_color)

        page.apply_redactions()

        # Полностью подчищаем объекты Redaction, чтобы браузер не рисовал их красные рамки
        for annot in list(page.annots()):
            if annot.type[0] == 12:  # 12 = Redaction
                page.delete_annot(annot)

        for rect, patch_pix in pending_patches:
            page.insert_image(rect, pixmap=patch_pix)

        # 2 этап: отрисовываем прямые линии (дорисовка таблиц/границ)
        for mod in page_mods:
            if mod.get("type") == "line" and mod.get("p1") and mod.get("p2"):
                p1_disp = mod["p1"]
                p2_disp = mod["p2"]

                color_hex = (mod.get("color") or "#000000").lstrip("#").lower()
                if len(color_hex) == 3:
                    color_hex = "".join(c * 2 for c in color_hex)
                elif len(color_hex) != 6:
                    color_hex = "000000"

                # Исключаем попадание системных/отладочных красных линий (базовые линии Paddle/VLM)
                if color_hex in ("ff0000", "f00", "e11d48", "ef4444") and not mod.get("isUserCreated"):
                    continue

                # Отсекаем вырожденные микро-отрезки
                if abs(p1_disp[0] - p2_disp[0]) < 1.0 and abs(p1_disp[1] - p2_disp[1]) < 1.0:
                    continue

                # Переводим координаты линии из display в raw-пространство
                p1_raw = display_to_raw_point(p1_disp[0], p1_disp[1], rotation, raw_w, raw_h)
                p2_raw = display_to_raw_point(p2_disp[0], p2_disp[1], rotation, raw_w, raw_h)

                try:
                    r = int(color_hex[0:2], 16) / 255.0
                    g = int(color_hex[2:4], 16) / 255.0
                    b = int(color_hex[4:6], 16) / 255.0
                    line_color = (r, g, b)
                except Exception:
                    line_color = (0, 0, 0)

                line_width = float(mod.get("width") or 1.0)
                page.draw_line(fitz.Point(p1_raw), fitz.Point(p2_raw), color=line_color, width=line_width)

        # 3 этап: вставляем новый / измененный текст
        for mod in page_mods:
            if mod.get("type") == "line":
                continue
            text = (mod.get("text") or "").strip()
            if not text:
                continue

            # h (высота бокса, для расчёта кегля) считаем в DISPLAY-пространстве -
            # это то, как блок реально выглядит на экране/странице.
            dx0, dy0, dx1, dy1 = mod["bbox"]
            h = abs(dy1 - dy0)

            x0, y0, x1, y1 = display_to_raw_bbox(mod["bbox"], rotation, raw_w, raw_h)

            raw_fs = mod.get("fontSize")
            if raw_fs is not None and float(raw_fs) > 0:
                font_size = float(raw_fs)
            else:
                font_size = max(6.0, round(h * 0.75, 1))
            # Гарнитура: приоритет пользовательского/присланного фронтендом семейства
            # (уже отражает авто-подбор из ocr_typography / FontMeta.family), иначе —
            # конфигурируемый дефолт (НЕ безусловный Times New Roman, см. ТЗ 3.1).
            req_family = mod.get("fontFamily") or OCR_TEXT_DEFAULT_FAMILY
            is_b = bool(mod.get("isBold"))
            is_i = bool(mod.get("isItalic"))
            font_file = resolve_system_font(req_family, is_b, is_i)

            # Горизонтальный масштаб/поправка baseline из авто-подобранного
            # стиля OCR (см. ocr_typography.py). По умолчанию нейтральны.
            raw_sx = mod.get("scaleX")
            scale_x = float(raw_sx) if raw_sx else 1.0
            raw_bo = mod.get("baselineOffset")
            baseline_offset = float(raw_bo) if raw_bo is not None else 0.0

            font_key = f"F_{abs(hash(font_file or req_family)) % 100000}"
            if font_file:
                page.insert_font(fontname=font_key, fontfile=font_file)
                font_name = font_key
            else:
                font_name = "tiro" if "times" in req_family.lower() else "helv"

            # Точная базовая линия текста: центрирование по высоте бокса
            # Эквивалентно вертикальному выравниванию в браузере
            box_mid_y = (dy0 + dy1) / 2.0
            # Оптическая поправка: базовая линия проходит ниже центра на ~35% кегля шрифта
            base_disp_x = dx0
            base_disp_y = box_mid_y + (font_size * 0.35) + baseline_offset
            bx, by = display_to_raw_point(base_disp_x, base_disp_y, rotation, raw_w, raw_h)
            point = fitz.Point(bx, by)

            # Горизонтальный масштаб scale_x - через morph (TextHScale), без
            # вброса вертикального сдвига. Нейтрален при scale_x == 1.0.
            morph = None
            if abs(scale_x - 1.0) > 0.005:
                morph = (point, fitz.Matrix(scale_x, 0, 0, 1, 0, 0))

            # ---- Задача 3: визуальная деградация нового текста (ТОЛЬКО OCR) ----
            # Sandwich-паттерн: растровый деградированный патч (insert_image) +
            # невидимый текстовый слой (render_mode=3) для поиска/выделения/экспорта.
            # Цвет чернил берётся из Ink Sampling (контекст региона, снятый ДО
            # apply_redactions — см. ink_contexts выше).
            degraded_ok = False
            if mode == "ocr" and ENABLE_OCR_TEXT_NOISE and font_file:
                try:
                    ink_ctx = None
                    key = ("id", str(mod["id"])) if mod.get("id") else None
                    if key is None or key not in ink_contexts:
                        src_bb = mod.get("origBbox") or mod.get("bbox")
                        if src_bb:
                            key = ("bbox", tuple(round(float(v), 1) for v in src_bb))
                    entry = ink_contexts.get(key) if key else None
                    if entry is not None:
                        arr, rrect, obb = entry
                        ink_ctx = {"region_rgb": arr, "region_rect": rrect, "bbox_disp": obb}

                    png_bytes, geom = render_text_patch(
                        text, font_file, font_size, scale_x, baseline_offset,
                        dx0=dx0, dy0=dy0, ink_context=ink_ctx,
                    )
                    if png_bytes is not None and geom and geom["w_pt"] > 0.1 and geom["h_pt"] > 0.1:
                        disp_rect = fitz.Rect(
                            geom["x"], geom["y"],
                            geom["x"] + geom["w_pt"], geom["y"] + geom["h_pt"],
                        )
                        rx0, ry0, rx1, ry1 = display_to_raw_bbox(
                            (disp_rect.x0, disp_rect.y0, disp_rect.x1, disp_rect.y1),
                            rotation, raw_w, raw_h,
                        )
                        raw_rect = fitz.Rect(rx0, ry0, rx1, ry1)
                        page.insert_image(raw_rect, stream=png_bytes, rotate=rotation,
                                          keep_proportion=False)
                        page.insert_text(
                            point, text,
                            fontsize=font_size, fontname=font_name,
                            color=(0, 0, 0), rotate=rotation, morph=morph,
                            render_mode=3,
                        )
                        degraded_ok = True
                except Exception:  # noqa: BLE001
                    logger.warning("[OCR] деградация текста не удалась, fallback на векторный текст")
                    degraded_ok = False

            if degraded_ok:
                continue

            # ---- Native / fallback: обычный видимый векторный текст ----
            try:
                page.insert_text(
                    point,
                    text,
                    fontsize=font_size,
                    fontname=font_name,
                    color=(0, 0, 0),
                    rotate=rotation,
                    morph=morph,
                )
            except Exception:
                rect = fitz.Rect(x0, y0, x0 + max(200.0, (x1 - x0) * 1.5), y1 + 15)
                page.insert_textbox(
                    rect,
                    text,
                    fontsize=font_size,
                    fontname=font_name,
                    color=(0, 0, 0),
                    rotate=rotation,
                    morph=morph,
                )


@document_bp.route("/upload", methods=["POST"])
def upload_pdf():
    file = request.files.get("file")
    mode = (request.form.get("mode") or "").strip().lower()

    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Пожалуйста, загрузите файл в формате PDF."}), 400

    if mode not in VALID_MODES:
        return jsonify({"error": "Не указан или некорректен режим (mode: native|ocr)."}), 400

    clear_caches()

    pdf_bytes = file.read()
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = len(doc)
        doc.close()
    except Exception as exc:
        return jsonify({"error": f"Не удалось открыть PDF: {exc}"}), 400

    job = job_manager.create_job(
        filename=file.filename,
        pdf_bytes=pdf_bytes,
        page_count=page_count,
        mode=mode,
    )
    return jsonify({"job_id": job.job_id, "page_count": page_count, "mode": mode})

@document_bp.route("/<job_id>/progress", methods=["GET"])
def get_progress(job_id):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify(job.progress)


@document_bp.route("/<job_id>/page/<int:page_num>", methods=["GET"])
def get_page(job_id, page_num):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    if page_num < 1 or page_num > job.page_count:
        return jsonify({"error": f"Страница {page_num} вне диапазона (1..{job.page_count})"}), 400

    if page_num in job.pages:
        return jsonify(job.pages[page_num])

    def on_progress(percent, step):
        job.set_progress(percent, step)

    mode = job.mode
    try:
        # Передаем temp_pdf_path вместо копирования всего массива байтов в память
        pdf_source = job.temp_pdf_path if os.path.exists(job.temp_pdf_path) else job.pdf_bytes
        if mode == "native":
            result = process_native_page(
                pdf_source=pdf_source,
                page_number=page_num,
                job_id=job_id,
                on_progress=on_progress,
            )
        else:
            result = process_single_page(
                pdf_source=pdf_source,
                page_number=page_num,
                job_id=job_id,
                on_progress=on_progress,
            )
            result.setdefault("source_type", "ocr")
    except OcrPipelineError as exc:
        job.set_progress(0, "error")
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("[PDF] unexpected error")
        job.set_progress(0, "error")
        return jsonify({"error": f"Внутренняя ошибка обработки: {exc}"}), 500

    result["mode"] = mode
    result["image_url"] = f"/api/document/{job_id}/render/{page_num}"
    job.pages[page_num] = result
    return jsonify(result)


@document_bp.route("/<job_id>/render/<int:page_num>", methods=["GET"])
def get_rendered_image(job_id, page_num):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    # Открываем напрямую с диска без создания буфера в RAM
    try:
        if os.path.exists(job.temp_pdf_path):
            doc = fitz.open(job.temp_pdf_path)
        else:
            doc = fitz.open(stream=job.pdf_bytes, filetype="pdf")

        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=150, alpha=False)
        img_bytes = pix.tobytes("png")
        doc.close()
        return send_file(io.BytesIO(img_bytes), mimetype="image/png")
    except Exception as exc:
        return jsonify({"error": f"Не удалось отрендерить страницу: {exc}"}), 500


@document_bp.route("/<job_id>/file", methods=["GET"])
def get_pdf_file(job_id):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "PDF-файл не найден"}), 404

    if os.path.exists(job.temp_pdf_path):
        return send_file(
            job.temp_pdf_path,
            mimetype="application/pdf",
            as_attachment=False,
            download_name=job.filename or "document.pdf",
        )

    return send_file(
        io.BytesIO(job.pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=job.filename or "document.pdf",
    )


@document_bp.route("/<job_id>/searchable/<int:page_num>", methods=["GET"])
def get_searchable_pdf(job_id, page_num):
    filename = f"{job_id}_p{page_num}_searchable.pdf"
    file_path = os.path.join(DEBUG_SEARCHABLE_DIR, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "Searchable PDF еще не сформирован"}), 404
    return send_file(file_path, mimetype="application/pdf")

@document_bp.route("/<job_id>/export/docx", methods=["POST"])
def export_docx(job_id):
    """
    Экспорт в Word:
    - OCR-режим: применяет правки пользователя к PDF -> LightOnOCR + Qwen (9B) -> docx_renderer
    - Native-режим: PyMuPDF textual layer -> docx_export
    """
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    body = request.get_json(silent=True) or {}
    modifications = body.get("modifications", {})
    mode = job["mode"]
    base_name = os.path.splitext(job.get("filename", "document.pdf"))[0]

    # ==================== OCR-РЕЖИМ (LightOn + Qwen 3.5 9B) ====================
    if mode == "ocr":
        def on_vlm_progress(percent, step):
            job["progress"] = {"percent": percent, "step": step}

        src_doc = fitz.open(stream=job["pdf_bytes"], filetype="pdf")
        try:
            # Геометрия уже открытых (просмотренных пользователем) страниц —
            # нужна restore_local_background для защиты соседних строк
            # (см. _nearest_neighbor_top_y). Для страниц, которые пользователь
            # не открывал в редакторе, геометрии не будет — тогда поведение
            # как раньше, без защиты соседей (не хуже прежнего).
            page_blocks_map = {
                str(pnum): pdata.get("blocks")
                for pnum, pdata in (job.get("pages") or {}).items()
                if pdata.get("blocks")
            }
            # Накладываем правки пользователя на PDF перед отправкой в VLM
            apply_pdf_modifications(src_doc, modifications, mode="ocr", page_blocks=page_blocks_map)

            docx_stream = export_modified_pdf_to_docx_vlm(
                modified_doc=src_doc,
                doc_title=base_name,
                on_progress=on_vlm_progress
            )
            return send_file(
                docx_stream,
                as_attachment=True,
                download_name=f"{base_name}.docx",
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        except Exception as exc:
            logger.exception(f"[DOCX_VLM] Ошибка экспорта через LightOn+Qwen: {exc}")
            return jsonify({"error": f"Не удалось создать DOCX: {exc}"}), 500
        finally:
            src_doc.close()

    # ==================== NATIVE-РЕЖИМ (Обычный PDF) ====================
    from native_pipeline import extract_native_page_from_doc
    import json

    src_doc = fitz.open(stream=job["pdf_bytes"], filetype="pdf")
    try:
        # Быстро накладываем правки в памяти
        apply_pdf_modifications(src_doc, modifications, mode=job["mode"])

        pages_payload = []
        for page_num in range(1, src_doc.page_count + 1):
            udm_page = extract_native_page_from_doc(src_doc, page_num)
            page_dict = json.loads(udm_page.model_dump_json())
            pages_payload.append(page_dict)

        docx_stream = build_docx_from_udm_pages(pages_payload)
    except Exception as exc:
        logger.exception(f"[DOCX] ошибка сборки документа: {exc}")
        return jsonify({"error": f"Не удалось создать DOCX: {exc}"}), 500
    finally:
        src_doc.close()

    return send_file(
        docx_stream,
        as_attachment=True,
        download_name=f"{base_name}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

@document_bp.route("/<job_id>/save", methods=["POST"])
def save_pdf(job_id):
    """Сохранение отредактированного PDF-файла: как скан (растр) или с текстом."""
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Исходный PDF-файл не найден"}), 404

    data = request.get_json(silent=True) or {}
    modifications = data.get("modifications", {})
    export_mode = data.get("export_mode", "searchable").lower()

    try:
        doc = fitz.open(stream=job["pdf_bytes"], filetype="pdf")
        page_blocks_map = None
        if job["mode"] == "ocr":
            page_blocks_map = {
                str(pnum): pdata.get("blocks")
                for pnum, pdata in (job.get("pages") or {}).items()
                if pdata.get("blocks")
            }
        apply_pdf_modifications(doc, modifications, mode=job["mode"], page_blocks=page_blocks_map)

        # Режим запекания в скан: рендерим каждую страницу в растр без текстовых слоев
        if export_mode == "raster":
            raster_doc = fitz.open()
            for page in doc:
                # Удаляем любые полигональные аннотации/рамки распознавания перед запеканием растра
                for annot in list(page.annots()):
                    page.delete_annot(annot)
                
                # Рендерим чистый скан страницы без системных обводок
                pix = page.get_pixmap(dpi=200, alpha=False, annots=False)
                new_page = raster_doc.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(new_page.rect, pixmap=pix)
            doc.close()
            target_doc = raster_doc
            out_filename = f"scan_{job.get('filename', 'document.pdf')}"
        else:
            # Очищаем документ от абсолютно всех служебных аннотаций и рамок Redaction
            for page in doc:
                for annot in list(page.annots()):
                    page.delete_annot(annot)

            target_doc = doc
            out_filename = f"edited_{job.get('filename', 'document.pdf')}"

        output_stream = io.BytesIO()
        target_doc.save(output_stream, garbage=4, deflate=True)
        target_doc.close()
        output_stream.seek(0)

        return send_file(
            output_stream,
            as_attachment=True,
            download_name=out_filename,
            mimetype="application/pdf"
        )
    except Exception as exc:
        logger.exception(f"[PDF] Ошибка сохранения: {exc}")
        return jsonify({"error": f"Не удалось сохранить PDF: {exc}"}), 500

@document_bp.route("/<job_id>", methods=["DELETE"])
def close_job(job_id):
    """Явное закрытие сессии пользователем (освобождает временный файл и память)."""
    success = job_manager.delete_job(job_id)
    return jsonify({"success": success})