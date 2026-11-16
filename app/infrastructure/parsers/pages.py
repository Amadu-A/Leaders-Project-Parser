# app/infrastructure/parsers/pages.py
"""Чистые парсеры каталога, реквизитов и контактов; сетевых операций здесь нет."""

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from app.domain.models import CatalogPage, Contacts, Detail, Listing, SourceError, StructureError
from app.infrastructure.parsers import public_api
from app.infrastructure.parsers.normalization import (
    LEADER_ROLES,
    email,
    official_site,
    phone,
    safe_source_url,
    unique,
)

ORIGIN = "https://leaders-project.ru"


def text(node: Tag) -> str:
    """Нормализует пробельные символы видимого текста, включая nbsp."""
    return " ".join(node.get_text(" ", strip=True).split())


def catalog(html: str, url: str) -> CatalogPage:
    """Читает ссылки или кнопочную пагинацию без предположений о query-параметрах."""
    if html.lstrip().startswith("{"):
        return public_api.catalog(html, url)
    soup = BeautifulSoup(html, "html.parser")
    companies = []
    seen = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if not re.fullmatch(r"(?:https://leaders-project\.ru)?/companies/\d{10,12}/?", href):
            continue
        detail_url = safe_source_url(href, url)
        if detail_url in seen:
            continue
        seen.add(detail_url)
        name = text(anchor)
        if not name:
            raise StructureError("У карточки каталога отсутствует название")
        inn = detail_url.rsplit("/", 1)[-1]
        # Ближайший блок компании важнее ID в URL: так обнаруживаются конфликты ИНН.
        for ancestor in list(anchor.parents)[:4]:
            if ancestor.name in {"main", "body", "html"}:
                break
            labels = [
                re.fullmatch(r"ИНН\s+(\d{10,12})", text(node))
                for node in ancestor.find_all(["span", "small"])
            ]
            found = [match[1] for match in labels if match]
            if found:
                if len(set(found)) != 1:
                    raise StructureError("Не удалось однозначно определить ИНН строки каталога")
                inn = found[0]
                break
        companies.append(Listing(name, inn, detail_url))
    if not companies:
        raise StructureError("В каталоге нет карточек: нужен JavaScript или изменена разметка")

    next_url = None
    # rel/accessible label are stable; numeric page links are accepted only when observed.
    for anchor in soup.select("a[href]"):
        label = f"{anchor.get('aria-label', '')} {text(anchor)}".lower()
        rel = anchor.get("rel", [])
        if "next" in rel or re.search(r"следующ|впер[её]д|next", label):
            if anchor.get("aria-disabled") != "true":
                next_url = safe_source_url(anchor["href"], url)
                break
    if next_url is None:
        current = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        candidates = []
        for anchor in soup.select("a[href]"):
            candidate = urljoin(url, anchor["href"])
            parts = urlsplit(candidate)
            if parts.path != urlsplit(url).path:
                continue
            pages = parse_qs(parts.query).get("page", [])
            if pages and pages[0].isdigit() and int(pages[0]) == current + 1:
                candidates.append(safe_source_url(candidate, url))
        if candidates:
            next_url = candidates[0]
    navigation = soup.select_one('nav[aria-label="Страницы компаний"]')
    if navigation:
        current_button = navigation.select_one('[aria-current="page"]')
        next_button = navigation.select_one('button[aria-label="Следующая страница"]')
        if not current_button or not next_button or not text(current_button).isdigit():
            raise StructureError("Изменилась структура кнопочной пагинации")
        current_number = int(text(current_button))
        fragment = urlsplit(url).fragment
        if fragment.startswith("lp-page=") and current_number != int(fragment[8:]):
            raise StructureError("Активная страница не соответствует checkpoint")
        location = soup.select_one('meta[name="leaders-parser-location"]')
        actual_url = safe_source_url(location["content"] if location else url, url)
        if next_button.has_attr("disabled") or next_button.get("aria-disabled") == "true":
            next_url = None
        else:
            # Это внутренний cursor, а не неподтверждённый параметр URL источника.
            next_url = actual_url + f"#lp-page={current_number + 1}"
    if next_url == url:
        raise StructureError("Пагинация повторяет текущую страницу")
    total = None
    content = soup.select_one("main") or soup
    normalized = text(content)
    matches = re.findall(
        r"(?:из|всего|найдено)\s+([\d\s]+)\s*(?:компан|организац|$)", normalized, re.I
    )
    if matches:
        total = int(re.sub(r"\s", "", matches[-1]))
    return CatalogPage(companies, next_url, total)


def detail(html: str, expected_inn: str) -> Detail:
    """Проверяет ИНН в реквизитах и исключает учредителей из руководителя."""
    if html.lstrip().startswith("{"):
        return public_api.detail(html, expected_inn)
    soup = BeautifulSoup(html, "html.parser")
    requisites = soup.select_one("section#a-requisites")
    leaders = soup.select_one("section#a-leaders")
    if requisites is None:
        raise StructureError("Отсутствует ожидаемый раздел реквизитов")
    inn = ""
    for label in requisites.find_all(["span", "dt", "div"]):
        if text(label) != "ИНН":
            continue
        row = label.parent
        match = re.search(r"\b\d{10,12}\b", text(row))
        if match:
            inn = match[0]
            break
    if not inn:
        raise StructureError("Реквизит ИНН не найден")
    if inn != expected_inn:
        raise SourceError("ИНН карточки отличается от ИНН каталога", "inn_mismatch")
    if leaders is None:
        raise StructureError("Отсутствует ожидаемый раздел руководства")
    for label in leaders.find_all(["div", "span", "dt", "h2", "h3"]):
        role = text(label)
        if role.casefold() in {"учредители", "учредитель"}:
            break
        if label.name in {"h2", "h3"}:
            continue
        if len(role) > 100 or not re.search(LEADER_ROLES, role, re.I):
            continue
        # Роль должна быть отдельным листовым узлом, иначе весь section даёт ложный match.
        if label.find(["div", "span", "dt"]):
            continue
        sibling = label.find_next_sibling(["div", "span", "dd"])
        if sibling and text(sibling) and "учредител" not in text(sibling).lower():
            return Detail(inn, text(sibling), "ok")
    return Detail(inn)


def contacts(html: str) -> Contacts:
    """Берёт все контакты строго из секции текущей компании, никогда из footer."""
    if html.lstrip().startswith("{"):
        return public_api.contacts(html)
    soup = BeautifulSoup(html, "html.parser")
    content = text(soup.select_one("main") or soup).lower()
    if re.search(
        r"(?:войдите|авторизуйтесь).{0,100}(?:просмотр\w* контакт|увидеть контакт)"
        r"|контакт\w*.{0,80}доступ\w*.{0,40}(?:после входа|после регистрации)",
        content,
    ):
        raise SourceError("Для просмотра контактов требуется вход", "login", pause=True)
    section = soup.select_one('section[aria-label="Как связаться с компанией"]')
    if section is None:
        # Отсутствие блока допустимо только внутри распознанной страницы компании.
        if not soup.select_one("h1") or not soup.select_one('a[href$="/contacts"]'):
            raise StructureError("Не удалось распознать страницу контактов компании")
        return Contacts(status="missing")
    phones = []
    for anchor in section.select('a[href^="tel:"]'):
        phones.append(phone(anchor["href"]))
    emails = [email(anchor["href"]) for anchor in section.select('a[href^="mailto:"]')]
    websites = []
    for label in section.find_all(["div", "span", "dt", "p"]):
        if text(label).casefold() not in {"сайт", "сайты"}:
            continue
        block = label.parent
        for anchor in block.select("a[href]"):
            value = official_site(anchor["href"])
            if value:
                websites.append(value)
    return Contacts(unique(phones), unique(emails), unique(websites))
