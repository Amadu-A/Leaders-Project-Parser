# app/infrastructure/parsers/finance.py
"""Преобразование текущей выручки в миллионы рублей без binary float-арифметики."""

import re
from decimal import Decimal, InvalidOperation

from app.domain.models import StructureError


def rubles_to_millions(value) -> Decimal | None:
    """Публичный API отдаёт revenue в рублях; null означает отсутствие данных."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise StructureError("Изменился числовой формат выручки API")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise StructureError("Выручка API не является конечным числом")
    return amount / Decimal(1_000_000)


def displayed_revenue(value: str) -> Decimal | None:
    """Читает основное значение млн/млрд/тыс/₽, сохраняя ноль и отрицательные суммы."""
    value = " ".join(value.replace("−", "-").split())
    if value.casefold() in {"", "—", "-", "нет данных", "н/д"}:
        return None
    match = re.fullmatch(
        r"([+-]?\s*\d[\d\s]*(?:[.,]\d+)?)\s*(млрд|млн|тыс\.?)?\s*(?:₽|руб\.?)?",
        value,
        re.I,
    )
    if not match:
        raise StructureError("Не удалось распознать текущее значение выручки")
    try:
        number = Decimal(re.sub(r"\s", "", match[1]).replace(",", "."))
    except InvalidOperation as error:
        raise StructureError("Некорректное значение выручки") from error
    unit = (match[2] or "").lower().rstrip(".")
    return (
        number
        * {
            "млрд": Decimal(1000),
            "млн": Decimal(1),
            "тыс": Decimal("0.001"),
            "": Decimal("0.000001"),
        }[unit]
    )
