# tests/test_excel.py
"""Проверки сохранения данных, formula injection и RAM-only выгрузки 37 000 строк."""

import tempfile
import threading
import time
from io import BytesIO

import psutil
import pytest
from openpyxl import load_workbook

from app.domain.models import Conflict
from app.infrastructure.excel.exporter import build_workbook


def company(number=1):
    """Даёт реалистичные несколько контактов и текстовый ИНН с ведущим нулём."""
    return {
        "id": number,
        "ordinal": number,
        "name": '=HYPERLINK("https://evil.test")',
        "inn": "0012345678",
        "director": "+ИВАНОВ ИВАН",
        "phones": ["+78001112233", "+78004445566"],
        "emails": ["one@example.test", "two@example.test"],
        "websites": ["https://example.test"],
        "detail_url": "https://leaders-project.ru/companies/0012345678",
        "detail_status": "ok",
        "contacts_status": "ok",
    }


def forbid_temp(*args, **kwargs):
    """Запрещает скрытые XML/XLSX temp-файлы во время создания отчёта."""
    raise AssertionError("Export must never create temporary files")


def test_xlsx_preserves_every_value_without_temp_files(monkeypatch):
    """Проверяет текст, контакты, фильтры, ссылки и точный порядок колонок."""
    monkeypatch.setattr(tempfile, "mkstemp", forbid_temp)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", forbid_temp)
    data = build_workbook([company()], [])
    book = load_workbook(BytesIO(data))
    sheet = book["Компании"]
    assert [cell.value for cell in sheet[1]] == [
        "№",
        "Наименование",
        "ИНН",
        "ФИО руководителя",
        "Телефон",
        "Почта",
        "Сайт",
    ]
    assert sheet["B2"].data_type == sheet["C2"].data_type == sheet["D2"].data_type == "s"
    assert sheet["C2"].value == "0012345678"
    assert sheet["E2"].value == "+78001112233\n+78004445566"
    assert sheet["F2"].value == "one@example.test\ntwo@example.test"
    assert sheet["G2"].hyperlink.target == "https://example.test"
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:G2"
    assert sheet["E2"].alignment.wrap_text


def test_incomplete_and_multiple_websites():
    """Все обнаруженные строки и все сайты остаются в XLSX, ошибки видны отдельно."""
    row = company()
    row["contacts_status"] = "pending"
    row["websites"] = ["https://one.test", "https://two.test"]
    book = load_workbook(BytesIO(build_workbook([row], [])))
    assert book["Компании"]["G2"].value == "https://one.test\nhttps://two.test"
    assert "Ошибки" in book.sheetnames and book["Ошибки"].max_row == 2


def test_contacts_are_never_silently_truncated():
    """Непредставимая в XLSX ячейка отклоняется, а не теряет собранные контакты."""
    row = company()
    row["emails"] = ["a" * 32768]
    with pytest.raises(Conflict, match="32767"):
        build_workbook([row], [])


def test_long_company_names_have_readable_row_height():
    """Высота строки учитывает перенос названия даже при небольшом числе контактов."""
    row = company()
    row["name"] = "Длинное наименование компании " * 8
    book = load_workbook(BytesIO(build_workbook([row], [])))
    assert book["Компании"].row_dimensions[2].height >= 90


def test_37000_rows_ram_only(monkeypatch):
    """Измеряет peak RSS и проверяет последнюю строку большой синтетической выгрузки."""
    monkeypatch.setattr(tempfile, "mkstemp", forbid_temp)
    process = psutil.Process()
    baseline = process.memory_info().rss
    samples = [baseline]
    stop = threading.Event()

    def sample():
        """Снимает RSS в фоне, не влияя на memory-only создание отчёта."""
        while not stop.wait(0.01):
            samples.append(process.memory_info().rss)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    started = time.perf_counter()
    try:
        data = build_workbook((company(i) for i in range(1, 37001)), [])
    finally:
        stop.set()
        thread.join()
    elapsed = time.perf_counter() - started
    delta = (max(samples) - baseline) / 1024**2
    print(
        f"37000 rows: {elapsed:.2f}s, peak RSS delta {delta:.1f} MiB, "
        f"XLSX {len(data) / 1024**2:.2f} MiB"
    )
    assert delta < 800
    book = load_workbook(BytesIO(data), read_only=True)
    assert book["Компании"].max_row == 37001
    assert next(book["Компании"].iter_rows(min_row=37001, values_only=True))[0] == 37000
