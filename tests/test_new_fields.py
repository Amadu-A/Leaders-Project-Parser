# tests/test_new_fields.py
"""КПП, единицы выручки и числовые ячейки нового Excel на примерах ТЗ."""

import json
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.domain.models import StructureError
from app.infrastructure.excel.exporter import build_workbook
from app.infrastructure.parsers import pages
from app.infrastructure.parsers.finance import displayed_revenue


@pytest.mark.parametrize(
    "source,expected",
    [
        ("25 млн ₽", "25"),
        ("-2 млн ₽", "-2"),
        ("1,5 млрд ₽", "1500"),
        ("962,9 млрд ₽", "962900"),
        ("1\u00a0180 млрд ₽", "1180000"),
        ("1\u202f234,5 млн ₽", "1234.5"),
        ("0 ₽", "0"),
        ("750 тыс. ₽", "0.75"),
        ("Нет данных", None),
    ],
)
def test_displayed_revenue_units(source, expected):
    """Проверяет миллионы, миллиарды, знак, разделители тысяч и настоящий ноль."""
    assert displayed_revenue(source) == (Decimal(expected) if expected is not None else None)


def test_user_html_kpp_and_current_revenue(fixture_html):
    """Читает текущий span из примера, игнорируя историю и процент изменения."""
    html = fixture_html("kpp-requisites.html")
    assert pages.kpp(html, "7708196924") == "770101001"
    assert pages.kpp(html.replace("770101001", "001234567"), "7708196924") == "001234567"
    row = fixture_html("revenue-company-row.html")
    assert pages.catalog(row, "https://leaders-project.ru/companies/projecting?size=100").companies[
        0
    ].revenue_mln == Decimal(-2)
    block = fixture_html("revenue-billion-block.html")
    html = '<div><div><a href="/companies/1000000000">Company</a></div>' + block + "</div>"
    assert pages.catalog(
        html, "https://leaders-project.ru/companies/projecting?size=100"
    ).companies[0].revenue_mln == Decimal(962900)
    html = '<div><a href="/companies/1000000000">Company</a><div><span>0 ₽</span></div></div>'
    assert (
        pages.catalog(html, "https://leaders-project.ru/companies/projecting?size=100")
        .companies[0]
        .revenue_mln
        == 0
    )


def test_api_exact_rubles_and_optional_kpp():
    """Проверенное API revenue хранит рубли, а история не участвует в преобразовании."""
    url = "https://leaders-project.ru/companies/projecting?size=100"
    data = {
        "items": [
            {
                "name": "Company",
                "inn": "1000000000",
                "url": "/companies/1000000000",
                "revenue": 5843594000.0,
                "revenueHistory": [{"revenue": 9999999999999}],
            }
        ],
        "total": 1,
        "nextCursor": None,
    }
    assert pages.catalog(json.dumps(data), url).companies[0].revenue_mln == Decimal("5843.594")
    for amount, expected in [(0, Decimal(0)), (None, None), (-2000000, Decimal(-2))]:
        data["items"][0]["revenue"] = amount
        assert pages.catalog(json.dumps(data), url).companies[0].revenue_mln == expected
    detail = {"inn": "1000000000", "kpp": "001234567", "leaders": []}
    assert pages.detail(json.dumps(detail), detail["inn"]).kpp == "001234567"
    detail.pop("kpp")
    assert pages.detail(json.dumps(detail), detail["inn"]).kpp == ""
    detail["kpp"] = 1234567
    with pytest.raises(StructureError):
        pages.kpp(json.dumps(detail), detail["inn"])


@pytest.mark.parametrize("amount", [None, Decimal(0), Decimal("-2.125")])
def test_xlsx_revenue_is_numeric_or_blank(amount):
    """Ноль не превращается в отсутствие данных; пустое поле не становится нулём."""
    row = {
        "ordinal": 1,
        "name": "Company",
        "inn": "0012345678",
        "kpp": "001201001",
        "revenue_mln": amount,
        "detail_url": "https://leaders-project.ru/companies/0012345678",
        "detail_status": "ok",
        "contacts_status": "ok",
    }
    sheet = load_workbook(BytesIO(build_workbook([row], [])))["Компании"]
    assert sheet["D2"].value == "001201001" and sheet["D2"].data_type == "s"
    assert sheet["E2"].value == (float(amount) if amount is not None else None)
    if amount is not None:
        assert sheet["E2"].data_type == "n"
