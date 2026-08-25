# Sheets Hub

Windows-программа, которая читает **несколько Google Таблиц-источников**, сводит строки в одно окно и умеет **вносить данные в таблицу-назначение**.

Работает и на этой машине (macOS) для проверки, целевой запуск — Windows: `run.bat` или собранный `SheetsHub.exe`.

## Что умеет

- Читать любое число таблиц-источников по ссылке или ID
- Отдельно указать таблицы, **куда вносить** новые строки
- Разбивать одну исходную строку на несколько пунктов **по типу услуги и адресу**
- Подбирать назначение по услуге и адресу
- Показывать все строки в одном окне, с колонкой «Источник»
- Искать по любой колонке и фильтровать по дате
- Двойным кликом править ячейку в исходной таблице
- Копировать выбранную строку в назначение
- Добавлять новую строку в назначение
- Экспортировать видимые строки в CSV
- Запоминать список таблиц в `config.yaml` или в окне **Таблицы**

Если колонки в таблицах называются по-разному, в конфиге можно задать `map` — внутри программы поля станут одинаковыми.

## Установка на Windows

1. Поставьте [Python 3.11+](https://www.python.org/downloads/) и отметьте **Add python.exe to PATH**.
2. Скопируйте папку проекта на компьютер.
3. Дважды нажмите `run.bat` — окружение и зависимости подтянутся сами.

Вручную:

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m sheets_hub
```

На macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m sheets_hub
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
