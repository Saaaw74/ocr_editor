# -*- coding: utf-8 -*-
import re
from models import (
    BoundingBox,
    BlockType,
    FontMeta,
    TextBlock,
    TableBlock,
    TableRow,
    TableCell,
    PictureBlock,
    DocumentPage,
    Line
)

_SUBTEXT_MARKER = re.compile(
    r"^(?:\(?подпись\)?|\(?расшифровка\s*(?:подписи)?\)?|\(?м\.?\s*п\.?\)|\(?ф\.?и\.?о\.?\)?)$",
    re.IGNORECASE
)
_FIO_PATTERN = re.compile(
    r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.|[А-ЯЁ]\.\s*[А-ЯЁ]\.\s*[А-ЯЁ][а-яё]+",
    re.UNICODE
)

def parse_paddle_to_udm(
    paddle_data: list,
    page_w_pt: float = 595.0,
    page_h_pt: float = 842.0,
    img_w_px: float = 0.0,
    img_h_px: float = 0.0,
    page_num: int = 1,
    page_style: object = None,
    line_styles: dict = None,
) -> DocumentPage:
    scale_x = page_w_pt / img_w_px if img_w_px else 1.0
    scale_y = page_h_pt / img_h_px if img_h_px else 1.0

    line_styles = line_styles or {}

    def _font_meta_for(idx: int, bbox_h_pt: float) -> FontMeta:
        """Стиль блока: результат Typography Engine строки -> стиль страницы ->
        прежняя геометрическая эвристика (fallback)."""
        style = line_styles.get(idx) or page_style
        if style is not None:
            return FontMeta(
                family=style.family or "Times New Roman",
                size_pt=max(4.0, min(24.0, float(style.font_size))),
                weight=int(style.weight or 400),
                color="#000000",
                italic=bool(style.italic),
                scale_x=float(style.scale_x or 1.0),
                baseline_offset=float(style.baseline_offset or 0.0),
                confidence=max(0.0, min(1.0, float(style.confidence or 0.0))),
                source=style.source or "auto",
            )
        return FontMeta(
            size_pt=max(6.5, min(14.0, round(bbox_h_pt * 0.8, 1)))
        )

    blocks = []
    for idx, item in enumerate(paddle_data):
        text = (item.get("text") or "").strip()
        bbox_px = item.get("bbox_px")
        if not text or not bbox_px or len(bbox_px) != 4:
            continue

        min_x, min_y, max_x, max_y = [float(v) for v in bbox_px]
        bbox = BoundingBox(
            x1=round(min_x * scale_x, 2),
            y1=round(min_y * scale_y, 2),
            x2=round(max_x * scale_x, 2),
            y2=round(max_y * scale_y, 2),
        )
        if bbox.x2 <= bbox.x1 or bbox.y2 <= bbox.y1:
            continue

        confidence = float(item.get("confidence", 1.0))
        block_id = f"paddle_{idx}"
        line_id = f"{block_id}_l0"

        line = Line(
            id=line_id,
            bbox=bbox,
            text=text,
        )

        bbox_h_pt = bbox.y2 - bbox.y1
        blocks.append(TextBlock(
            id=block_id,
            type=BlockType.TEXT,
            bbox=bbox,
            confidence=confidence,
            source="ocr",
            html_content=f"<p>{text}</p>",
            raw_text=text,
            font=_font_meta_for(idx, bbox_h_pt),
            lines=[line],
        ))

    return DocumentPage(
        page_num=page_num,
        width_pt=page_w_pt,
        height_pt=page_h_pt,
        source_type="ocr",
        blocks=blocks,
    )