<!-- README.md -->
# Leaders Project Parser

Автономный сбор каталога [leaders-project.ru](https://leaders-project.ru/companies/projecting?size=100): полный список компаний, руководители, все телефоны, email и официальные сайты. Интерфейс на FastAPI/Jinja2/JavaScript, отдельный последовательный worker, собственная PostgreSQL. Полный сбор запускается кнопкой **«Начать сбор»**; сборка и запуск контейнеров не обращаются к каталогу.

## Как работает

1. Worker проверяет robots.txt, условия доступа и проводит пробу трёх карточек с контактами. При HTTP 404 проверяет до пяти кандидатов. Если данные не удаётся проверить, массовый сбор не начинается.
2. Собирает **весь индекс**, передавая `nextCursor` из каждого ответа публичного API. Последняя страница сохраняется до завершения индексации; число уникальных организаций сверяется с `total`.
3. Сначала завершает индексацию, затем обрабатывает организации в исходном порядке.
4. Проверяет ИНН, руководителя из `leaders` отдельно от `founders`, все типизированные контакты и официальный `website`. Один ответ `/api/v1/companies/{inn}` содержит оба набора данных; в RAM хранится только текущая карточка. Результаты реквизитов и контактов фиксируются раздельно. После рестарта pending-контакты запрашиваются заново.
5. Формирует Excel только в памяти по запросу скачивания. Все найденные компании включаются в отчёт, неполные карточки — также в лист «Ошибки».

Обычный HTML сайта возвращает оболочку без данных. При подтверждённом разрешении владельца `SOURCE_MODE=auto` и `http` используют публичный JSON API для стандартного каталога из ТЗ; Chromium не запускается. Каждый фактический запрос, redirect и retry проходит через одну HTTP-сессию и limiter с паузой 7–12 секунд. Keepalive дольше этой паузы позволяет переиспользовать соединение. Ответы API и HTML не сохраняются на диск.

Пагинация API проверена на двух непересекающихся списках по 100 компаний: `nextCursor` возвращает тот же следующий список, что и подтверждённый `offset=100`. Последний ответ содержит `nextCursor: null`. Сервис передаёт opaque cursor без декодирования, сохраняет его в checkpoint и обнаруживает повтор содержимого или cursor.

HTML-парсеры и `SOURCE_MODE=browser` оставлены для диагностики разметки. Кнопочный драйвер нажимает `nav[aria-label="Страницы компаний"] button[aria-label="Следующая страница"]`, ждёт изменения `aria-current="page"` и списка. На живом сайте подтверждён переход на `?page=2&size=100`. Внутренний `#lp-page=N` сохраняет позицию для восстановления. Используется один Chromium context, все его запросы также проходят через limiter; лишние ресурсы и prefetch отключены, JS/CSS кэшируются в RAM. Этот режим существенно медленнее: `BROWSER_RENDER_TIMEOUT=1800` ограничивает ожидание DOM; live-проверка карточек выполнена в основном HTTP/API-режиме.

`robots.txt` запрещает `/api`, при этом публичный интерфейс зависит от read-only `/api/v1/companies`. **13.11.2026 пользователь подтвердил разрешение владельца источника на автоматический сбор.** `SOURCE_API_ACCESS_APPROVED=true` учитывает это разрешение только для публичных запросов интерфейса. Значение `false` запрещает их согласно robots.txt. Закрытые разделы, авторизация и CAPTCHA не обходятся. См. [исследование источника](docs/research.md).

## Локальная проверка в Windows PowerShell

Python 3.12+ и Node.js 22+ для небольших JS-тестов:

```powershell
cd C:\Users\i.mein\PycharmProjects\Leaders-Project-Parser
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install -e '.[dev]'
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
.\.venv\Scripts\python -m pytest -q
node --test tests/test_ui.mjs
```

Offline-тесты не обращаются к сайту. Два PostgreSQL-теста включаются только с `TEST_DATABASE_URL`, указывающим именно на **изолированную одноразовую PostgreSQL-базу `leaders_test`**: fixture проверяет имя БД и пересоздаёт её таблицы. Никогда не указывайте production database.

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/leaders_test'
.\.venv\Scripts\python -m pytest -q -m postgres
```

Проверка настоящего Chromium на синтетическом HTML полностью offline:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.cache\ms-playwright"
.\.venv\Scripts\python -m playwright install --only-shell chromium
$env:TEST_BROWSER = '1'
.\.venv\Scripts\python -m pytest -q tests/test_source.py
Remove-Item Env:TEST_BROWSER
```

Разрешённый малый smoke источника — **только вручную**, две страницы каталога и три организации, без сохранения HTML, контактов в логах и XLSX:

```powershell
.\.venv\Scripts\python -m scripts.smoke
```

Для повторной проверки только трёх карточек, когда пагинация уже проверена:

```powershell
.\.venv\Scripts\python -m scripts.smoke --inn 1657043757 --inn 7708196924 --inn 7707717910
```

Скрипт показывает этап, число запросов и наличие нужных секций, не выводя значения контактов.

Перед Git commit/push:

```powershell
git diff --check
git status --short
git add app tests scripts docs .github Dockerfile compose.yaml .dockerignore .gitignore .env.example alembic.ini pyproject.toml requirements.lock README.md
git commit -m "Implement resumable Leaders Project parser"
git push origin main
```

Если Git сообщает `dubious ownership` из-за запуска в sandbox, можно добавить **только к команде** `git -c safe.directory=C:/Users/i.mein/PycharmProjects/Leaders-Project-Parser ...`, без изменения глобальной конфигурации. В этой работе commit и push автоматически не выполняются.

## Развёртывание на aistation

Никакие существующие контейнеры shared-infrastructure не изменяются. Вывод `docker ps` из ТЗ не заменяет проверку занятости порта перед запуском:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker network ls
sudo ss -lntp '( sport = :8089 )'
```

Продолжать, только если порт 8089 свободен. Нужен Docker Compose с поддержкой `env_file.required` (2.24+):

```bash
mkdir -p ~/projects
cd ~/projects
git clone git@github.com:Amadu-A/Leaders-Project-Parser.git leaders-project-parser
cd leaders-project-parser
```

Создать приватный `.env` с двумя случайными паролями. Команда не выводит их и не перезаписывает существующий файл:

```bash
umask 077
python3 - <<'PY'
from pathlib import Path
from secrets import token_urlsafe
with Path('.env').open('x', encoding='utf-8') as stream:
    stream.write(f'APP_PASSWORD={token_urlsafe(24)}\nPOSTGRES_PASSWORD={token_urlsafe(24)}\n')
PY
```

При необходимости добавить в `.env` `APP_BIND_IP=192.168.55.3` и `APP_PORT=8089`, ограничив доступ доверенной LAN/VPN. Без override сервис слушает **127.0.0.1:8089**; извне используется SSH-туннель. Basic Auth защищает UI, API, контакты и скачивание; пароль минимум 16 символов, логин `operator` по умолчанию. Для сети без защищённого VPN/SSH используйте HTTPS на существующем reverse proxy. PostgreSQL наружу не публикуется.

Все Compose-команды используют одинаковое layered loading; приоритет: окружение команды → `.env` → `.env.example`:

```bash
docker compose --env-file .env.example --env-file .env config --quiet
docker compose --env-file .env.example --env-file .env build
docker compose --env-file .env.example --env-file .env up -d --wait --wait-timeout 180
docker compose --env-file .env.example --env-file .env ps
curl --fail http://127.0.0.1:8089/health
curl --fail http://127.0.0.1:8089/ready
docker compose --env-file .env.example --env-file .env logs --tail=100 leaders-parser-api leaders-parser-worker
```

Миграции выполняет API перед стартом, worker ждёт его healthcheck. Один сессионный advisory lock допускает только один worker. Unique active slot в БД исключает второй незавершённый job, включая paused. Результаты и checkpoints находятся только в private PostgreSQL volume. Браузерный временный профиль находится в контейнерном **tmpfs `/tmp`**, не на диске. Экспорт не использует даже tmpfs: `BytesIO` + XlsxWriter `in_memory=True`.

База Docker — Python 3.12 slim; устанавливается только Chromium Headless Shell, совместимый с закреплённым `playwright` из `requirements.lock`. Системные зависимости и браузер вынесены в отдельные кэшируемые слои. Для нестабильного CDN предусмотрен build argument `PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000`; необязательный `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST` поддерживает доверенный локальный кэш официального архива. По умолчанию используется официальный CDN. Приложение работает отдельным непривилегированным пользователем; схема установки следует [документации Playwright](https://playwright.dev/python/docs/browsers).

Из Windows для default loopback bind:

```powershell
ssh -N -L 8089:127.0.0.1:8089 main@aistation
```

Открыть `http://localhost:8089`, войти и нажать «Начать сбор». После закрытия вкладки worker продолжает. В основном режиме нужен один GET карточки на компанию плюс страницы каталога: для объёма около 37 тысяч компаний паузы 7–12 секунд означают примерно 3–6 суток без учёта повторов и сетевых задержек. ETA появляется после пяти обработанных организаций и является приблизительным.

## Обновление без потери прогресса

Сначала нажать **«Пауза»** и дождаться отображения статуса. Сохранить резервную копию структурированных данных перед миграциями в защищённом каталоге оператора; готовые Excel на сервер не записываются:

```bash
umask 077
docker compose --env-file .env.example --env-file .env exec -T leaders-parser-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > leaders-parser-before-update.dump
git rev-parse HEAD
git pull --ff-only origin main
docker compose --env-file .env.example --env-file .env build
docker compose --env-file .env.example --env-file .env up -d --wait --wait-timeout 180
docker compose --env-file .env.example --env-file .env ps
curl --fail http://127.0.0.1:8089/ready
docker compose --env-file .env.example --env-file .env logs --tail=100 leaders-parser-worker
```

Затем нажать «Продолжить». При неожиданном рестарте runnable-задание восстанавливается автоматически; paused остаётся paused. SIGTERM прерывает паузы/cooldown, активному запросу даётся ограниченный deadline; `stop_grace_period=90s`. Для rollback кода использовать предыдущий commit и пересобрать образ. Совместимость схемы проверять до rollback; destructive Alembic downgrade не запускать автоматически. Backup защищать и удалять по правилам владельца данных. **Не использовать `docker compose down -v` при обновлениях.**

## Статусы, ошибки и жизненный цикл

`queued`, `discovering`, `processing`, `paused`, `cooldown`, `completed`, `completed_with_errors`, `failed`, `cancelled`. Отсутствующее поле (`missing`) отличается от непроверенной страницы (`pending`), ошибки (`error`) и HTTP 404 (`not_found`). При изменении HTML, 403 или CAPTCHA worker ставит задание на паузу. Первый 429 включает глобальный durable cooldown с `Retry-After`; повторный 429 требует ручного продолжения. Ошибки карточек после bounded retries не стирают предыдущие результаты.

Полностью зелёный статус — только после индексации и обработки всего списка без открытых ошибок. «Повторить ошибки» сбрасывает только ошибочные операции. Одинаковый URL не дублируется; разные карточки с одинаковым ИНН сохраняются и отмечаются конфликтом. Остановленное задание сохраняет частичные данные и экспорт. Новое задание можно начать после завершения/остановки предыдущего.

Кнопка «Удалить данные задания» доступна только для завершённого или остановленного задания. Worker автоматически очищает терминальные задания через 7 суток; активные и paused не удаляются. Скачивание не удаляет данные. Логи идут в stdout/stderr с Docker rotation 10 МБ × 5 файлов; HTML и персональные контакты не журналируются.

## API

Все `/api/*` требуют Basic Auth. Для мутаций также нужен `X-Requested-With: LeadersParser`, а при наличии `Origin` он должен совпадать с origin приложения.

| Метод | Путь | Результат |
|---|---|---|
| POST | `/api/jobs` | Единственное активное задание |
| GET | `/api/jobs/latest` | Последнее задание для восстановления вкладки |
| GET | `/api/jobs/{id}` | Счётчики, этап, ETA, события |
| POST | `/api/jobs/{id}/pause`, `/resume`, `/cancel`, `/retry-failed` | Управление |
| GET | `/api/jobs/{id}/companies?page=1&size=25&q=&errors_only=false` | Страница, максимум 100 записей |
| GET | `/api/jobs/{id}/download` | Новый XLSX в RAM, включая частичные данные |
| DELETE | `/api/jobs/{id}` | Каскадное удаление терминального задания |
| GET | `/health`, `/ready` | Liveness / доступность схемы БД |

Архивное задание можно открыть по `http://localhost:8089/#UUID`; повторное скачивание доступно по его API URL до удаления. Границы слоёв и проверенные сценарии описаны в [architecture.md](docs/architecture.md).
