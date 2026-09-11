# -*- coding: utf-8 -*-

from __future__ import annotations
import math
import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Только для статических анализаторов (Pylance/mypy): здесь np
    # безусловно "настоящий" модуль numpy, поэтому "np.ndarray" в
    # аннотациях ниже - валидный тип. В рантайме (см. блок else) np может
    # оказаться None, если NumPy не установлен - код-код ниже везде
    # проверяет `np is None` перед использованием, аннотации на это не
    # влияют (только подсказки для IDE/type checker).
    import numpy as np
else:
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        np = None

try:
    from scipy import ndimage
except ImportError:  # pragma: no cover
    ndimage = None

import fitz  # PyMuPDF

logger = logging.getLogger("table_grid_protect")

# ==================== Параметры (универсальные, без привязки к документу) ====================

# DPI, на котором строится глобальная карта линий. Совпадает со значением
# по умолчанию restore_local_background(dpi=150) в document_routes.py, чтобы
# избежать лишнего ресемплинга маски при кропе под конкретный erase-бокс.
GRID_MASK_DPI = 150.0

# Минимальная ФИЗИЧЕСКАЯ длина непрерывного тёмного пробега (pt), чтобы он
# считался линией таблицы/бланка, а не буквой/цифрой/акцентом. 40 pt гарантирует,
# что ни один десцендер (хвостик буквы) или наклонный штрих не будет сочтен линией.
MIN_LINE_LEN_PT = 40.0

# Окно локального среднего (pt) для адаптивной бинаризации.
ADAPTIVE_WINDOW_PT = 8.0

# Порог контраста линии: отсекает бледные градиентные складки сканера и компрессионные артефакты
ADAPTIVE_OFFSET = 28.0

# Толерантность к перекосу скана (px, в направлении, ПЕРПЕНДИКУЛЯРНОМ
# проверяемой линии) при проверке "достаточно ли длинный пробег". См.
# описание в docstring модуля (п.3). 2px при GRID_MASK_DPI=150 надёжно
# держит скан-перекосы вплоть до ~2-3 градусов на характерных длинах ячеек.
SKEW_TOLERANCE_PX = 2

# Финальная дилатация итоговой маски (px) - захват антиалиасинг-ободка линии.
GRID_MASK_DILATE_PX = 1


@dataclass
class GridLineMask:
    """Глобальная (на всю страницу) карта пикселей линий таблицы/бланка.
    mask: bool HxW. rect: DISPLAY-пространство страницы, которому
    соответствует mask (обычно (0,0,page.rect.width,page.rect.height))."""

    mask: np.ndarray
    rect: fitz.Rect
    dpi: float

    def crop(self, rect_pt: fitz.Rect) -> np.ndarray:
        """Возвращает булев кроп маски, соответствующий rect_pt (DISPLAY-
        пространство, pt). Пустой массив shape (0,0), если rect_pt не
        пересекается со страницей/маской, или если NumPy недоступен."""
        if np is None or self.mask is None or self.mask.size == 0:
            return None
        H, W = self.mask.shape[:2]
        pw = max(1e-3, float(self.rect.width))
        ph = max(1e-3, float(self.rect.height))
        sx = W / pw
        sy = H / ph

        # Нормализация координат относительно левого верхнего угла страницы
        rx0 = min(rect_pt.x0, rect_pt.x1)
        rx1 = max(rect_pt.x0, rect_pt.x1)
        ry0 = min(rect_pt.y0, rect_pt.y1)
        ry1 = max(rect_pt.y0, rect_pt.y1)

        x0 = int(math.floor((rx0 - self.rect.x0) * sx))
        y0 = int(math.floor((ry0 - self.rect.y0) * sy))
        x1 = int(math.ceil((rx1 - self.rect.x0) * sx))
        y1 = int(math.ceil((ry1 - self.rect.y0) * sy))

        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)

        if x1 <= x0 or y1 <= y0:
            return np.zeros((0, 0), dtype=bool)
        return self.mask[y0:y1, x0:x1]


def _adaptive_ink_mask(gray: np.ndarray, dpi: float) -> np.ndarray:
    """Локально-адаптивная бинаризация (см. docstring модуля, п.2)."""
    win_px = max(3, int(round(ADAPTIVE_WINDOW_PT / 72.0 * dpi)))
    if win_px % 2 == 0:
        win_px += 1
    local_mean = ndimage.uniform_filter(gray, size=win_px, mode="nearest")
    return (local_mean - gray) >= ADAPTIVE_OFFSET


def _long_run_mask(ink: np.ndarray, min_len_px: int, axis: str) -> np.ndarray:
    """Пиксели ink, принадлежащие пробегу длины >= min_len_px вдоль axis
    ('h' - горизонталь, 'v' - вертикаль), с толерантностью к перекосу
    (см. docstring модуля, п.3): перед открытием маска чуть расширяется
    ПЕРПЕНДИКУЛЯРНО оси, чтобы небольшой перекос не рвал пробег; на выходе
    результат обрезается обратно до фактических пикселей ink."""
    tol = max(0, int(SKEW_TOLERANCE_PX))
    if axis == "h":
        perp_struct = np.ones((2 * tol + 1, 1), dtype=bool) if tol > 0 else None
        open_struct = np.ones((1, max(3, min_len_px)), dtype=bool)
    else:
        perp_struct = np.ones((1, 2 * tol + 1), dtype=bool) if tol > 0 else None
        open_struct = np.ones((max(3, min_len_px), 1), dtype=bool)

    tolerant = ndimage.binary_dilation(ink, structure=perp_struct) if perp_struct is not None else ink
    opened = ndimage.binary_opening(tolerant, structure=open_struct)
    return opened & ink


def build_grid_line_mask(page: fitz.Page, dpi: float = GRID_MASK_DPI) -> Optional[GridLineMask]:
    """Строит глобальную карту линий таблицы/бланка для ВСЕЙ страницы (см.
    docstring модуля). Возвращает None, если NumPy/SciPy недоступны или
    страница пуста/некорректна - вызывающий код должен в этом случае мягко
    деградировать к прежнему локальному эвристическому способу защиты линий.

    ВАЖНО: вызывать ДО применения любых редакций/правок на этой странице -
    маска должна отражать ИСХОДНУЮ, ещё не тронутую геометрию бланка."""
    if np is None or ndimage is None:
        return None
    try:
        dpi_int = int(round(dpi))
        pix = page.get_pixmap(dpi=dpi_int, alpha=False, colorspace=fitz.csGRAY)
        if pix.width < 4 or pix.height < 4:
            return None
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width
        ).astype(np.float32)

        ink = _adaptive_ink_mask(gray, dpi_int)
        if not ink.any():
            grid = np.zeros_like(ink, dtype=bool)
        else:
            min_len_px = max(6, int(round(MIN_LINE_LEN_PT / 72.0 * dpi_int)))
            horiz = _long_run_mask(ink, min_len_px, axis="h")
            vert = _long_run_mask(ink, min_len_px, axis="v")
            grid = horiz | vert
            if GRID_MASK_DILATE_PX > 0:
                grid = ndimage.binary_dilation(grid, iterations=GRID_MASK_DILATE_PX)

        # page.rect в PyMuPDF всегда задан в DISPLAY-пространстве с учетом угла /Rotate
        page_rect = fitz.Rect(page.rect)
        return GridLineMask(mask=grid, rect=page_rect, dpi=float(dpi_int))
    except Exception:  # noqa: BLE001
        logger.warning("[GRID] build_grid_line_mask failed, falling back to legacy heuristic", exc_info=True)
        return None