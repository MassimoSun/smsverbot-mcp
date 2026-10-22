# Каталоги MCP: что готово и что нужно от владельца

Публичный эндпоинт: `https://kremledroid.duckdns.org/mcp` (streamable HTTP, TLS Let's Encrypt до 18.01.2027).

## Уже в репозитории

| Файл | Для чего |
|---|---|
| `server.json` | официальный реестр MCP (registry.modelcontextprotocol.io) |
| `smithery.yaml` | Smithery (startCommand + configSchema) |
| `README.md` | установка в одну строку для Claude Code / Cursor / Hermes |

## Блокер

Все каталоги индексируют **публичный GitHub-репозиторий**. Сейчас код лежит в приватном bare-репо на VPS.
Нужно: создать на GitHub пустой репозиторий `smsverbot-mcp` (без README) и я запушу. Для каталогов репозиторий должен быть **публичным** — код без секретов, `.env` и `*.db` в игноре, README без внутренних деталей.

## Порядок публикации

1. ✅ **Official MCP Registry** — опубликовано 20.10.2026: `io.github.MassimoSun/smsverbot-mcp` v0.1.0, статус `active`, remote streamable-http. Проверка: `curl "https://registry.modelcontextprotocol.io/v0/servers?search=smsverbot"`. Повторная публикация: `mcp-publisher publish` (бинарь в `C:\Users\Professional\.local\bin\mcp-publisher.exe`, авторизация через device flow — токен уже сохранён).
2. **GitHub** — ✅ публичный `MassimoSun/smsverbot-mcp`, ветка `main`.
3. **Glama** — glama.ai/mcp/servers → Add Server → GitHub URL (аккаунт зарегистрирован 20.10).
4. **Smithery** — smithery.ai → Add server → GitHub URL (аккаунт зарегистрирован 20.10). Для Python-сервера может понадобиться remote-режим с URL эндпоинта, а не stdio.
5. **mcp.so** — mcp.so/submit → GitHub repo URL (аккаунт зарегистрирован 20.10).
6. **awesome-mcp-servers** — PR в `punkpeye/awesome-mcp-servers`, строка ниже.

Старое описание шагов (для истории): имя в реестре обязано совпадать с GitHub-логином по регистру — `io.github.MassimoSun/...`, иначе 403. Описание ≤ 100 символов. Пакет PyPI указывать нельзя, пока он не опубликован.

## Текст карточки (одинаковый везде)

**Name:** Smsverbot — SMS verification for AI agents
**Short:** Buy a real phone number, wait for the SMS code, cancel with automatic refund. 153 countries, 2500+ services, prepaid RUB balance, pay-as-you-go.
**Tools (9):** list_services, list_countries, get_price, buy_number, wait_for_code, cancel_order, get_balance, order_history, whoami
**Transport:** streamable HTTP `https://kremledroid.duckdns.org/mcp`, plus stdio (`python server.py --stdio`)
**Auth:** API key — either `Authorization: Bearer <key>` header (hosted mode) or the first `api_key` argument; issued in the Telegram bot, balance top-up there too
**Tags:** sms, otp, verification, phone-number, virtual-number, telegram, whatsapp, agents

## Чего в каталогах быть не должно

- источник номеров (5sim/Tiger) — только «real carrier SIMs via partner providers»;
- внутренние цены закупки и наценка;
- IP VPS и любые секреты.

## Метрика после публикации

30 дней: 20 установок (GitHub clones + каталоги), 5 платящих ключей, 100 проданных кодов.
