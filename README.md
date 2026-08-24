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

Рекомендуемый способ — **вход вашим Google-аккаунтом** (OAuth Desktop). Тогда запись идёт от вашего email, без расшаривания на service account.

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Создайте проект → **APIs & Services → Enable APIs** → включите **Google Sheets API**.
3. **APIs & Services → OAuth consent screen** — тип External (или Internal), добавьте себя в **Test users**, если приложение в режиме Testing.
4. **Credentials → Create credentials → OAuth client ID** → тип **Desktop app** → скачайте JSON.
5. В программе: **Таблицы → Выбрать JSON…** → этот файл → операторы потом только вводят свою почту.
6. Войдите тем аккаунтом, у которого уже есть доступ к таблицам (например Editor).

Рядом с программой появятся `credentials.json` (клиент) и `token.json` (вход оператора). Их не коммитьте.

**Для операторов:** достаточно ввести свою Google-почту и один раз нажать «Разрешить» в браузере. JSON и Google Cloud им не нужны — файл приложения кладёт администратор один раз на ПК.

### Старый способ (service account)

По-прежнему можно указать JSON сервисного аккаунта. Тогда каждую таблицу нужно открыть для email вида `…@….iam.gserviceaccount.com` с ролью **Редактор**.

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

В CI `credentials.json` кладётся в архив из GitHub Secret `SHEETS_HUB_OAUTH_JSON` (Settings → Secrets → Actions). Локально: скопируйте свой OAuth JSON в `packaging/bundled_credentials.json` перед `build_exe.bat`.


## Важно

- `credentials.json` и `token.json` — секреты. Не отправляйте их в чат и не коммитьте.
- Если таблиц много, Google может ненадолго ограничить частоту запросов. Тогда подождите минуту и нажмите «Обновить всё».
- Двойной клик пишет в **исходную** таблицу и строку. Кнопки «Добавить» и «Внести в назначение» пишут в таблицу из списка **destinations**.
- В таблице-назначении в первой строке должны быть заголовки колонок.
