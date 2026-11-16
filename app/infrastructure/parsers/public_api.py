# app/infrastructure/parsers/public_api.py
"""Строгие парсеры проверенной публичной JSON-схемы Leaders Project."""

import json
import re
from decimal import Decimal

from app.domain.models import CatalogPage, Contacts, Detail, Listing, SourceError, StructureError
from app.infrastructure.parsers.finance import rubles_to_millions
from app.infrastructure.parsers.normalization import (
    email,
    official_site,
    phone,
    safe_source_url,
    unique,
)
from app.infrastructure.source_urls import catalogue_api_url, next_catalogue_api_url


def catalog(raw: str, url: str) -> CatalogPage:
    """Читает items/total/nextCursor и сохраняет все найденные карточки по порядку."""
    try:
        data = json.loads(raw, parse_float=Decimal)
    except (ValueError, TypeError) as error:
        raise StructureError("Публичный каталог вернул некорректный JSON") from error
    if (
        not isinstance(data, dict)
        or not {"items", "total", "nextCursor"}.issubset(data)
        or not isinstance(data["items"], list)
        or type(data["total"]) is not int
        or data["total"] < 0
    ):
        raise StructureError("Изменилась схема публичного каталога")
    cursor = data["nextCursor"]
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise StructureError("Публичный каталог вернул некорректный cursor")
    companies, seen = [], set()
    for item in data["items"]:
        if (
            not isinstance(item, dict)
            or not all(isinstance(item.get(key), str) for key in ("url", "name", "inn"))
            or not item["name"].strip()
            or not re.fullmatch(r"\d{10,12}", item["inn"])
        ):
            raise StructureError("Неполная запись публичного каталога")
        detail_url = safe_source_url(item["url"], url)
        if not re.fullmatch(r"https://leaders-project\.ru/companies/\d{10,12}", detail_url):
            raise StructureError("Публичный каталог вернул некорректный URL карточки")
        if detail_url not in seen:
            companies.append(
                Listing(
                    " ".join(item["name"].split()),
                    item["inn"],
                    detail_url,
                    revenue_mln=rubles_to_millions(item.get("revenue")),
                )
            )
            seen.add(detail_url)
    if not companies and (cursor is not None or data["total"] > 0):
        raise StructureError("Публичный каталог пуст до подтверждённого окончания")
    next_url = next_catalogue_api_url(url, cursor) if cursor else None
    if next_url and next_url == catalogue_api_url(url):
        raise StructureError("Публичный каталог повторяет текущий cursor")
    return CatalogPage(companies, next_url, data["total"])


def company(raw: str, fields: set[str]) -> dict:
    """Отличает явно пустые поля от изменившейся или неполной схемы ответа."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise StructureError("Публичный API вернул некорректный JSON") from error
    if not isinstance(data, dict) or not fields.issubset(data):
        raise StructureError("Изменилась схема публичной карточки")
    if not isinstance(data.get("inn"), str) or not re.fullmatch(r"\d{10,12}", data["inn"]):
        raise StructureError("Публичная карточка не содержит строковый ИНН")
    return data


def detail(raw: str, expected_inn: str) -> Detail:
    """Извлекает руководителя из leaders, никогда не читая founders."""
    data = company(raw, {"inn", "leaders"})
    if data["inn"] != expected_inn:
        raise SourceError("ИНН карточки отличается от ИНН каталога", "inn_mismatch")
    code = kpp(raw, expected_inn)
    if not isinstance(data["leaders"], list):
        raise StructureError("Изменилась схема руководителей")
    for leader in data["leaders"]:
        if not isinstance(leader, dict) or not {"type", "name", "position"}.issubset(leader):
            raise StructureError("Неполная запись руководителя")
        name, position = leader["name"], leader["position"]
        if (
            not isinstance(name, str)
            or not isinstance(leader["type"], str)
            or not (position is None or isinstance(position, str))
        ):
            raise StructureError("Изменилась схема ФИО руководителя")
        if leader["type"] != "individual":
            if leader["type"] not in {"legal_entity", "legal", "organization", "company"}:
                raise StructureError("Неизвестный тип руководителя публичного API")
            continue
        # В JSON leaders отделён от founders: сама категория явно обозначает руководство.
        if name.strip():
            return Detail(data["inn"], " ".join(name.split()), "ok", kpp=code)
    return Detail(data["inn"], kpp=code)


def kpp(raw: str, expected_inn: str) -> str:
    """Проверяет реквизиты для дозаполнения, не обрабатывая руководителя и контакты."""
    data = company(raw, {"inn"})
    if data["inn"] != expected_inn:
        raise SourceError("ИНН карточки отличается от ИНН каталога", "inn_mismatch")
    value = data.get("kpp")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StructureError("КПП API должен быть строкой")
    return value.strip()


def website(value: str) -> str:
    """Поддерживает доменное имя из поля website без подстановки навигационных URL."""
    value = value.strip()
    if re.fullmatch(r"[\w.-]+\.[\w-]{2,}(?::\d+)?(?:/[^\s]*)?", value):
        value = "https://" + value
    return official_site(value)


def contacts(raw: str) -> Contacts:
    """Читает все типизированные контакты компании и официальный website."""
    data = company(raw, {"inn", "contacts", "website"})
    if not isinstance(data["contacts"], list) or not (
        data["website"] is None or isinstance(data["website"], str)
    ):
        raise StructureError("Изменилась схема контактных данных")
    phones, emails, websites = [], [], []
    for item in data["contacts"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("type"), str)
            or not isinstance(item.get("value"), str)
        ):
            raise StructureError("Неполная запись контакта")
        kind, value = item["type"].lower(), item["value"]
        if kind in {"phone", "fax"}:
            normalized = phone(value)
            if not re.search(r"\d", normalized):
                raise StructureError("Телефон API не содержит номер")
            phones.append(normalized)
        elif kind == "email":
            for address in re.split(r"[,;]", email(value)):
                address = address.strip()
                if "@" not in address or re.search(r"\s", address):
                    raise StructureError("Email API не содержит адрес")
                emails.append(address)
        elif kind == "website":
            websites.append(website(value))
        elif kind not in {"social", "messenger", "office", "address"}:
            raise StructureError("Неизвестный тип контакта публичного API")
    if data["website"]:
        websites.append(website(data["website"]))
    phones, emails, websites = unique(phones), unique(emails), unique(websites)
    return Contacts(phones, emails, websites, "ok" if phones or emails or websites else "missing")
