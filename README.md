# smsverbot-mcp

[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-published-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.MassimoSun/smsverbot-mcp)

**SMS verification for AI agents.** An MCP server that lets an agent buy a real phone number, wait for the confirmation code, and cancel the order with a refund if the code never arrives. Remote streamable-HTTP endpoint, prepaid balance, pay-as-you-go — no subscriptions.

Works with Claude Code, Cursor, VS Code, Codex, Hermes and any MCP client. Keywords: MCP server, model context protocol, SMS verification, SMS activation, virtual numbers, AI agents, phone verification.

- Endpoint: `https://kremledroid.duckdns.org/mcp`
- Tools: `list_services`, `list_countries`, `get_price`, `buy_number`, `wait_for_code`, `cancel_order`, `get_balance`, `order_history`, `whoami`

## Step 1 — get your key in the bot

Open **[@Smsverbot](https://t.me/Smsverbot)** in Telegram, press **🤖 MCP для агентов** (or send `/api_key`). The bot gives you the API key and shows your balance. **The balance is shared with the bot**: top up once — the same money pays for both numbers bought in the chat and numbers bought by your agent. No separate account, no card, no crypto wallet needed for MCP.

## Step 2 — try the free sandbox first

Press **🧪 Тестовый режим** in the same menu. A sandbox key buys fake numbers (`+10000000000`) and returns the fixed code `123456` after ~4 seconds, costs nothing, and never touches real providers. Build and debug your integration for free, then switch to a real key when you are ready.

## Step 3 — connect

```bash
claude mcp add --transport http smsverbot https://kremledroid.duckdns.org/mcp
```

---

MCP-сервер, который продаёт AI-агентам номера для приёма SMS-кодов подтверждения. Это отдельный продукт, не связанный с Telegram-ботом. Работает без собственного UI и веб-фреймворка: агент подключается по stdio или streamable HTTP и вызывает инструменты MCP.

Деньги хранятся в целых копейках. Баланс предоплаченный: агент пополняет счёт, покупка номера списывает стоимость, отмена возвращает её ровно один раз.

## Подключение за 30 секунд (хостед, без установки)

Эндпоинт: `https://kremledroid.duckdns.org/mcp` (streamable HTTP, TLS).

Claude Code:
```bash
claude mcp add --transport http smsverbot https://kremledroid.duckdns.org/mcp
```

Cursor (`~/.cursor/mcp.json`):
```json
{ "mcpServers": { "smsverbot": { "url": "https://kremledroid.duckdns.org/mcp" } } }
```

VS Code (`.vscode/mcp.json`):
```json
{ "servers": { "smsverbot": { "type": "http", "url": "https://kremledroid.duckdns.org/mcp" } } }
```

Дальше: получить API-ключ у Telegram-бота @Smsverbot, положить баланс и передать ключ одним из двух способов — заголовком `Authorization: Bearer <ключ>` в конфиге клиента (рекомендуется для хостед-режима) или первым аргументом `api_key` любого инструмента. Проверка, что ключ принят: инструмент `whoami` — отдаёт `key_id` и баланс.

## Возможности

- 9 MCP-инструментов для каталога, цен, покупки, ожидания кода, отмены, баланса, истории и проверки ключа.
- Провайдер-агностичный слой: детерминированный `mock` без сети и реальный адаптер `fivesim`.
- SQLite с WAL и транзакциями `BEGIN IMMEDIATE` для всех операций с балансом.
- Ключи API хранятся только как SHA-256; открытый ключ показывается один раз.
- Идемпотентный возврат средств при отмене.
- Ограничения частоты и дневной лимит на ключ.

## Установка

Windows (PowerShell или cmd):

```bat
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

Linux/macOS:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Требуется Python 3.11+. Проект проверялся на Python 3.14.

## Настройка

Скопируйте `.env.example` в `.env` и заполните значения. Файл `.env` читается автоматически, переменные окружения имеют приоритет.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `MCP_DB` | `./mcp.db` | Путь к SQLite-базе |
| `MCP_API_KEY` | пусто | Ключ по умолчанию для stdio-клиентов, если `api_key` передан пустым |
| `PROVIDER` | `mock` | `mock` или `fivesim` |
| `FIVESIM_TOKEN` | пусто | Токен 5sim, обязателен для `fivesim` |
| `MCP_MARKUP` | `2.5` | Наценка к закупочной цене |
| `RATE_LIMIT_PER_15MIN` | `20` | Лимит покупок на ключ за 15 минут |
| `DAILY_CODE_LIMIT` | `200` | Дневной лимит заявок на ключ (UTC) |
| `CODE_WAIT_SECONDS` | `120` | Максимальное ожидание SMS-кода |
| `USD_KOP_RATE` | `9200` | Курс USD→RUB в копейках за доллар |

Секретов в репозитории нет. Не коммитьте `.env` и реальные токены.

## Запуск

Mock-режим не делает сетевых запросов и подходит для тестов и локальной разработки:

```bat
.venv/Scripts/python server.py --stdio
```

Live-режим включается только явно:

```bat
set PROVIDER=fivesim
set FIVESIM_TOKEN=your_token_here
.venv/Scripts/python server.py --stdio
```

HTTP-транспорт (streamable HTTP):

```bat
.venv/Scripts/python server.py --http --port 8765
```

По умолчанию HTTP слушает `127.0.0.1`; хост можно изменить через `--host`.

### Ключи и пополнение

Открытый API-ключ показывается только при создании. В базе остаётся лишь его SHA-256.

```bat
.venv/Scripts/python server.py --create-key "my-agent"
.venv/Scripts/python server.py --topup svb_ВАШ_КЛЮЧ 500
```

Вторая команда пополняет баланс на 500 рублей (50000 копеек).

## Админ: выдача ключей

`admin.py` читает путь к базе из `MCP_DB` (по умолчанию `./mcp.db`).

Создать ключ с меткой и сразу пополнить баланс на 500 рублей:

```bat
python admin.py create --label client1 --topup 500
```

Показать все ключи (ID, метка, баланс в рублях):

```bat
python admin.py list
```

Пополнить баланс ключа на 200 рублей:

```bat
python admin.py topup --key-id ID --amount 200
```

Показать последние заказы ключа:

```bat
python admin.py orders --key-id ID
```

Открытый API-ключ показывается только один раз при создании.

## Подключение MCP-клиентов

Во всех примерах замените путь на абсолютный путь к `server.py`. Для live-режима добавьте `PROVIDER` и `FIVESIM_TOKEN` в блок `env`.

### Claude Desktop

Файл `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "smsverbot": {
      "command": "C:/path/to/.venv/Scripts/python.exe",
      "args": ["C:/path/to/smsverbot-mcp/server.py", "--stdio"],
      "env": {
        "MCP_DB": "C:/path/to/smsverbot-mcp/mcp.db",
        "PROVIDER": "mock"
      }
    }
  }
}
```

### Cursor

Файл `.cursor/mcp.json` (или глобальный `~/.cursor/mcp.json`) использует ту же схему `mcpServers`, что и Claude Desktop.

### Claude Code

```bash
claude mcp add smsverbot -- python server.py --stdio
```

Публичный HTTP-сервер:

```bash
claude mcp add --transport http smsverbot https://kremledroid.duckdns.org/mcp
```

Для отдельной базы и live-провайдера передайте переменные окружения в оболочке перед запуском команды.

### Hermes и другие MCP-клиенты

Зарегистрируйте stdio-сервер с командой `python` и аргументами `server.py --stdio`, либо подключите HTTP-транспорт по адресу `http://127.0.0.1:8765/mcp`. Передайте `MCP_DB` и при необходимости `PROVIDER`/`FIVESIM_TOKEN` через `env`.

## Инструменты

Все инструменты принимают `api_key` первым аргументом. Если передать пустую строку, используется `MCP_API_KEY` из окружения. Успех: `{"ok": true, ...}`. Ошибка: `{"ok": false, "error": "CODE", "message": "..."}`. Коды ошибок: `UNAUTHORIZED`, `INSUFFICIENT_FUNDS`, `RATE_LIMITED`, `DAILY_LIMIT`, `ORDER_NOT_FOUND`, `NO_NUMBERS`, `UNAVAILABLE`, `UPSTREAM_ERROR`, `INTERNAL`.

| Инструмент | Описание |
| --- | --- |
| `list_services(country="indonesia")` | Список доступных сервисов |
| `list_countries` | Список доступных стран |
| `get_price(service, country)` | Розничная цена без покупки и списания |
| `buy_number(service, country)` | Купить номер и списать баланс |
| `wait_for_code(order_id)` | Дождаться SMS-кода по заказу |
| `cancel_order(order_id)` | Отменить заказ и вернуть деньги один раз |
| `get_balance()` | Текущий баланс в копейках и рублях |
| `order_history(limit=10)` | Последние заказы ключа |
| `whoami` | Проверить ключ: `key_id` и баланс в рублях |

Чтение каталога, цен, баланса и истории никогда не списывает деньги.

## Биллинг

- Все суммы хранятся и списываются в целых копейках; денежные операции не используют float.
- Закупочная цена `cost_kop` берётся у провайдера.
- Розничная цена: `price_kop = max(ceil(cost_kop * MCP_MARKUP), cost_kop + 1000)`, затем округление вверх до 100 копеек.
- Покупка: сначала создаётся заказ в статусе `waiting`, затем баланс списывается атомарно. При нехватке средств заказ отменяется у провайдера и помечается `cancelled`.
- `wait_for_code` только опрашивает провайдера. Таймаут переводит ответ в `pending`, деньги не возвращаются.
- Отмена возвращает полную розничную цену, если заказ был в статусе `waiting`/`open`. Повторная отмена возвращает `refunded_kop: 0`.
- Каждое изменение баланса фиксируется в таблице `ledger`: `topup`, `debit`, `refund`.

## Лимиты

- `RATE_LIMIT_PER_15MIN` — скользящее окно на ключ, по умолчанию 20 запросов покупки за 15 минут.
- `DAILY_CODE_LIMIT` — число незавершённых отменой заказов на ключ за сутки UTC, по умолчанию 200.
- `CODE_WAIT_SECONDS` — максимум ожидания кода, по умолчанию 120 секунд; опрос каждые 2 секунды.
- Таймаут ожидания не освобождает номер и не возвращает деньги; для возврата вызовите `cancel_order`.

## AUP: допустимое и запрещённое использование

Разрешено:

- легальная регистрация и подтверждение собственных аккаунтов;
- автоматизация тестирования собственных продуктов;
- сценарии, где использование временного номера не нарушает правила сервиса и законодательство.

Запрещено:

- мошенничество, спам, обход банов и массовое создание фейковых аккаунтов;
- покупка номеров для преступлений или нарушения законов;
- перепродажа доступа способом, скрывающим конечного пользователя от проверки;
- использование сервиса лицами младше установленного законом возраста.

Оператор вправе заблокировать ключ и отказать в обслуживании при нарушении AUP. Пользователь обязан соблюдать правила целевых сервисов, включая их ограничения на виртуальные номера.

## Проверка

```bat
.venv/Scripts/python -m pytest -q tests/
.venv/Scripts/python -m compileall -q .
```

Тест `tests/test_mcp_client.py` запускает сервер по stdio через официальный MCP-клиент, проверяет ровно 9 инструментов и читает баланс из общей тестовой базы.

## Структура

- `config.py` — настройки из окружения.
- `store.py` — SQLite, ключи, заказы, ledger, лимиты.
- `providers/` — протокол, mock- и 5sim-адаптеры.
- `service.py` — бизнес-логика цен, покупки, ожидания и отмены.
- `server.py` — FastMCP, 9 инструментов и CLI.
- `admin.py`, `admin_service.py` — админ-CLI и операции с ключами в рублях.
- `tests/` — сервисные тесты и интеграционный тест MCP-клиента.
