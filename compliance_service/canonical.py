"""Canonical document model: pages, tables, cells and their provenance.

The model is intentionally small and format agnostic. It is built from the
recognition channels (or from native Office parsing) and is the only structure
the field layer is allowed to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass(frozen=True)
class Locator:
    """Where a value came from. Never invented: any unknown part stays None."""
    file_id: str
    file_hash: str | None = None
    page: int | None = None
    table: int | None = None
    row: int | None = None
    column: int | None = None
    bbox: tuple | None = None

    def as_dict(self) -> dict:
        return {"file_id": self.file_id, "file_hash": self.file_hash, "page": self.page,
                "table": self.table, "row": self.row, "column": self.column,
                "bbox": list(self.bbox) if self.bbox else None}


@dataclass(frozen=True)
class Cell:
    text: str
    locator: Locator
    rowspan: int = 1
    colspan: int = 1
    header: bool = False

    @property
    def empty(self) -> bool:
        return not str(self.text or "").strip()

    def as_dict(self) -> dict:
        return {"text": self.text, "locator": self.locator.as_dict(), "rowspan": self.rowspan,
                "colspan": self.colspan, "header": self.header, "empty": self.empty}


@dataclass(frozen=True)
class Table:
    table_id: str
    rows: tuple
    locator: Locator
    header_rows: int = 1
    continuation_of: str | None = None
    notes: tuple = ()

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def column_paths(self) -> list:
        """Multi-row header paths, one per visible column."""
        if not self.rows or self.header_rows <= 0:
            return []
        paths = []
        for column in range(self.column_count):
            labels = []
            for row_index in range(min(self.header_rows, len(self.rows))):
                row = self.rows[row_index]
                if column < len(row) and not row[column].empty:
                    labels.append(row[column].text.strip())
            paths.append(tuple(dict.fromkeys(labels)))
        return paths

    def data_rows(self) -> list:
        return list(self.rows[self.header_rows:])

    def as_dict(self) -> dict:
        return {"table_id": self.table_id, "header_rows": self.header_rows,
                "continuation_of": self.continuation_of,
                "column_paths": [list(p) for p in self.column_paths()],
                "notes": list(self.notes), "locator": self.locator.as_dict(),
                "rows": [[cell.as_dict() for cell in row] for row in self.rows]}


@dataclass(frozen=True)
class Page:
    page: int
    page_type: str
    blocks: tuple = ()
    tables: tuple = ()
    receipt: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"page": self.page, "page_type": self.page_type,
                "blocks": [b if isinstance(b, dict) else b.as_dict() for b in self.blocks],
                "tables": [t.as_dict() for t in self.tables], "receipt": dict(self.receipt)}


@dataclass(frozen=True)
class CanonicalDocument:
    file_id: str
    file_hash: str | None
    pages: tuple

    def tables(self) -> list:
        return [table for page in self.pages for table in page.tables]

    def as_dict(self) -> dict:
        return {"file_id": self.file_id, "file_hash": self.file_hash,
                "pages": [page.as_dict() for page in self.pages]}


class _TableCollector(HTMLParser):
    """Collect one HTML table into rows of (text, rowspan, colspan)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list = []
        self._row = None
        self._cell = None
        self._span = (1, 1)
        self._text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._text = []
            self._span = (_int(attributes.get("rowspan")), _int(attributes.get("colspan")))
            self._header = tag == "th"
        elif tag == "br" and self._cell is not None:
            self._text.append(" ")

    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in {"td", "th"} and self._row is not None:
            self._row.append(("".join(self._text).strip(), self._span[0], self._span[1],
                              getattr(self, "_header", False)))
            self._text = []

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


def _int(value, default: int = 1) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def tables_from_html(blocks, *, file_id: str, page: int, file_hash: str | None = None) -> list:
    """Rebuild tables from vl_ocr HTML blocks, keeping spans and empty cells."""
    tables = []
    for index, block in enumerate([b for b in blocks or []
                                   if isinstance(b, dict) and b.get("label") == "table"], start=1):
        collector = _TableCollector()
        try:
            collector.feed(str(block.get("content") or ""))
        except Exception:
            continue
        rows = []
        for row_index, raw_row in enumerate(collector.rows):
            cells = []
            for column_index, (text, rowspan, colspan, header) in enumerate(raw_row):
                cells.append(Cell(text=text,
                                  locator=Locator(file_id=file_id, file_hash=file_hash, page=page,
                                                  table=index, row=row_index, column=column_index),
                                  rowspan=rowspan, colspan=colspan,
                                  header=header or row_index == 0))
            rows.append(tuple(cells))
        if rows:
            header_rows = _header_row_count(rows)
            tables.append(Table(table_id="p%s-t%s" % (page, index), rows=tuple(rows),
                                locator=Locator(file_id=file_id, file_hash=file_hash, page=page,
                                                table=index),
                                header_rows=header_rows))
    return tables


def _header_row_count(rows) -> int:
    """Count leading label rows; a row with any numeric cell ends the header.

    Multi-row headers are normal in Chinese daily reports (group row followed by
    a column row), so a header row is any leading row without digits.
    """
    count = 0
    for row in rows:
        texts = [cell.text.strip() for cell in row if not cell.empty]
        if len(texts) < 2:
            break
        if any(any(char.isdigit() for char in text) for text in texts):
            break
        count += 1
    return max(0, min(count, len(rows) - 1)) if len(rows) > 1 else min(count, 1)


def mark_continuation(tables) -> list:
    """Flag tables that continue the previous one (same column signature)."""
    marked = []
    previous = None
    for table in tables:
        signature = tuple(tuple(path) for path in table.column_paths())
        repeats = bool(previous) and signature == previous[1] and bool(signature)
        marked.append(Table(**{**table.__dict__,
                               "continuation_of": previous[0] if repeats else None}))
        previous = (table.table_id, signature)
    return marked


def document_from_blocks(blocks, *, file_id: str, page: int, page_type: str = "raster_scan",
                         file_hash: str | None = None, receipt: dict | None = None) -> CanonicalDocument:
    """Build one page of the canonical model from channel output."""
    tables = mark_continuation(tables_from_html(blocks, file_id=file_id, page=page,
                                                file_hash=file_hash))
    text_blocks = tuple(b for b in blocks or [] if isinstance(b, dict) and b.get("label") != "table")
    page_model = Page(page=page, page_type=page_type, blocks=text_blocks,
                      tables=tuple(tables), receipt=dict(receipt or {}))
    return CanonicalDocument(file_id=file_id, file_hash=file_hash, pages=(page_model,))
