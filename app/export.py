"""
Inventory exports: the printer list, notes included, as CSV or PDF.

The PDF is written by hand on the standard library, for the same reason the
SNMP client is: the install stays three pure-Python wheels. It only needs the
PDF base-14 Helvetica fonts, which every reader has built in, so nothing is
embedded and the whole writer is a page layout plus a few hundred bytes of
object table.
"""

from __future__ import annotations

import csv
import io
import time
import zlib
from typing import Any, Iterable

from .store import PRINTER_COLORS, PrinterRecord

HEALTH_LABELS = {
    "ok": "Ready",
    "warning": "Attention",
    "critical": "Critical",
    "offline": "Offline",
    "unknown": "Unknown",
}


def _when(seconds: float | None) -> str:
    if not seconds:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def _status(record: PrinterRecord) -> str:
    return HEALTH_LABELS.get(record.health, "Unknown")


def _supplies(record: PrinterRecord) -> str:
    """'Black 82%; Cyan 14%' -- consumables only, in the order reported."""
    parts = []
    for supply in (record.stats or {}).get("supplies") or []:
        if supply.get("is_receptacle"):
            continue
        name = (supply.get("colorant") or supply.get("description")
                or supply.get("type") or "Supply")
        pct = supply.get("percent")
        parts.append(f"{name.capitalize()} {pct}%" if pct is not None
                     else f"{name.capitalize()} n/a")
    return "; ".join(parts)


def filename(extension: str) -> str:
    return time.strftime(f"printers-%Y-%m-%d.{extension}")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "Name", "IP address", "Hostname", "SNMP sysName", "Vendor", "Model",
    "Serial number", "MAC address", "Location", "Color", "Color means",
    "Status", "Online",
    "Polling enabled", "Lowest toner %", "Supplies", "Page count",
    "Last polled", "Last seen", "First seen", "Web interface", "Source",
    "Notes",
]


def _csv_cell(value: Any) -> Any:
    """
    Neutralise text a spreadsheet would run as a formula.

    Notes and labels are free text typed into the dashboard, and Excel treats
    a cell starting with = + - @ as a formula -- the standard CSV-injection
    route. A leading apostrophe makes it plain text in every spreadsheet.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def to_csv(records: Iterable[PrinterRecord],
           labels: dict[str, str] | None = None) -> bytes:
    labels = labels or {}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for r in records:
        stats = r.stats or {}
        writer.writerow([_csv_cell(v) for v in (
            r.display_name, r.ip, r.hostname or "", r.sys_name or "",
            r.vendor or "", r.model or "", r.serial_number or "",
            r.mac_address or "", r.location or "", r.color or "",
            labels.get(r.color or "", ""), _status(r),
            "yes" if r.online else "no", "yes" if r.enabled else "no",
            stats.get("lowest_toner_percent", ""), _supplies(r),
            stats.get("page_count", ""), _when(r.last_polled),
            _when(r.last_seen), _when(r.first_seen), r.web_url or "",
            r.source, r.notes or "",
        )])
    # The byte-order mark is what makes Excel read the file as UTF-8 rather
    # than mangling every accented character in a note or location.
    return ("﻿" + out.getvalue()).encode("utf-8")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

# Advance widths (per 1000 em) of the base-14 Helvetica faces for printable
# ASCII, from Adobe's AFM files. Needed to wrap text into table cells.
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278,
    278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584,
    584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667,
    556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611,
    278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222,
    222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500,
    500, 500, 334, 260, 334, 584,
]
_HELVETICA_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278,
    278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584,
    584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722,
    611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611,
    333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278,
    278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556,
    556, 500, 389, 280, 389, 584,
]
# The few non-ASCII characters this module writes itself; anything else
# outside ASCII is measured as an average glyph, which only affects wrapping.
_EXTRA_WIDTHS = {"·": 278, "—": 1000, "–": 556, "…": 1000}

_FONTS = {"F1": _HELVETICA, "F2": _HELVETICA_BOLD}

PAGE_W, PAGE_H = 792, 612  # US Letter, landscape
MARGIN = 36
FONT_SIZE = 8
LEADING = 10
PAD_X, PAD_Y = 4, 4
MAX_LINES = 40  # the most one row can hold and still fit on a page

# (heading, width in points, right-aligned). Widths sum to PAGE_W - 2*MARGIN.
PDF_COLUMNS = [
    ("Printer", 130, False),
    ("Address", 72, False),
    ("Model", 110, False),
    ("Location", 90, False),
    ("Status", 58, False),
    ("Toner", 40, True),
    ("Pages", 52, True),
    ("Notes", 168, False),
]

INK = (0.04, 0.04, 0.04)
MUTED = (0.45, 0.45, 0.43)
RULE = (0.85, 0.85, 0.82)
STRIPE = (0.965, 0.965, 0.955)


def _width(text: str, font: str, size: float) -> float:
    table = _FONTS[font]
    total = 0
    for ch in text:
        code = ord(ch)
        if 32 <= code <= 126:
            total += table[code - 32]
        else:
            total += _EXTRA_WIDTHS.get(ch, 556)
    return total * size / 1000


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Greedy word wrap; a word wider than the cell is broken mid-word."""
    lines: list[str] = []
    for paragraph in (text or "").replace("\r", "").split("\n"):
        line = ""
        for word in paragraph.replace("\t", " ").split(" "):
            candidate = f"{line} {word}" if line else word
            if _width(candidate, font, size) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # Too wide for a line of its own (a URL, a run of dashes): break
            # it wherever it fills the cell, measuring each character once.
            line, used = "", 0.0
            for ch in word:
                step = _width(ch, font, size)
                if line and used + step > width:
                    lines.append(line)
                    line, used = "", 0.0
                line += ch
                used += step
        lines.append(line)
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines or [""]


def _pdf_string(text: str) -> str:
    """A PDF literal string in WinAnsi; unmappable characters become '?'."""
    raw = text.encode("cp1252", "replace").decode("latin-1")
    raw = "".join(ch if ch >= " " else " " for ch in raw)
    return "(" + raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


class _Page:
    def __init__(self) -> None:
        self.ops: list[str] = []

    def fill(self, rgb) -> None:
        self.ops.append("%.3f %.3f %.3f rg" % tuple(rgb))

    def rect(self, x, y, w, h, rgb) -> None:
        self.fill(rgb)
        self.ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def line(self, x1, y1, x2, y2, rgb, width=0.5) -> None:
        self.ops.append("%.3f %.3f %.3f RG %.2f w" % (*rgb, width))
        self.ops.append(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def text(self, x, y, s, font="F1", size=FONT_SIZE, rgb=INK, right=False) -> None:
        if right:
            x -= _width(s, font, size)
        self.fill(rgb)
        self.ops.append(
            f"BT /{font} {size} Tf {x:.2f} {y:.2f} Td {_pdf_string(s)} Tj ET"
        )


def to_pdf(records: list[PrinterRecord], labels: dict[str, str] | None = None,
           version: str = "") -> bytes:
    labels = labels or {}
    exported = time.strftime("%Y-%m-%d %H:%M")
    counts: dict[str, int] = {}
    for r in records:
        counts[_status(r)] = counts.get(_status(r), 0) + 1
    tally = " · ".join(
        f"{counts[label]} {label.lower()}" for label in HEALTH_LABELS.values()
        if counts.get(label)
    )
    subtitle = f"{len(records)} printer{'s' if len(records) != 1 else ''}"
    if tally:
        subtitle += f" · {tally}"
    subtitle += f" · exported {exported}"

    any_color = any(r.color in PRINTER_COLORS for r in records)
    swatch = 9 if any_color else 0  # room left in the name cell for the tag

    # A key to the swatches, in palette order, for the colours actually used:
    # a coloured square means nothing on paper without it.
    used = [c for c in PRINTER_COLORS if any(r.color == c for r in records)]
    legend = [(c, c.capitalize() + (f": {labels[c]}" if labels.get(c) else ""))
              for c in used]

    def cells(r: PrinterRecord) -> list[list[str]]:
        stats = r.stats or {}
        toner = stats.get("lowest_toner_percent")
        pages = stats.get("page_count")
        status = _status(r) + ("" if r.enabled else " (not polled)")
        values = [
            r.display_name, r.ip, r.model or "", r.location or "", status,
            f"{toner}%" if toner is not None else "",
            f"{pages:,}" if isinstance(pages, int) else "",
            r.notes or "",
        ]
        out = []
        for i, ((_, width, _), value) in enumerate(zip(PDF_COLUMNS, values)):
            inner = width - 2 * PAD_X - (swatch if i == 0 else 0)
            lines = _wrap(value, "F1", FONT_SIZE, inner)
            # A row cannot continue onto the next page, so a note longer than
            # a whole page is cut short; the CSV always carries it in full.
            if len(lines) > MAX_LINES:
                lines = lines[:MAX_LINES - 1] + ["\u2026 (continued in the CSV export)"]
            out.append(lines)
        return out

    pages: list[_Page] = []
    top = PAGE_H - MARGIN
    bottom = MARGIN + 18  # leave room for the footer

    def new_page() -> tuple[_Page, float]:
        page = _Page()
        pages.append(page)
        y = top - 14
        page.text(MARGIN, y, "Printer inventory", font="F2", size=15)
        y -= 15
        page.text(MARGIN, y, subtitle, size=8.5, rgb=MUTED)
        y -= 14
        if legend:
            x = MARGIN
            for color, text in legend:
                width = 10 + _width(text, "F1", 8) + 14
                if x + width > PAGE_W - MARGIN:
                    x = MARGIN
                    y -= 12
                page.rect(x, y - 0.5, 6, 6, _rgb(PRINTER_COLORS[color]))
                page.text(x + 10, y, text, size=8)
                x += width
            y -= 14
        # Column headings, repeated on every page.
        head_h = LEADING + 2 * PAD_Y
        x = MARGIN
        for heading, width, right in PDF_COLUMNS:
            tx = x + width - PAD_X if right else x + PAD_X
            page.text(tx, y - PAD_Y - 7, heading.upper(), font="F2", size=7,
                      rgb=MUTED, right=right)
            x += width
        y -= head_h
        page.line(MARGIN, y, PAGE_W - MARGIN, y, MUTED, 0.75)
        return page, y

    page, y = new_page()
    if not records:
        page.text(MARGIN + PAD_X, y - PAD_Y - 8, "No printers in the inventory.",
                  rgb=MUTED)

    for index, r in enumerate(records):
        wrapped = cells(r)
        height = max(len(lines) for lines in wrapped) * LEADING + 2 * PAD_Y
        if y - height < bottom and y < top - 60:
            page, y = new_page()
        if index % 2:
            page.rect(MARGIN, y - height, PAGE_W - 2 * MARGIN, height, STRIPE)
        x = MARGIN
        for col, ((_, width, right), lines) in enumerate(zip(PDF_COLUMNS, wrapped)):
            tx = x + PAD_X
            if col == 0 and r.color in PRINTER_COLORS:
                page.rect(tx, y - PAD_Y - 7.5, 6, 6, _rgb(PRINTER_COLORS[r.color]))
            if col == 0:
                tx += swatch
            for n, text in enumerate(lines):
                ty = y - PAD_Y - 7 - n * LEADING
                if right:
                    page.text(x + width - PAD_X, ty, text, right=True)
                else:
                    page.text(tx, ty, text, font="F2" if col == 0 else "F1")
            x += width
        y -= height
        page.line(MARGIN, y, PAGE_W - MARGIN, y, RULE)

    footer = "Printer Monitor" + (f" v{version}" if version else "")
    for number, p in enumerate(pages, start=1):
        p.text(MARGIN, MARGIN, footer, size=7.5, rgb=MUTED)
        p.text(PAGE_W - MARGIN, MARGIN, f"Page {number} of {len(pages)}",
               size=7.5, rgb=MUTED, right=True)

    return _assemble(pages, title=f"Printer inventory {exported}")


def _assemble(pages: list[_Page], title: str) -> bytes:
    """Serialise pages into a PDF 1.4 file with a correct xref table."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in once the page tree's number is known
    pages_id = add(b"")
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
             b"/Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
             b"/Encoding /WinAnsiEncoding >>")
    info = add(("<< /Title " + _pdf_string(title) + " /Producer (Printer Monitor)"
                + time.strftime(" /CreationDate (D:%Y%m%d%H%M%S) >>"))
               .encode("latin-1"))

    kids = []
    for page in pages:
        stream = zlib.compress("\n".join(page.ops).encode("latin-1"))
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
                      + stream + b"\nendstream")
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_id, PAGE_W, PAGE_H, f1, f2, content)
        ))

    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    objects[pages_id - 1] = (
        b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
        + b"] /Count %d >>" % len(kids)
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\n"
            b"startxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, info, xref))
    return bytes(out)
