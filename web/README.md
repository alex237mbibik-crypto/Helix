# Sheets Hub Web

Веб-версия Sheets Hub: календарь записи из **таблиц Nextcloud** (`.xlsx` через WebDAV).

## Запуск

```bash
cd web
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m sheets_hub
```

Или `run.sh` / `run.bat`.

Откроется **http://127.0.0.1:8787/**

## Вход в Nextcloud

1. В Nextcloud: аватар → **Настройки → Безопасность → Создать пароль приложения**
2. Скопируйте `credentials.example.json` → `credentials.json` и заполните:

```json
{
  "type": "nextcloud",
  "url": "https://ваш-nextcloud",
  "username": "логин",
  "app_password": "пароль-приложения"
}
```

3. Либо в интерфейсе: **Ctrl+Shift+T** → **JSON входа**

Календари — обычные `.xlsx` в Files (можно открывать в Nextcloud Office).  
В настройках указывайте путь вида `Calendars/Clinic.xlsx`, лист — имя вкладки.

## Сеть (другие ПК в LAN)

```bash
SHEETS_HUB_HOST=0.0.0.0 SHEETS_HUB_PORT=8787 python -m sheets_hub
```

Отдельного логина нет — не выставляйте в интернет без защиты.

## Переменные

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `SHEETS_HUB_HOST` | `127.0.0.1` | хост |
| `SHEETS_HUB_PORT` | `8787` | порт |
| `SHEETS_HUB_NO_BROWSER` | — | `1` = не открывать браузер |
