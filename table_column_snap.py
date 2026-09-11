# -*- coding: utf-8 -*-


import logging
from typing import List, Optional

from models import BlockType, BoundingBox

logger = logging.getLogger("table_column_snap")

# ==================== КОНФИГ ====================

ENABLE_TABLE_X_SNAP = True

# Насколько близко должны быть левые края X двух блоков-разных-строк, чтобы
# считаться кандидатами в одну колонку (pt).
X_TOLERANCE_PT = 6.0

# Максимальный сдвиг (pt), на который снап может подвинуть левый край блока.
# Больший требуемый сдвиг = скорее всего это НЕ колонка, снап не применяется.
DELTA_X_MAX_PT = 10.0

# Минимальное число строк (блоков), чтобы кластер X-координат считался
# настоящей колонкой таблицы, а не случайным совпадением X у пары блоков.
MIN_ROWS_PER_COLUMN = 3


def _y_overlap(a: BoundingBox, b: BoundingBox) -> bool:
    return a.y1 < b.y2 and b.y1 < a.y2


def _y_extent_overlap(y0a, y1a, y0b, y1b) -> bool:
    return y0a < y1b and y0b < y1a


class _Column:
    __slots__ = ("blocks", "sum_x", "y_min", "y_max")

    def __init__(self, block):
        self.blocks = [block]
        self.sum_x = block.bbox.x1
        self.y_min = block.bbox.y1
        self.y_max = block.bbox.y2

    @property
    def mean_x(self) -> float:
        return self.sum_x / len(self.blocks)

    def can_accept(self, block) -> bool:
        if abs(block.bbox.x1 - self.mean_x) > X_TOLERANCE_PT:
            return False
        
        for existing in self.blocks:
            if _y_overlap(block.bbox, existing.bbox):
                return False
        return True

    def add(self, block):
        self.blocks.append(block)
        self.sum_x += block.bbox.x1
        self.y_min = min(self.y_min, block.bbox.y1)
        self.y_max = max(self.y_max, block.bbox.y2)


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _union_find_tables(columns: List[_Column]):
   
    n = len(columns)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _y_extent_overlap(columns[i].y_min, columns[i].y_max, columns[j].y_min, columns[j].y_max):
                union(i, j)

    groups = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)
    return list(groups.values())


def apply_table_column_snap(udm_page) -> Optional[dict]:
   
    if not ENABLE_TABLE_X_SNAP:
        return None

    text_blocks = [
        b for b in (udm_page.blocks or [])
        if getattr(b, "type", None) == BlockType.TEXT and getattr(b, "bbox", None) is not None
    ]

    # ---- 1. Кластеризация в колонки (greedy first-fit по X + Y-disjoint) ----
    # Сортировка по Y, затем по X: обрабатываем блоки "сверху вниз, слева
    # направо" — стабильный порядок для воспроизводимых результатов снапа.
    ordered = sorted(text_blocks, key=lambda b: (b.bbox.y1, b.bbox.x1))

    open_columns: List[_Column] = []
    for block in ordered:
        target = None
        best_dist = None
        for col in open_columns:
            if col.can_accept(block):
                dist = abs(block.bbox.x1 - col.mean_x)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    target = col
        if target is not None:
            target.add(block)
        else:
            open_columns.append(_Column(block))

    columns = [c for c in open_columns if len(c.blocks) >= MIN_ROWS_PER_COLUMN]

    debug_log = {
        "enabled": True,
        "params": {
            "x_tolerance_pt": X_TOLERANCE_PT,
            "delta_x_max_pt": DELTA_X_MAX_PT,
            "min_rows_per_column": MIN_ROWS_PER_COLUMN,
        },
        "columns_detected": len(columns),
        "tables_detected": 0,
        "columns": [],
        "conflicts": [],
    }

    if not columns:
        return debug_log

    # ---- 2. Финальная направляющая колонки — медиана X участников ----
    guides = [_median([b.bbox.x1 for b in col.blocks]) for col in columns]

    # ---- 3. Защита от конфликта соседних направляющих: если направляющие
    
    disabled = set()
    for i in range(len(columns)):
        for j in range(i + 1, len(columns)):
            if i in disabled or j in disabled:
                continue
            if abs(guides[i] - guides[j]) < X_TOLERANCE_PT:
                loser = i if len(columns[i].blocks) <= len(columns[j].blocks) else j
                disabled.add(loser)
                debug_log["conflicts"].append({
                    "column_a_guide_x": round(guides[i], 2),
                    "column_b_guide_x": round(guides[j], 2),
                    "disabled_column_guide_x": round(guides[loser], 2),
                })

    # ---- 4. Применяем снап ----
    for idx, col in enumerate(columns):
        guide_x = guides[idx]
        col_entry = {
            "guide_x": round(guide_x, 2),
            "row_count": len(col.blocks),
            "y_min": round(col.y_min, 2),
            "y_max": round(col.y_max, 2),
            "block_ids": [],
            "skipped_block_ids": [],
        }
        if idx in disabled:
            col_entry["disabled_due_to_conflict"] = True
            debug_log["columns"].append(col_entry)
            continue

        for block in col.blocks:
            delta = guide_x - block.bbox.x1
            if abs(delta) > DELTA_X_MAX_PT:
                col_entry["skipped_block_ids"].append(block.id)
                continue
            width = block.bbox.x2 - block.bbox.x1
            block.original_bbox = BoundingBox(
                x1=block.bbox.x1, y1=block.bbox.y1,
                x2=block.bbox.x2, y2=block.bbox.y2,
            )
            block.bbox = BoundingBox(
                x1=round(guide_x, 2), y1=block.bbox.y1,
                x2=round(guide_x + width, 2), y2=block.bbox.y2,
            )
            block.column_guide_x = round(guide_x, 2)

            # Смещаем вложенные строки и спаны, сохраняя исходные координаты для ластика
            for line in getattr(block, "lines", []):
                if line.original_bbox is None:
                    line.original_bbox = BoundingBox(
                        x1=line.bbox.x1, y1=line.bbox.y1,
                        x2=line.bbox.x2, y2=line.bbox.y2,
                    )
                lw = line.bbox.x2 - line.bbox.x1
                new_lx1 = round(line.bbox.x1 + delta, 2)
                line.bbox = BoundingBox(
                    x1=new_lx1, y1=line.bbox.y1,
                    x2=round(new_lx1 + lw, 2), y2=line.bbox.y2,
                )

                # Смещаем WordSpans, если они есть
                for span in getattr(line, "spans", []):
                    if getattr(span, "bbox", None) is not None:
                        sw = span.bbox.x2 - span.bbox.x1
                        new_sx1 = round(span.bbox.x1 + delta, 2)
                        span.bbox = BoundingBox(
                            x1=new_sx1, y1=span.bbox.y1,
                            x2=round(new_sx1 + sw, 2), y2=span.bbox.y2,
                        )

            col_entry["block_ids"].append(block.id)

        debug_log["columns"].append(col_entry)

    # ---- 5. Диагностическая группировка колонок в "таблицы" ----
    table_groups = _union_find_tables(columns)
    debug_log["tables_detected"] = sum(1 for g in table_groups if len(g) >= 2)
    debug_log["tables"] = [
        {
            "column_guides": [round(guides[i], 2) for i in group],
            "column_count": len(group),
        }
        for group in table_groups if len(group) >= 2
    ]

    return debug_log