# Каталоги MCP: что готово и что нужно от владельца

Публичный эндпоинт: `https://mcp.188.137.227.14.sslip.io/mcp` (streamable HTTP, TLS Let's Encrypt до 18.01.2027).

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

1. **GitHub** — создать `MassimoSun/smsverbot-mcp`, Public, без инициализации → push `master`.
2. **Official MCP Registry** — `npx @modelcontextprotocol/publisher publish` или PR с `server.json` в `modelcontextprotocol/registry`; нужен GitHub-логин и подтверждение владения namespace `io.github.massimosun`.
3. **Smithery** — smithery.ai → Add server → GitHub URL; подхватит `smithery.yaml`.
4. **Glama** — glama.ai/mcp → Submit; индексирует репозиторий автоматически, карточка появляется за 1–2 дня.
5. **mcp.so** — форма Submit на сайте, поле «GitHub repo URL».
6. **PulseMCP / awesome-mcp-servers** — PR в список `punkpeye/awesome-mcp-servers` (раздел Finance/Utilities), строка: `- [smsverbot](https://github.com/MassimoSun/smsverbot-mcp) — SMS verification codes for AI agents with prepaid RUB balance.`

## Текст карточки (одинаковый везде)

**Name:** Smsverbot — SMS verification for AI agents
**Short:** Buy a real phone number, wait for the SMS code, cancel with automatic refund. 153 countries, 2500+ services, prepaid RUB balance, pay-as-you-go.
**Tools (9):** list_services, list_countries, get_price, buy_number, wait_for_code, cancel_order, get_balance, order_history, whoami
**Transport:** streamable HTTP `https://mcp.188.137.227.14.sslip.io/mcp`, plus stdio (`python server.py --stdio`)
**Auth:** API key — either `Authorization: Bearer <key>` header (hosted mode) or the first `api_key` argument; issued in the Telegram bot, balance top-up there too
**Tags:** sms, otp, verification, phone-number, virtual-number, telegram, whatsapp, agents

## Чего в каталогах быть не должно

- источник номеров (5sim/Tiger) — только «real carrier SIMs via partner providers»;
- внутренние цены закупки и наценка;
- IP VPS и любые секреты.

## Метрика после публикации

30 дней: 20 установок (GitHub clones + каталоги), 5 платящих ключей, 100 проданных кодов.
