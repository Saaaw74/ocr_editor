# -*- coding: utf-8 -*-


from typing import Tuple

BBox = Tuple[float, float, float, float]


def raw_to_display_bbox(bbox: BBox, rotation: int, raw_w: float, raw_h: float):
    
    x0, y0, x1, y1 = bbox
    r = int(rotation) % 360

    if r == 0:
        return (x0, y0, x1, y1), raw_w, raw_h
    if r == 90:
        return (raw_h - y1, x0, raw_h - y0, x1), raw_h, raw_w
    if r == 180:
        return (raw_w - x1, raw_h - y1, raw_w - x0, raw_h - y0), raw_w, raw_h
    if r == 270:
        return (y0, raw_w - x1, y1, raw_w - x0), raw_h, raw_w

    # Нестандартный угол - не должно происходить для валидного PDF.
    return (x0, y0, x1, y1), raw_w, raw_h


def display_to_raw_bbox(bbox: BBox, rotation: int, raw_w: float, raw_h: float) -> BBox:
    
    dx0, dy0, dx1, dy1 = bbox
    r = int(rotation) % 360

    if r == 0:
        return (dx0, dy0, dx1, dy1)
    if r == 90:
        return (dy0, raw_h - dx1, dy1, raw_h - dx0)
    if r == 180:
        return (raw_w - dx1, raw_h - dy1, raw_w - dx0, raw_h - dy0)
    if r == 270:
        return (raw_w - dy1, dx0, raw_w - dy0, dx1)

    return (dx0, dy0, dx1, dy1)


def get_page_rotation_and_raw_size(page) -> Tuple[int, float, float]:
    
    rotation = int(page.rotation or 0) % 360
    mb = page.mediabox
    return rotation, float(mb.width), float(mb.height)


def display_to_raw_point(dx: float, dy: float, rotation: int, raw_w: float, raw_h: float) -> Tuple[float, float]:
  
    r = int(rotation) % 360
    if r == 0:
        return (dx, dy)
    if r == 90:
        return (dy, raw_h - dx)
    if r == 180:
        return (raw_w - dx, raw_h - dy)
    if r == 270:
        return (raw_w - dy, dx)
    return (dx, dy)