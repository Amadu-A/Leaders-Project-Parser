# tests/test_parsers.py
"""Проверки извлечения, scope контактов, пагинации и отсутствующих полей."""

import pytest

from app.domain.models import SourceError, StructureError
from app.infrastructure.parsers import pages

URL = "https://leaders-project.ru/companies/projecting?size=100"


def catalog_html(count, offset=0, next_link=""):
    """Строит синтетический каталог с произвольным числом уникальных карточек."""
    return (
        "<main>"
        + "".join(
            f'<a href="/companies/{1000000000 + i}">Компания {i}</a>'
            for i in range(offset, offset + count)
        )
        + next_link
        + "</main>"
    )


@pytest.mark.parametrize("count", [1, 100, 351])
def test_catalog_sizes(count):
    """Не ограничивает размер списка и сохраняет исходный порядок."""
    result = pages.catalog(catalog_html(count), URL)
    assert len(result.companies) == count
    assert result.companies[-1].inn == str(1000000000 + count - 1)


def test_provided_catalog_and_detail(fixture_html):
    """Из реального примера не смешивает директора с учредителями."""
    listing = pages.catalog(fixture_html("catalog-row.html"), URL)
    assert listing.companies[0].inn == "1657043757"
    html = fixture_html("leaders.html") + fixture_html("requisites.html")
    result = pages.detail(html, "1657043757")
    assert result.director == "ГАРАЕВ АЛМАЗ ЛЯБИСОВИЧ"
    assert "ТАТЭНЕРГО" not in result.director
    with pytest.raises(SourceError, match="ИНН карточки"):
        pages.detail(html, "0000000001")


@pytest.mark.parametrize("role", ["Директор", "Управляющий", "И. о. генерального директора"])
def test_director_variants(role, fixture_html):
    """Поддерживает явно обозначенных руководителей с разными должностями."""
    html = (
        fixture_html("requisites.html")
        + f'<section id="a-leaders"><div>{role}</div><div>ИВАНОВ ИВАН</div></section>'
    )
    assert pages.detail(html, "1657043757").director == "ИВАНОВ ИВАН"


def test_missing_director_and_structure(fixture_html):
    """Пустое поле руководителя допустимо; пропавшая структура требует паузы."""
    html = fixture_html("requisites.html") + '<section id="a-leaders"><h2>Учредители</h2></section>'
    assert pages.detail(html, "1657043757").director_status == "missing"
    with pytest.raises(StructureError):
        pages.detail("<h1>Оболочка</h1>", "1657043757")


def test_founder_named_director_is_not_an_executive(fixture_html):
    """Название учредителя с названием должности не превращается в ФИО руководителя."""
    html = fixture_html("requisites.html") + (
        '<section id="a-leaders"><h2>Директор и учредители</h2>'
        '<h3>Учредители</h3><div><span>ООО "ДИРЕКТОР"</span>'
        "<span>Организация</span></div></section>"
    )
    assert pages.detail(html, "1657043757").director == ""


def test_pagination_and_duplicates():
    """Подтверждённый next URL сохраняется, последняя страница не отбрасывается."""
    html = catalog_html(100, next_link='<a rel="next" href="?size=100&amp;page=2">Далее</a>')
    result = pages.catalog(html, URL)
    assert result.next_url == URL + "&page=2"
    last = pages.catalog(catalog_html(17, 100), result.next_url)
    assert len(last.companies) == 17 and last.next_url is None
    assert len(pages.catalog(catalog_html(1) * 2, URL).companies) == 1
    with pytest.raises(StructureError):
        pages.catalog(
            catalog_html(1, next_link='<a rel="next" href="https://evil.test/">Далее</a>'), URL
        )
    with pytest.raises(StructureError):
        pages.catalog("<main>Загрузка...</main>", URL)


def test_visible_inn_is_not_assumed_from_card_id():
    """Разные URL с одинаковым видимым ИНН доходят до проверки конфликтов в БД."""
    html = (
        '<main><div><a href="/companies/1000000001">Один</a>'
        "<span>ИНН 0012345678</span></div>"
        '<div><a href="/companies/1000000002">Два</a>'
        "<span>ИНН 0012345678</span></div></main>"
    )
    result = pages.catalog(html, URL)
    assert len(result.companies) == 2
    assert result.companies[0].inn == result.companies[1].inn == "0012345678"


def test_all_contacts_and_scope(fixture_html):
    """Сохраняет все пять телефонов/email и исключает footer, офисы и соцсети."""
    html = (
        fixture_html("contacts.html") + '<footer><a href="tel:+70000000000">Платформа</a></footer>'
    )
    result = pages.contacts(html)
    assert len(result.phones) == len(result.emails) == 5
    assert result.phones[0] == "+74302000249"
    assert result.websites == ["http://eprog.ru"]
    assert "+70000000000" not in result.phones
    more = (
        '<section aria-label="Как связаться с компанией"><div><dt>Сайт</dt>'
        '<a href="https://site.test">Сайт</a><a href="https://t.me/x">tg</a>'
        '<a href="/companies/1">офис</a></div>'
        '<a href="mailto:A@B.RU?subject=x">email</a>'
        '<a href="mailto:a@b.ru">ещё</a><a href="tel:+7 (800) 11-22">tel</a></section>'
    )
    result = pages.contacts(more)
    assert result.emails == ["a@b.ru"] and result.phones == ["+78001122"]
    assert result.websites == ["https://site.test"]


def test_missing_contacts_vs_unchecked():
    """Отличает распознанную пустую страницу от неполученного JS-контента."""
    result = pages.contacts(
        '<h1>Компания</h1><a href="/companies/1000000000/contacts">Контакты</a>'
    )
    assert result.status == "missing" and not result.phones
    with pytest.raises(StructureError):
        pages.contacts('<footer><a href="tel:+7499">Платформа</a></footer>')
    with pytest.raises(SourceError) as caught:
        pages.contacts("<main><h1>Компания</h1>Войдите для просмотра контактов</main>")
    assert caught.value.kind == "login" and caught.value.pause


def test_button_pagination_cursor_and_last_page(fixture_html):
    """Next-кнопка задаёт cursor; disabled подтверждает конец после сохранения списка."""
    navigation = fixture_html("pagination.html")
    first = pages.catalog(catalog_html(100) + navigation, URL)
    assert first.next_url == URL + "#lp-page=2"
    second_navigation = navigation.replace(
        'aria-label="Страница 1" aria-current="page"', 'aria-label="Страница 1"'
    ).replace('aria-label="Страница 2"', 'aria-label="Страница 2" aria-current="page"')
    second = pages.catalog(catalog_html(100, 100) + second_navigation, first.next_url)
    assert second.next_url == URL + "#lp-page=3"
    last_navigation = second_navigation.replace(
        'aria-label="Следующая страница"', 'aria-label="Следующая страница" disabled'
    )
    last = pages.catalog(catalog_html(10, 100) + last_navigation, first.next_url)
    assert len(last.companies) == 10 and last.next_url is None
    with pytest.raises(StructureError, match="checkpoint"):
        pages.catalog(catalog_html(100) + navigation, first.next_url)
