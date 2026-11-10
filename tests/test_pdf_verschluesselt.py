"""Encrypted PDFs: protected only against editing (empty user password) they are read; a PDF that
needs a password to open is skipped for good, not recorded as an error."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from projektakte.extract import PDF_ENCRYPTED_TEXT, extract

TEXT = "Die Rettungswege im Obergeschoss sind mindestens 1,20 m breit auszubilden."


def _pdf(user_password: str, algorithm: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject(
        {NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 11 Tf 50 800 Td ({TEXT}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.encrypt(user_password=user_password, owner_password="eigentuemer", algorithm=algorithm)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize("algorithm", ["AES-128", "AES-256", "RC4-128"])
def test_pdf_protected_only_against_editing_is_read(algorithm):
    r = extract(Path("Konzept.pdf"), _pdf("", algorithm))
    assert r.status == "ok" and "1,20 m" in r.text, (r.status, r.message)


def test_pdf_with_a_password_is_skipped_and_not_retried():
    r = extract(Path("Geheim.pdf"), _pdf("geheim", "AES-256"))
    assert (r.status, r.message, r.incomplete, r.retry) == ("skipped", PDF_ENCRYPTED_TEXT, False, False)
