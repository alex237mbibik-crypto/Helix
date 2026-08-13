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

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Создайте проект → **APIs & Services → Enable APIs** → включите **Google Sheets API**.
3. **IAM & Admin → Service Accounts → Create**.
4. У аккаунта откройте ключ **JSON**.
5. В программе нажмите **Таблицы → Выбрать JSON-ключ…** и укажите этот файл. Ключ сохранится рядом с программой как `credentials.json`.
6. В JSON есть поле `client_email` — вида `sheets-hub@....iam.gserviceaccount.com`. Его же показывает окно **Таблицы**.
7. Каждую нужную таблицу откройте в Google → **Настройки доступа** → добавьте этот email как **Редактор**.

Без шага 6 API таблицу не увидит.

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

Готовый файл: `dist\SheetsHub\SheetsHub.exe`. Рядом с ним положите `config.yaml` и `credentials.json`.

## Важно

- Сервисный ключ — это полный доступ к расшаренным таблицам. Не отправляйте `credentials.json` в чат и не коммитьте его.
- Если таблиц много, Google может ненадолго ограничить частоту запросов. Тогда подождите минуту и нажмите «Обновить всё».
- Двойной клик пишет в **исходную** таблицу и строку. Кнопки «Добавить» и «Внести в назначение» пишут в таблицу из списка **destinations**.
- В таблице-назначении в первой строке должны быть заголовки колонок.
