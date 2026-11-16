# app/infrastructure/parsers/normalization.py
"""Общая нормализация HTML и публичного JSON без сетевых операций."""

import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from app.domain.models import StructureError

SOCIAL = {
    "vk.com",
    "t.me",
    "telegram.me",
    "wa.me",
    "whatsapp.com",
    "youtube.com",
    "rutube.ru",
    "dzen.ru",
    "max.ru",
    "ok.ru",
    "facebook.com",
    "instagram.com",
}
LEADER_ROLES = r"директор|управляющ|председатель|президент|руководител"


def unique(values: list[str]) -> list[str]:
    """Удаляет повторы, сохраняя первое значение и порядок появления."""
    return list(dict.fromkeys(value for value in values if value))


def safe_source_url(href: str, base: str) -> str:
    """Разрешает только публичные страницы компаний на исходном домене."""
    parts = urlsplit(urljoin(base, href))
    if (
        parts.scheme != "https"
        or parts.netloc != "leaders-project.ru"
        or not parts.path.startswith("/companies/")
    ):
        raise StructureError("Небезопасная ссылка пагинации или карточки")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, ""))


def phone(value: str) -> str:
    """Сохраняет номер и добавочный, не преобразуя их в числовой тип."""
    value = re.sub(r"^tel:", "", unquote(value), flags=re.I).split("?", 1)[0]
    value = re.sub(r"(?:доб\.?|ext\.?)\s*[:.]?\s*(\d+)", r";ext=\1", value, flags=re.I)
    return re.sub(r"[^\d+;=a-zA-Z]", "", value)


def email(value: str) -> str:
    """Нормализует адрес, исключая параметры mailto."""
    return re.sub(r"^mailto:", "", unquote(value), flags=re.I).split("?", 1)[0].strip().lower()


def official_site(value: str) -> str:
    """Исключает платформу, соцсети, credentials и относительные office-ссылки."""
    value = value.strip()
    if not re.match(r"^https?://", value, re.I):
        return ""
    parts = urlsplit(value)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.username or parts.password or not host:
        return ""
    if any(
        host == domain or host.endswith("." + domain) for domain in SOCIAL | {"leaders-project.ru"}
    ):
        return ""
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "", parts.query, "")
    )
