from enum import Enum
from typing import List, Optional, Union
from pydantic import BaseModel, Field


class BlockType(str, Enum):
    TEXT = "text"
    SECTION_HEADER = "section_header"
    TABLE = "table"
    PICTURE = "picture"
    DRAWING = "drawing"


class CoordinateSystem(str, Enum):
    PDF_POINTS = "pdf_points"  # Стандартные 72 pt (Top-Left)
    NORMALIZED = "normalized"   # 0.0 - 1.0


class BoundingBox(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return round(self.x2 - self.x1, 2)

    @property
    def height(self) -> float:
        return round(self.y2 - self.y1, 2)


class FontMeta(BaseModel):
    family: str = "Arial"
    size_pt: float = 11.0
    weight: int = 400  # 400 = normal, 700 = bold
    color: str = "#000000"

    italic: Optional[bool] = False
    scale_x: Optional[float] = 1.0       # горизонтальный масштаб (0.85..1.15)
    baseline_offset: Optional[float] = 0.0  # pt
    confidence: Optional[float] = 1.0    # 0.0..1.0
    source: Optional[str] = "auto"       # auto | inherited | user | fallback


class WordSpan(BaseModel):
    """Минимальная текстовая единица внутри строки с полными атрибутами шрифта."""
    id: str
    bbox: BoundingBox
    text: str
    font_family: Optional[str] = "Arial"
    size_pt: Optional[float] = 11.0
    is_bold: Optional[bool] = False
    is_italic: Optional[bool] = False
    color: Optional[str] = "#000000"


class Line(BaseModel):

    id: str
    bbox: BoundingBox
    text: str
    spans: List[WordSpan] = Field(default_factory=list)
    original_bbox: Optional[BoundingBox] = None


class BaseBlock(BaseModel):
    id: str
    type: BlockType
    bbox: BoundingBox
    confidence: float = 1.0
    is_modified: bool = False

    source: str = "ocr"


class TextBlock(BaseBlock):
    type: BlockType = BlockType.TEXT
    html_content: str
    raw_text: str
    font: FontMeta = Field(default_factory=FontMeta)

    lines: List[Line] = Field(default_factory=list)

    geometry: Optional[dict] = None

    original_bbox: Optional[BoundingBox] = None

    column_guide_x: Optional[float] = None


class TableCell(BaseModel):
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    text: str
    bbox: Optional[BoundingBox] = None


class TableRow(BaseModel):
    cells: List[TableCell]


class TableBlock(BaseBlock):
    type: BlockType = BlockType.TABLE
    html_content: str
    raw_text: str = ""  # adapters.py уже передаёт это значение - поле было пропущено ранее
    rows: List[TableRow] = Field(default_factory=list)
    bordered: bool = True

class PictureBlock(BaseBlock):
    type: BlockType = BlockType.PICTURE
    image_base64: Optional[str] = None


class DocumentPage(BaseModel):
    page_num: int
    width_pt: float
    height_pt: float
    coordinate_system: CoordinateSystem = CoordinateSystem.PDF_POINTS

    source_type: str = "ocr"
    blocks: List[Union[TextBlock, TableBlock, PictureBlock]]


class DocumentMetadata(BaseModel):
    filename: str
    total_pages: int
    is_scanned: bool


class UnifiedDocument(BaseModel):
    metadata: DocumentMetadata
    pages: List[DocumentPage]