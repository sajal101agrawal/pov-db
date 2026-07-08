from __future__ import annotations

import io
from zipfile import ZipFile

from app.sources.samco import _csv_text_from_content, _extract_download_url


def test_samco_extracts_download_url_from_html_table() -> None:
    html = """
    <thead><tr><th>NSEFO</th></tr></thead>
    <tbody><tr><td><a href="/bse_nse_mcx/datacopy/abc">20260707_NSEFO.csv</a></td></tr></tbody>
    """

    url = _extract_download_url(html, "https://www.samco.in/bse_nse_mcx/getBhavcopy")

    assert url == "https://www.samco.in/bse_nse_mcx/datacopy/abc"


def test_samco_accepts_direct_csv_content() -> None:
    csv = "INSTRUMENT,SYMBOL\nOPTSTK,RELIANCE\n"

    assert _csv_text_from_content(csv.encode()) == csv


def test_samco_accepts_legacy_zip_content() -> None:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("bhav.csv", "SYMBOL,SERIES\nRELIANCE,EQ\n")

    assert _csv_text_from_content(buffer.getvalue()) == "SYMBOL,SERIES\nRELIANCE,EQ\n"


def test_samco_rejects_html_as_csv() -> None:
    assert _csv_text_from_content(b"<html><body>not ready</body></html>") is None
