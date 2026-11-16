<!-- docs/upgrade-kpp-revenue.md -->
# Обновление работающего сбора: КПП, выручка, доступ без пароля

**Текущее задание сохраняется.** Обновление добавляет миграцию `0002`; PostgreSQL и её volume остаются на месте. Основной `next_url`, найденные компании, их порядок, ФИО, контакты и счётчики сохраняются.

Нужно поставить сбор **на паузу**, затем штатно остановить worker перед миграцией. Кнопка «Остановить» отменяет задание — для обновления она не нужна. API кратко останавливается после сборки образа. PostgreSQL остаётся запущенной всё время.

## 1. Windows: проверки, commit/push, передача вспомогательного скрипта

```powershell
cd C:\Users\i.mein\PycharmProjects\Leaders-Project-Parser
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.cache\ms-playwright"
$env:TEST_BROWSER = '1'
.\.venv\Scripts\python -m pytest -q -p no:cacheprovider
node --test tests/test_ui.mjs
git diff --check
git status --short
git add app tests scripts docs .env.example compose.yaml README.md
git commit -m "Add KPP and revenue with resumable backfill; remove web authentication"
git push origin main
scp .\scripts\deploy_checkpoint.py main@aistation:/tmp/leaders-deploy-checkpoint.py
```

PostgreSQL-тесты включаются только с `TEST_DATABASE_URL` на отдельную одноразовую базу **`leaders_test`**. Не указывайте рабочую базу. Если Windows не разрешает стандартный каталог pytest, добавьте `--basetemp="$PWD\.cache\pytest-deploy-$(Get-Date -Format yyyyMMddHHmmss)"`.

Скрипт в `/tmp` передаётся заранее: его можно выполнить внутри **старого** API-образа до `git pull`. Он использует существующий доступ контейнера к БД, не требует знания старого пароля UI и не создаёт задания.

## 2. Ubuntu: найти проект и поставить именно текущее задание на паузу

Все команды ниже выполняются **в одной SSH-сессии**:

```bash
set -euo pipefail
API_CID=$(docker ps -q \
  --filter label=com.docker.compose.project=leaders-project-parser \
  --filter label=com.docker.compose.service=leaders-parser-api)
test -n "$API_CID"
PROJECT_DIR=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$API_CID")
cd "$PROJECT_DIR"
test -f compose.yaml && test -f .env
dc=(docker compose --env-file .env.example --env-file .env)
JOB_ID=$("${dc[@]}" exec -T leaders-parser-api python - pause --print-id < /tmp/leaders-deploy-checkpoint.py)
printf 'Сохранён JOB_ID=%s\n' "$JOB_ID"
"${dc[@]}" stop -t 90 leaders-parser-worker
```

Теперь worker остановлен, активный запрос завершён или прерван в пределах штатного deadline. Данные и cursor могли успеть корректно сохраниться до остановки, поэтому baseline фиксируется **после** неё.

## 3. Резервная копия и baseline

```bash
umask 077
BACKUP_DIR="$HOME/backups/leaders-parser/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
"${dc[@]}" exec -T leaders-parser-api python - snapshot \
  --job-id "$JOB_ID" --require-paused \
  < /tmp/leaders-deploy-checkpoint.py > "$BACKUP_DIR/job-before.json"
cp .env "$BACKUP_DIR/env-before"
git rev-parse HEAD > "$BACKUP_DIR/commit-before.txt"
"${dc[@]}" exec -T leaders-parser-postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/postgres.dump.partial"
"${dc[@]}" exec -T leaders-parser-postgres pg_restore --list \
  < "$BACKUP_DIR/postgres.dump.partial" > /dev/null
mv "$BACKUP_DIR/postgres.dump.partial" "$BACKUP_DIR/postgres.dump"
printf 'Резервная копия: %s\n' "$BACKUP_DIR"
```

`pg_restore --list` только проверяет архив, ничего не восстанавливает. Backup содержит структурированные данные и хранится вне Git с закрытыми правами.

## 4. Обновить код, собрать образ, применить расширяющую миграцию

```bash
git pull --ff-only origin main
"${dc[@]}" build
"${dc[@]}" stop -t 90 leaders-parser-api
python3 - <<'PY'
from pathlib import Path
path = Path('.env')
updates = {'APP_BIND_IP': '192.168.55.3', 'APP_PORT': '8089'}
output = []
for line in path.read_text(encoding='utf-8-sig').splitlines(keepends=True):
    key = line.split('=', 1)[0].strip().removeprefix('export ').upper()
    if key in {'APP_USERNAME', 'APP_PASSWORD'}:
        continue
    if key in updates:
        continue
    output.append(line)
text = ''.join(output)
if text and not text.endswith('\n'):
    text += '\n'
text += ''.join(f'{key}={value}\n' for key, value in updates.items())
path.write_text(text, encoding='utf-8')
PY
"${dc[@]}" config --quiet
"${dc[@]}" run --rm --no-deps leaders-parser-api alembic upgrade head
"${dc[@]}" up -d --no-deps --no-build --wait --wait-timeout 180 leaders-parser-api
"${dc[@]}" exec -T leaders-parser-api alembic current
"${dc[@]}" exec -T leaders-parser-api alembic check
```

Редактирование `.env` удаляет только настройки входа и задаёт запрошенный адрес приложения. `POSTGRES_*` и `DATABASE_URL` сохраняются. `--no-deps` не пересоздаёт PostgreSQL. Миграция добавляет колонки и отдельную таблицу checkpoint дозаполнения; исходная миграция `0001` не изменена.

## 5. Проверить сохранённый прогресс до запуска worker

```bash
API_URL='http://192.168.55.3:8089'
curl --fail "$API_URL/health"
curl --fail "$API_URL/ready"
python3 - "$BACKUP_DIR/job-before.json" "$API_URL" "$JOB_ID" <<'PY'
import json, sys, urllib.request
with open(sys.argv[1], encoding='utf-8') as stream:
    before = json.load(stream)
with urllib.request.urlopen(f'{sys.argv[2]}/api/jobs/{sys.argv[3]}', timeout=15) as response:
    after = json.load(response)
for key in ('id', 'status', 'stage', 'next_url', 'pages', 'found', 'processed',
            'pending', 'discovery_complete', 'expected_total'):
    assert after[key] == before[key], f'Изменилось поле {key}'
assert after['status'] == 'paused'
print('Прогресс сохранён:', after['id'], 'страниц:', after['pages'],
      'найдено:', after['found'], 'обработано:', after['processed'])
print('Организаций с новыми непроверенными полями:', after['fields_pending'])
PY
```

UI, API и скачивание теперь доступны без логина/пароля. Если сравнение не прошло, оставьте worker остановленным и проверьте логи API и backup; не создавайте новое задание и не выполняйте сброс БД.

## 6. Запустить worker и продолжить тот же UUID

```bash
"${dc[@]}" up -d --no-deps --no-build --wait --wait-timeout 180 leaders-parser-worker
curl --fail -X POST -H 'X-Requested-With: LeadersParser' \
  "$API_URL/api/jobs/$JOB_ID/resume"
curl --fail "$API_URL/api/jobs/$JOB_ID"
"${dc[@]}" ps
"${dc[@]}" logs --tail=100 leaders-parser-api leaders-parser-worker
```

Альтернатива последнему POST — открыть `http://192.168.55.3:8089/#UUID` с сохранённым UUID и нажать **«Продолжить»**. **«Начать сбор» нажимать не нужно.**

Основная индексация продолжится с сохранённого cursor. После её окончания отдельный проход `backfill_revenue` обновит выручку старых записей. Он может читать каталог с первой страницы, но не создаёт компании, не меняет их порядок, счётчики основного индекса и его `next_url`; у него собственный cursor. Проход прекращается, когда все старые суммы проверены или каталог закончен. Исчезнувшие из каталога компании отражаются в ошибках, а не считаются нулевой выручкой.

Обычная обработка ещё не открытых карточек сразу сохраняет КПП. Этап `backfill_kpp` затем дозаполняет КПП уже обработанных карточек, не перезаписывая ФИО и контакты. Все дополнительные GET проходят через существующий limiter. После рестарта оба механизма продолжаются по сохранённому состоянию.

Для исторического завершённого задания есть кнопка **«Дозаполнить КПП и выручку»** (`POST /api/jobs/{id}/backfill-fields`): она использует тот же UUID, не создавая новый сбор. Для текущего незавершённого задания достаточно обычного resume.

Не удаляйте volumes и не запускайте destructive downgrade. При откате старый образ не знает ревизию `0002`; простой запуск его штатной команды миграции не является корректным rollback. До восстановления сервиса оставляйте задание paused и используйте сохранённые backup/commit для диагностики.
