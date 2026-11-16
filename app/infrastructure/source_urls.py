# app/infrastructure/source_urls.py
"""Проверенные URL публичного протокола каталога; никаких сетевых операций."""

from urllib.parse import parse_qs, urlencode, urlsplit

from app.domain.models import StructureError

ORIGIN = "https://leaders-project.ru"
CATALOG_PATH = "/api/v1/companies"
CATALOG_FILTERS = {"role": "designer", "sort": "rating", "dir": "desc", "legalStatus": "active"}


def catalogue_api_url(url: str) -> str | None:
    """Сопоставляет проверенный projecting-каталог публичному read-only API."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "leaders-project.ru":
        return None
    params = parse_qs(parts.query, keep_blank_values=True)
    if any(len(values) != 1 for values in params.values()):
        raise StructureError("Повторяющиеся параметры каталога")
    params = {key: values[0] for key, values in params.items()}
    try:
        if parts.path.rstrip("/") == "/companies/projecting":
            if set(params) - {"page", "size"} or parts.fragment:
                return None
            size, page = int(params.get("size", 100)), int(params.get("page", 1))
            if not 1 <= size <= 100 or page < 1:
                raise ValueError
            query = CATALOG_FILTERS | {"limit": size, "offset": (page - 1) * size}
        elif parts.path == CATALOG_PATH:
            if (
                set(params) - {*CATALOG_FILTERS, "limit", "offset", "cursor"}
                or any(params.get(key) != value for key, value in CATALOG_FILTERS.items())
                or not 1 <= int(params.get("limit", 100)) <= 100
                or ("offset" in params and int(params["offset"]) < 0)
                or ("cursor" in params and not params["cursor"])
            ):
                raise ValueError
            query = params
        else:
            return None
    except ValueError as error:
        raise StructureError("Некорректные параметры публичного каталога") from error
    return ORIGIN + CATALOG_PATH + "?" + urlencode(query)


def next_catalogue_api_url(url: str, cursor: str) -> str:
    """Передаёт серверный opaque cursor без декодирования и предположений о page."""
    current = catalogue_api_url(url)
    if current is None:
        raise StructureError("Не удалось определить контекст JSON-каталога")
    query = {key: values[0] for key, values in parse_qs(urlsplit(current).query).items()}
    query.pop("offset", None)
    query["cursor"] = cursor
    return ORIGIN + CATALOG_PATH + "?" + urlencode(query)
