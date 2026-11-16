# app/infrastructure/excel/exporter.py
"""Формирует XLSX строго в BytesIO: без temp XML, файлов и внешнего storage."""

from io import BytesIO
from math import ceil

import xlsxwriter

from app.domain.models import SUCCESS, Conflict


def wrapped_lines(value: str, width: int) -> int:
    """Оценивает высоту по длинным названиям и каждому переносу контактов."""
    return sum(max(1, ceil(len(line) / (width - 2))) for line in str(value).splitlines() or [""])


def build_workbook(rows, issues: list[dict]) -> bytes:
    """Экспортирует все карточки текстовыми ячейками, исключая formula injection."""
    output = BytesIO()
    with xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    ) as workbook:
        header = workbook.add_format(
            {
                "bg_color": "#15345B",
                "font_color": "white",
                "bold": True,
                "text_wrap": True,
                "valign": "vcenter",
            }
        )
        cell = workbook.add_format({"text_wrap": True, "valign": "top", "num_format": "@"})
        link = workbook.add_format(
            {"text_wrap": True, "valign": "top", "font_color": "#15345B", "underline": 1}
        )
        sheet = workbook.add_worksheet("Компании")
        headers = ["№", "Наименование", "ИНН", "ФИО руководителя", "Телефон", "Почта", "Сайт"]
        sheet.write_row(0, 0, headers, header)
        sheet.set_row(0, 32)
        widths = [7, 48, 16, 36, 25, 32, 38]
        for index, width in enumerate(widths):
            sheet.set_column(index, index, width)
        errors = list(issues)
        company_issues = {issue.get("company_id") for issue in issues}
        count = 0
        for count, company in enumerate(rows, 1):
            phones, emails, sites = (
                company.get(key) or [] for key in ("phones", "emails", "websites")
            )
            values = [
                company["ordinal"],
                company["name"],
                company["inn"],
                company.get("director", ""),
                "\n".join(phones),
                "\n".join(emails),
                "\n".join(sites),
            ]
            sheet.write_number(count, 0, values[0], cell)
            for column, value in enumerate(values[1:], 1):
                # write_string сохраняет даже строки, начинающиеся с = + - @ и tab.
                if len(str(value)) > 32767:
                    raise Conflict("Значение превышает предел ячейки Excel 32767 символов")
                sheet.write_string(count, column, str(value), cell)
            if (
                len(sites) == 1
                and sites[0].startswith(("https://", "http://"))
                and len(sites[0]) <= 2079
            ):
                sheet.write_url(count, 6, sites[0], link, string=sites[0])
            lines = max(
                wrapped_lines(str(value), width)
                for value, width in zip(values[1:], widths[1:], strict=True)
            )
            sheet.set_row(count, min(409, max(36, 15 * (lines + 1))))
            if (
                company.get("detail_status") not in SUCCESS
                or company.get("contacts_status") not in SUCCESS
                or company.get("inn_conflict")
            ) and company.get("id") not in company_issues:
                errors.append(
                    {
                        "url": company["detail_url"],
                        "stage": "company",
                        "message": "Карточка не полностью обработана",
                        "kind": "unfinished",
                        "attempts": company.get("detail_attempts", 0),
                    }
                )
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, count, 6)
        if errors:
            error_sheet = workbook.add_worksheet("Ошибки")
            error_sheet.write_row(
                0, 0, ["URL", "Этап", "Тип", "Причина", "HTTP", "Попыток"], header
            )
            for index, width in enumerate([60, 18, 22, 65, 10, 12]):
                error_sheet.set_column(index, index, width)
            for number, issue in enumerate(errors, 1):
                values = [
                    issue.get(key, "")
                    for key in ("url", "stage", "kind", "message", "http_status", "attempts")
                ]
                for column, value in enumerate(values):
                    error_sheet.write_string(number, column, str(value or ""), cell)
                error_sheet.set_row(number, 42)
            error_sheet.freeze_panes(1, 0)
            error_sheet.autofilter(0, 0, len(errors), 5)
    return output.getvalue()
