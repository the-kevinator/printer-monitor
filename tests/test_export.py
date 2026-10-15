"""The hand-written PDF and the CSV export, tested without the web app."""

import re
import zlib

from app import export
from app.store import PrinterRecord


def _printer(i, **fields):
    defaults = dict(
        ip=f"10.0.0.{i}", label=f"Printer {i}", model="HP M553",
        location="Floor 2", health="ok", notes="Behind reception.",
        stats={"lowest_toner_percent": 40, "page_count": 1000 * i},
    )
    defaults.update(fields)
    return PrinterRecord(**defaults)


def _objects(pdf: bytes) -> dict[int, int]:
    return {int(m.group(1)): m.start()
            for m in re.finditer(rb"(?m)^(\d+) 0 obj", pdf)}


def _page_text(pdf: bytes) -> str:
    """Every string drawn, decompressed from each content stream."""
    out = []
    for stream in re.findall(rb"stream\n(.*?)\nendstream", pdf, re.S):
        out.append(zlib.decompress(stream).decode("latin-1"))
    return "\n".join(out)


def test_xref_offsets_point_at_their_objects():
    """A reader trusts the xref table; one byte off and the file is 'damaged'."""
    pdf = export.to_pdf([_printer(i) for i in range(1, 5)], version="9.9")
    start = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
    assert pdf[start:start + 4] == b"xref"
    entries = re.findall(rb"(\d{10}) 00000 n ", pdf[start:])
    actual = _objects(pdf)
    assert [int(e) for e in entries] == [actual[n] for n in sorted(actual)]


def test_long_inventories_paginate_with_numbered_pages():
    pdf = export.to_pdf([_printer(i % 250 + 1, notes="word " * 60)
                         for i in range(80)])
    count = int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", pdf).group(1))
    assert count > 1
    text = _page_text(pdf)
    assert f"(Page 1 of {count})" in text
    assert f"(Page {count} of {count})" in text
    # The column headings repeat on every page.
    assert text.count("(NOTES)") == count


def test_text_is_escaped_and_encoded():
    pdf = export.to_pdf([_printer(1, notes="Café (east) \\ back door")])
    text = _page_text(pdf)
    assert "(Caf\xe9 \\(east\\) \\\\ back door)" in text


def test_an_enormous_note_is_cut_short_rather_than_hanging_or_overflowing():
    pdf = export.to_pdf([_printer(1, notes="x" * 20000)])
    text = _page_text(pdf)
    assert "continued in the CSV export" in text
    assert text.count("Page 1 of 1") == 1


def test_wrap_breaks_words_wider_than_the_cell():
    lines = export._wrap("a " + "y" * 200, "F1", 8, 60)
    assert lines[0] == "a"
    assert "".join(lines[1:]) == "y" * 200
    assert all(export._width(line, "F1", 8) <= 60 for line in lines)


def test_empty_inventory_still_makes_a_valid_page():
    pdf = export.to_pdf([])
    assert b"/Count 1" in pdf
    assert "No printers in the inventory." in _page_text(pdf)


def test_csv_defuses_formulas_but_leaves_numbers_alone():
    rows = export.to_csv([_printer(1, label="-Lobby", notes="+1 555 0100")])
    text = rows.decode("utf-8-sig")
    assert "'-Lobby" in text
    assert "'+1 555 0100" in text
    assert ",1000," in text


def test_pdf_has_a_legend_for_the_colours_in_use():
    pdf = export.to_pdf([_printer(1, color="blue"), _printer(2, color="red")],
                        {"blue": "2nd floor"})
    text = _page_text(pdf)
    assert "(Blue: 2nd floor)" in text
    assert "(Red)" in text
    assert "(Green" not in text
