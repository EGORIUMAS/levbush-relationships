# API сайта (контракт)

Один эндпоинт, запросы `GET <API>?q=<запрос>&…`, ответ — JSON.

- Этап 1: `<API>` = `https://xmtzjuyoqmjrhegbdrzx.supabase.co/functions/v1/api` (Supabase Edge Function).
- Этап 2 (локально): `<API>` = `https://<хост>/api` (`levbush web`), контракт тот же.

## Авторизация

Заголовок `Authorization` обязателен:

- Mini App: `Authorization: tma <Telegram.WebApp.initData>` (строка как есть).
- Сайт (Telegram Login Widget): `Authorization: tglogin <base64(JSON объекта user из виджета)>`
  (поля `id, first_name, last_name?, username?, photo_url?, auth_date, hash`).

Пускают участников группы (и админа бота). Ответы ошибок: `401 {"error":"auth"}` — нет/протухла подпись
(показать вход), `403 {"error":"not_member"}` — не участник группы.

## Счётчики (`c`)

Объект с целыми числами, ключи (любой может отсутствовать = 0):

| ключ | что |
|---|---|
| `msgs` | сообщения (без служебных) |
| `replies` | ответы (реплаи) на чужие сообщения без цитаты |
| `quotes` | ответы с цитатой (выделенный фрагмент) |
| `forwards` | пересланные сообщения |
| `comments` | ответы на посты канала (комментарии) |
| `mentions` | упоминания других (@username / ссылка на профиль) |
| `reactions` | поставленные реакции |
| `media` | медиа: фото, видео, файлы, аудио, GIF |
| `photos`, `videos`, `documents`, `audios`, `gifs` | по типам |
| `video_notes` | кружки |
| `voices` | голосовые |
| `stickers`, `polls`, `links` | стикеры, опросы, сообщения со ссылками |
| `words`, `chars` | слова и символы текста |
| `edits` | отредактированные сообщения |
| `replies_recv`, `quotes_recv`, `mentions_recv`, `reactions_recv` | то же, но полученные от других |
| `voice_sec`, `video_note_sec` | суммарная длительность голосовых/кружков, с |

## `q=graph`

```json
{
  "group": {"title": "…", "username": "…|null", "members": 42, "messages": 123456,
            "first_date": "2024-01-01T…Z", "map_updated_at": "…|null", "stats_updated_at": "…",
            "pass": {"state": "idle|running|done|error", "stage": "шаг 3/120: переписка за 2025-01-04",
                     "done": 2, "total": 120}},
  "me": 12345,
  "nodes": [{"id": 1, "name": "Имя Фамилия", "username": "nick|null", "avatar": "data:image/jpeg;base64,…|null",
             "is_member": true, "msgs": 1234, "summary": "одна строка из досье|null"}],
  "links": [{"a": 1, "b": 2, "strength": 0.83, "kind": "дружба", "tone": "тёплый", "summary": "одна строка"}]
}
```
`strength` ∈ [0,1]; у каждой пары `a < b`. Узлы — все, кто хоть раз писал/состоял в группе (включая вышедших).

## `q=person&id=<id>`

```json
{
  "person": {"id": 1, "name": "…", "first_name": "…", "last_name": "…|null", "username": "…|null",
             "avatar": "…|null", "is_bot": false, "is_premium": true, "is_member": true,
             "first_join": "ISO|null", "last_join": "ISO|null", "left_at": "ISO|null",
             "time_in_group_sec": 12345678, "first_msg_at": "ISO|null", "last_msg_at": "ISO|null"},
  "total": {"c": {…}, "hours": [24 ints, локальное время], "weekdays": [7 ints, пн..вс],
            "mean_hour": 21.4, "peak_hour": 22, "median_reply_sec": 95, "avg_session_min": 12.5,
            "sessions": 300, "active_days": 120, "longest_streak": 14, "active_min_per_day": 18.2,
            "conversations_started": 40, "top_reactions": [["❤", 120], ["😂", 80]]},
  "avg_per_day": {…счётчики, дробные: всего / дней в группе},
  "days_in_group": 350.2,
  "periods": {"d": [{"start": "2026-09-27", "c": {…}}],   // последние 30 дней, новые первыми
              "w": [{"start": "2026-09-21", "c": {…}}],   // 12 недель (понедельник)
              "m": [{"start": "2026-09-01", "c": {…}}]},  // 12 месяцев
  "dossier": {"as_of": "ISO", "summary": "…", "content": "markdown", "updated_at": "ISO"},
  "relations": [{"id": 2, "name": "…", "avatar": "…|null", "strength": 0.8, "kind": "…", "tone": "…", "summary": "…"}],
  "pairs": [{"id": 2, "name": "…", "out": {"replies": 1, "quotes": 0, "mentions": 3, "reactions": 10, "forwards": 0},
             "in": {…то же, от него к этому человеку}}]
}
```
`dossier` может быть `null`. Версий нет: у каждой записи досье в тексте указано, с какого числа она известна и когда изменилась или устарела. `relations` отсортированы по силе, `pairs` — по сумме взаимодействий.

## `q=relation&a=<id>&b=<id>` (порядок любой)

```json
{
  "a": {"id": 1, "name": "…", "avatar": "…|null"},
  "b": {"id": 2, "name": "…", "avatar": "…|null"},
  "relation": {"strength": 0.8, "quant": 0.7, "llm_score": 0.9, "kind": "…", "tone": "…",
               "summary": "…", "description": "markdown", "updated_at": "ISO", "as_of": "ISO"},
  "ab": {"replies": 1, "quotes": 0, "mentions": 3, "reactions": 10, "forwards": 0},
  "ba": {…}
}
```
`relation` может быть `null` (есть только счётчики). В ответе `a`/`b` — в том порядке, что в запросе.
