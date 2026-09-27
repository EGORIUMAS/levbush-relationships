# Levbush Relationships

Бот для группы обсуждения канала: подробная статистика участников и пар, досье и карта связей, которые ведёт
**Nemotron 3 Nano Omni**, пересказ последних событий, онлайн-карта (сайт / Mini App).

## Как устроено

```
Telegram ──Bot API──► levbush run (бот: только команды) ─┐
         ──MTProto──► Telethon (твой аккаунт) ───────────┤   история и живой сбор: сообщения, правки, удаления,
                                                          │   реакции поимённо, входы/выходы, профили, медиа
                                                        ▼
                    ~/.local/share/levbush/cache.db (SQLite) + /mnt/shared/levbush/media   ← переписка только локально
                                                        │
             stats.compute (каждые 2 мин) ──────────────┤──► Supabase Postgres: people, stats_*, pair_stats,
             Nemotron-разбор (первый прогон, потом ежедн.)┘    relations, dossiers, episodes
                                                                     │  sql/api.sql — api_graph / api_person / …
                                     Edge Function `api` (подпись Telegram) ◄── сайт web/ (GitHub Pages = Mini App)
```

- **Статистика** (`levbush/stats.py`): на человека — за день/неделю/месяц (последние 30/12/12) и всё время, среднее
  за день с момента вступления: сообщения, ответы, цитаты, комментарии к постам, пересылки, упоминания, реакции
  (поставлено/получено), медиа по типам, кружки и голосовые (и их длительность), стикеры, опросы, ссылки, слова,
  правки. Плюс: первый вход, время в группе с учётом выходов, часы и дни активности, средняя сессия, активных минут
  в день, медианное время ответа, серии дней, начатые беседы, любимые реакции. По парам (кто → кого): ответы, цитаты,
  упоминания, реакции, пересылки. Сила связи = количественная часть (0.4) + оценка Nemotron (0.6).
  Боты в статистику не входят (их сообщения остаются в переписке для нейросети как контекст). Сообщения
  «от имени группы/канала» приписываются человеку через `LEVBUSH_ALIASES`. Имена и ники берёт бот
  (`getChatMember`): Telethon для контактов аккаунта отдаёт имена из записной книжки.
- **Разбор** (`levbush/analyze.py`): переписка режется на окна (перерыв ≥ 30 мин или полная смена действующих лиц).
  Шаг = несколько последовательных окон + уже разобранные окна за 48 ч как контекст + текущие досье участников и
  описания связей между ними (с id записей). Nemotron получает вложения как есть (фото, видео, кружки, голосовые;
  PDF — страницами, из текстовых файлов — текст; речь дополнительно расшифрована Parakeet v3, т.к. Nemotron понимает
  только английскую речь) и возвращает **правки** — `add` / `update` / `remove` записей досье, сводку, поля связи,
  события и заметки о связи (`levbush/dossier.py`). Досье не переписывается целиком и не версионируется: у каждой
  записи видно, с какого числа она известна, когда и как изменилась (со старым текстом) или устарела.
  Первый прогон — такими шагами по всей истории; дальше раз в день (`LEVBUSH_DAILY_AT`) с контекстом за два дня.
  Кроме пишущих, в шаг идут полные досье тех, кого в окнах обсуждают заочно: @упоминание, ответ/цитата на их
  сообщение, пересылка или имя из «Как называют» в досье (с падежами: Лёва → Лёвы, Лёвой; формы вроде «Льва»
  Nemotron вносит сам). Остальные люди группы — одной строкой; им можно только добавлять записи «со слов …».
- **Nemotron** поднимается ботом сам (`levbush-nemotron`, systemd-run, порт 18090) и гасится после 15 мин простоя;
  ждёт, пока MiniMax H3 считает запрос; при нехватке VRAM усыпляет vLLM-Qwen и будит после.

## Запуск

1. **Бот**: @BotFather → новый бот. Добавить в группу обычным участником (права админа и выключенный privacy mode
   не нужны — всё собирает Telethon; команды бот видит и так).
   Для входа на сайте — `/setdomain` → `egoriumas.github.io`; для Mini App — `/newapp` (URL сайта).
2. **Telethon**: my.telegram.org → API development tools → `api_id`, `api_hash`.
3. **База**: Supabase, проект `levbush-relationships` уже создан, схема и Edge Function задеплоены.
   Нужна строка подключения: Connect → **Transaction pooler** (порт 6543 — 5432 через VPN sbx не проходит)
   (пароль — «Reset database password»).
4. Настройки: `cp deploy/levbush.env.example ~/.config/levbush.env && chmod 600 ~/.config/levbush.env`, заполнить.
5. Вход Telethon (интерактивно, один раз): `~/.local/share/levbush/venv/bin/python -m levbush login`
6. Сайт: в `web/config.js` — `bot: "<имя бота без @>"`; опубликовать `web/` на GitHub Pages.
7. Сервис:
   ```bash
   ln -s ~/levbush_relationships/deploy/levbush.service ~/.config/systemd/user/levbush.service
   systemctl --user daemon-reload && systemctl --user enable --now levbush
   journalctl --user -u levbush -f
   ```
8. В личке боту (от `LEVBUSH_ADMIN_ID`): **`/initiate`** — только после этого начинается сбор: участники →
   вся история → реакции поимённо → профили → медиа, дальше — живой сбор. Затем бот выгружает статистику и
   спрашивает, запускать ли первый разбор нейросетью. Флаг сохраняется: после перезапуска сбор продолжается сам.

Telegram-лимиты: паузы `LEVBUSH_TG_HISTORY_DELAY` (1 с на 100 сообщений), `TG_REACTION_DELAY` (1,5 с),
`TG_MEDIA_DELAY` (0,7 с), `TG_PROFILE_DELAY` (2 с); FloodWait — ждём +10 % и замедляемся вдвое (до ×16),
через 30 мин без FloodWait темп восстанавливается. Счётчик FloodWait и текущий темп — в `/status`.

## Команды бота

`/stats [@ник]` (или ответом) · `/me` · `/top [метрика] [d|w|m|a]` · `/pair @a [@b]` · `/dossier [@ник]` ·
`/links [@ник]` · `/retell 2ч | 30м | 14:30 | вчера 20:00` (или ответом на сообщение; не дальше 48 ч) · `/map`.
Админ: `/initiate`, `/status`, `/analyze`, `/sync`. Доступ — участникам группы.

## CLI

`python -m levbush login | run | sync | stats | analyze | migrate | web | status` (подробности — `--help`).

## Этап 2: без Supabase

Локальный Postgres → `LEVBUSH_DATABASE_URL` на него → `levbush migrate` → `levbush web --port 8095`
(тот же API на `/api` + раздача `web/`, TLS через Caddy) → в `web/config.js` `api: "/api"`.

## Тесты

```bash
~/.local/share/levbush/venv/bin/python -m pytest -q tests/                   # офлайн
docker run -d --rm --name levbush-pgtest -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine
LEVBUSH_TEST_DB=postgresql://postgres:test@127.0.0.1:55432/postgres ~/.local/share/levbush/venv/bin/python -m pytest -q tests/
```
