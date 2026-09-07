# Sheets Hub

Веб-приложение (и при необходимости Windows-окно), которое читает **несколько Google Таблиц**, показывает календарь записи и умеет **вносить данные** обратно в таблицу.

По умолчанию из исходников открывается **браузер** на `http://127.0.0.1:8787/`. Собранный `SheetsHub.exe` по-прежнему стартует как desktop-окно.

## Что умеет

- Читать любое число таблиц-источников по ссылке или ID
- Отдельно указать таблицы, **куда вносить** новые строки
- Разбивать одну исходную строку на несколько пунктов **по типу услуги и адресу**
- Подбирать назначение по услуге и адресу
- Показывать календарь записи в браузере
- Искать по имени, дате или телефону и фильтровать по цепочке название → услуга → город → адрес
- Записывать / освобождать слоты с защитой «не записывать» и маркером «записывают»
- Общий список таблиц в облачном реестре для всех операторов
- Запоминать список таблиц в `config.yaml` или в настройках

Если колонки в таблицах называются по-разному, в конфиге можно задать `map` — внутри программы поля станут одинаковыми.

## Запуск (веб)

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m sheets_hub
```

Откроется браузер: **http://127.0.0.1:8787/**  
Ключ `credentials.json` — через настройки (**JSON ключа**) или положите файл в каталог данных приложения.

Переменные окружения:

| Переменная | Значение |
|---|---|
| `SHEETS_HUB_UI` | `web` (по умолчанию), `desktop`, `ctk` |
| `SHEETS_HUB_HOST` | хост сервера, по умолчанию `127.0.0.1` (для LAN: `0.0.0.0`) |
| `SHEETS_HUB_PORT` | порт, по умолчанию `8787` |
| `SHEETS_HUB_NO_BROWSER` | `1` — не открывать браузер сам |

Доступ из сети (осторожно — без отдельного логина):

```bash
SHEETS_HUB_HOST=0.0.0.0 SHEETS_HUB_PORT=8787 python -m sheets_hub
```

## Установка на Windows (desktop exe)

1. Поставьте [Python 3.11+](https://www.python.org/downloads/) и отметьте **Add python.exe to PATH**.
2. Скопируйте папку проекта на компьютер.
3. Дважды нажмите `run.bat` — окружение и зависимости подтянутся сами.

Вручную:

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m sheets_hub
```

Desktop-окно без браузера:

```bat
set SHEETS_HUB_UI=desktop
.venv\Scripts\python -m sheets_hub
```

## Доступ к Google Таблицам

Используется **один сервисный аккаунт** (одна почта вида `…@….iam.gserviceaccount.com`).

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Создайте проект → **APIs & Services → Enable APIs** → включите **Google Sheets API**.
3. **Credentials → Create credentials → Service account** → создайте аккаунт → **Keys → Add key → JSON**.
4. Сохраните файл как `credentials.json` рядом с программой (или **Таблицы → JSON…**).
5. Каждую рабочую таблицу откройте для `client_email` из JSON с ролью **Редактор**.
6. Создайте ещё одну пустую Google-таблицу для **общего списка** (например «SheetsHub список»), откройте её для того же email как Редактор. В программе: **Таблицы** → поле «Общий список» → вставьте ссылку → сохраните. Лист `SheetsHub` создастся сам.

Операторам вход через браузер не нужен: все пишут от имени этой одной почты. Список таблиц подтягивается из общего реестра на всех ПК (при старте и каждые 30 с).

`credentials.json` не коммитьте.
## Конфиг

Скопируйте пример и подставьте свои таблицы:

```bash
copy config.example.yaml config.yaml
```

```yaml
credentials: credentials.json

sources:
  - name: Магазин 1
    spreadsheet_id: https://docs.google.com/spreadsheets/d/ID/edit
    sheet: Лист1

  - name: Магазин 2
    spreadsheet_id: ДРУГОЙ_ID
    sheet: Заказы
    map:
      name: Клиент
      amount: Сумма
      status: Статус

destinations:
  - name: Сводная
    spreadsheet_id: https://docs.google.com/spreadsheets/d/ID/edit
    sheet: Лист1
```

Ссылки на таблицы в код не зашиты: их вводите вы — в `config.yaml` или в окне программы, кнопка **Таблицы**.

`credentials.json` и `config.yaml` в git не попадают.

## Сборка .exe

Через GitHub Actions (рекомендуется): откройте [Actions → Build Windows](https://github.com/alex237mbibik-crypto/Helix/actions) → **Run workflow**. Готовый архив `SheetsHub-Windows` скачивается во вкладке прогона.

Локально на Windows, после того как `run.bat` хотя бы раз отработал:

```bat
build_exe.bat
```

Готовый файл: `dist\SheetsHub\SheetsHub.exe`. Нужна **вся папка** `SheetsHub` вместе с `_internal` (один `.exe` без неё не запустится).

В CI можно положить `credentials.json` в архив через GitHub Secret `SHEETS_HUB_CREDENTIALS_JSON`. Если секрета нет — положите SA JSON рядом с exe вручную. Локально: скопируйте JSON в `packaging/bundled_credentials.json` перед `build_exe.bat`.


## Важно

- `credentials.json` — секрет. Не отправляйте его в чат и не коммитьте.
- Если таблиц много, Google может ненадолго ограничить частоту запросов. Тогда подождите минуту и нажмите «Обновить всё».
- Двойной клик пишет в **исходную** таблицу и строку. Кнопки «Добавить» и «Внести в назначение» пишут в таблицу из списка **destinations**.
- В таблице-назначении в первой строке должны быть заголовки колонок.
