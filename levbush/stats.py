"""Статистика из локального кэша: на человека (всё время + последние дни/недели/месяцы), по парам, по группе.

Считается целиком заново (быстро: один проход по сообщениям), в Supabase уходят только изменившиеся строки.
"""
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .cache import Cache
from .config import Config
from .episodes import segment
from .normalize import MEDIA_KINDS, u16_slice

MEDIA_COUNTER = {"photo": "photos", "video": "videos", "document": "documents", "audio": "audios", "gif": "gifs",
                 "sticker": "stickers", "poll": "polls", "video_note": "video_notes", "voice": "voices"}
URL_RE = re.compile(r"https?://\S+|\bt\.me/\S+", re.I)
PAIR_KEYS = ("replies", "quotes", "mentions", "reactions", "forwards")

# вес взаимодействий в количественной силе связи
WEIGHTS = {"replies": 1.0, "quotes": 1.2, "mentions": 1.0, "reactions": 0.3, "forwards": 0.5, "co_episodes": 0.5}

# подписи счётчиков (бот и сайт)
LABELS = {
    "msgs": "сообщения", "replies": "ответы", "quotes": "цитаты", "comments": "комментарии к постам",
    "forwards": "пересылки", "mentions": "упоминания", "reactions": "реакции", "media": "медиа",
    "photos": "фото", "videos": "видео", "documents": "файлы", "audios": "аудио", "gifs": "GIF",
    "video_notes": "кружки", "voices": "голосовые", "stickers": "стикеры", "polls": "опросы", "links": "ссылки",
    "words": "слова", "chars": "символы", "edits": "правки", "replies_recv": "ответов получено",
    "quotes_recv": "цитирований получено", "mentions_recv": "упоминаний получено",
    "reactions_recv": "реакций получено", "voice_sec": "секунд голосовых", "video_note_sec": "секунд кружков",
}


@dataclass
class Result:
    people: dict = field(default_factory=dict)       # uid → строка people
    totals: dict = field(default_factory=dict)       # uid → {"c": {...}, "extra": {...}}
    periods: dict = field(default_factory=dict)      # (uid, 'd'|'w'|'m', date) → c
    pairs: dict = field(default_factory=dict)        # (src, dst) → c
    relations: dict = field(default_factory=dict)    # (a, b), a < b → {"quant", "co_episodes", "weight"}
    group: dict = field(default_factory=dict)
    daily: dict = field(default_factory=dict)        # date → (msgs, active)
    computed_at: int = 0


def retention_cutoffs(today: date, cfg: Config) -> tuple[date, date, date]:
    """Первые хранимые день, неделя (понедельник) и месяц."""
    day_cut = today - timedelta(days=cfg.keep_days - 1)
    week_cut = today - timedelta(days=today.weekday()) - timedelta(weeks=cfg.keep_weeks - 1)
    month_cut = today.replace(day=1)
    for _ in range(cfg.keep_months - 1):
        month_cut = (month_cut - timedelta(days=1)).replace(day=1)
    return day_cut, week_cut, month_cut


def _inc(store: dict, key, name: str, n=1):
    c = store.get(key)
    if c is None:
        c = store[key] = Counter()
    c[name] += n


def _circular_mean_hour(hours: list[int]) -> float | None:
    total = sum(hours)
    if not total:
        return None
    x = sum(n * math.cos(2 * math.pi * h / 24) for h, n in enumerate(hours))
    y = sum(n * math.sin(2 * math.pi * h / 24) for h, n in enumerate(hours))
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return round((math.atan2(y, x) * 24 / (2 * math.pi)) % 24, 1)


def _membership(events, first_msg, last_msg, is_member, member_since, now):
    """Интервалы членства → (first_join, exact, last_join, left_at, seconds)."""
    events = sorted(events)
    intervals = []
    inside, join_t, exact = False, None, True
    last_join = left_at = None
    if first_msg is not None and (not events or first_msg < events[0][0]):
        # писал раньше первого известного входа — считаем, что состоял с первого сообщения
        inside, join_t, exact = True, first_msg, False
    for t, ev in events:
        if ev == "join":
            if not inside:
                inside, join_t = True, t
            last_join = t
        elif ev == "leave":
            if inside:
                intervals.append((join_t, t))
                inside = False
            elif not intervals:
                start = first_msg if first_msg is not None and first_msg < t else t
                intervals.append((start, t))
                exact = False
            left_at = t
    if inside:
        if is_member or is_member is None:
            intervals.append((join_t, now))
        else:
            end = max(last_msg or join_t, join_t)
            intervals.append((join_t, end))
            left_at = left_at or end
    elif is_member:
        start = member_since or first_msg or now
        intervals.append((start, now))
        last_join = last_join or start
    first_join = intervals[0][0] if intervals else None
    seconds = sum(max(0, b - a) for a, b in intervals)
    if is_member:
        left_at = None
    return first_join, exact, last_join, left_at, seconds


def compute(cache: Cache, cfg: Config, now: int | None = None) -> Result:
    now = now or int(time.time())
    tz = cfg.tz
    res = Result(computed_at=now)
    chat = cache.get("chat", {}) or {}
    chat_id, channel_id = chat.get("id"), chat.get("channel_id")
    db = cache.db
    users = {r["id"]: dict(r) for r in db.execute("select * from users")}
    bot_ids = {uid for uid, u in users.items() if u.get("is_bot")}
    alias = cache.aliases(cfg.aliases)          # от имени группы/канала → конкретный человек
    # не люди: сама группа (аноним-админ без псевдонима), привязанный канал, боты — в статистику не идут
    hidden_ids = {x for x in (chat_id, channel_id) if x} | bot_ids
    by_username = {u["username"].lower(): uid for uid, u in users.items() if u.get("username")}

    today = datetime.fromtimestamp(now, tz).date()
    day_cut, week_cut, month_cut = retention_cutoffs(today, cfg)
    daily_cut = today - timedelta(days=89)

    msgs = db.execute("select id, date, edit_date, sender_id, reply_to, reply_peer, quote, fwd_from_id, fwd_date, auto_fwd, "
                      "text, entities, media, media_meta, service from messages where not deleted "
                      "order by date, id").fetchall()

    sender_of = {}
    date_of = {}
    auto_posts = set()
    totals: dict[int, Counter] = {}
    periods: dict = {}
    pairs: dict = {}
    hours = defaultdict(lambda: [0] * 24)
    weekdays = defaultdict(lambda: [0] * 7)
    times = defaultdict(list)             # uid → даты сообщений
    days_active = defaultdict(set)
    reply_lat = defaultdict(list)
    first_msg, last_msg = {}, {}
    daily_msgs, daily_users = Counter(), defaultdict(set)
    group_first = None
    content = []                           # несервисные сообщения для эпизодов

    def add(uid, name, d: date, n=1):
        _inc(totals, uid, name, n)
        if d >= day_cut:
            _inc(periods, (uid, "d", d), name, n)
        wk = d - timedelta(days=d.weekday())
        if wk >= week_cut:
            _inc(periods, (uid, "w", wk), name, n)
        mo = d.replace(day=1)
        if mo >= month_cut:
            _inc(periods, (uid, "m", mo), name, n)

    for r in msgs:
        mid, ts, s = r["id"], r["date"], r["sender_id"]
        if not r["auto_fwd"] and s in alias:
            s = alias[s]
        sender_of[mid] = s
        date_of[mid] = ts
        if r["service"]:
            continue
        content.append(r)
        if r["auto_fwd"]:
            auto_posts.add(mid)
            continue
        if s is None or s in hidden_ids:
            continue
        dt = datetime.fromtimestamp(ts, tz)
        d = dt.date()
        group_first = ts if group_first is None else group_first
        add(s, "msgs", d)
        hours[s][dt.hour] += 1
        weekdays[s][dt.weekday()] += 1
        times[s].append(ts)
        days_active[s].add(d)
        first_msg.setdefault(s, ts)
        last_msg[s] = ts
        if d >= daily_cut:
            daily_msgs[d] += 1
            daily_users[d].add(s)

        # ответы и цитаты
        target = None
        if r["reply_to"] and not (r["reply_peer"] and r["reply_peer"] != chat_id):
            rt = r["reply_to"]
            if rt in auto_posts:
                add(s, "comments", d)
            else:
                target = sender_of.get(rt)
                if target is not None and target != s and target not in hidden_ids:
                    kind = "quotes" if r["quote"] else "replies"
                    add(s, kind, d)
                    add(target, kind + "_recv", d)
                    _inc(pairs, (s, target), kind)
                    lat = ts - date_of.get(rt, ts)
                    if 0 <= lat < 86400:
                        reply_lat[s].append(lat)

        # пересылки
        if r["fwd_from_id"] is not None or r["fwd_date"] is not None:   # у скрытых пользователей id нет
            add(s, "forwards", d)
            src = r["fwd_from_id"]
            if src is not None and src != s and src in users and src not in hidden_ids:
                _inc(pairs, (s, src), "forwards")

        # упоминания, ссылки
        ents = json.loads(r["entities"]) if r["entities"] else []
        mentioned = set()
        has_link = r["media"] == "webpage"
        for e in ents:
            t = e.get("t")
            if t == "mention":
                name = u16_slice(r["text"], e["o"], e["l"]).lstrip("@").lower()
                uid = by_username.get(name)
                if uid:
                    mentioned.add(uid)
            elif t == "text_mention" and e.get("u"):
                mentioned.add(e["u"])
            elif t in ("url", "text_link"):
                has_link = True
        if not has_link and r["text"] and URL_RE.search(r["text"]):
            has_link = True
        mentioned.discard(s)
        for uid in mentioned:
            if uid in hidden_ids:
                continue
            add(s, "mentions", d)
            add(uid, "mentions_recv", d)
            _inc(pairs, (s, uid), "mentions")
        if has_link:
            add(s, "links", d)

        # медиа
        kind = r["media"]
        if kind in MEDIA_COUNTER:
            add(s, MEDIA_COUNTER[kind], d)
            if kind in MEDIA_KINDS:
                add(s, "media", d)
            if kind in ("voice", "video_note") and r["media_meta"]:
                dur = json.loads(r["media_meta"]).get("duration")
                if dur:
                    add(s, "voice_sec" if kind == "voice" else "video_note_sec", d, int(dur))
        text = r["text"] or ""
        if text:
            add(s, "words", d, len(text.split()))
            add(s, "chars", d, len(text))
        if r["edit_date"]:
            add(s, "edits", d)

    # реакции
    top_reactions = defaultdict(Counter)
    for msg_id, uid, emoji, rdate in db.execute("select msg_id, user_id, emoji, date from reactions"):
        if uid in hidden_ids or msg_id not in sender_of:
            continue
        ts = rdate or date_of.get(msg_id) or now
        d = datetime.fromtimestamp(ts, tz).date()
        add(uid, "reactions", d)
        top_reactions[uid][emoji] += 1
        target = sender_of.get(msg_id)
        if target is not None and target != uid and target not in hidden_ids and msg_id not in auto_posts:
            add(target, "reactions_recv", d)
            _inc(pairs, (uid, target), "reactions")

    # эпизоды: начатые беседы и совместное участие
    episodes = segment(content, cfg.gap_min, cfg.cast_window, alias)
    started = Counter()
    co = Counter()
    for ep in episodes:
        parts = [p for p in ep.participants if p not in hidden_ids]
        if not parts:
            continue
        first_author = next((alias.get(m["sender_id"], m["sender_id"]) for m in ep.msgs if not m["auto_fwd"]), None)
        if first_author is not None and len(parts) > 1:
            started[first_author] += 1
        if len(parts) <= 40:
            for i, a in enumerate(parts):
                for b in parts[i + 1:]:
                    co[(min(a, b), max(a, b))] += 1

    # кто попадает в people: участники (сейчас или раньше) и все, кто писал/реагировал
    membership = defaultdict(list)
    for uid, ts, ev in db.execute("select user_id, date, event from membership"):
        membership[uid].append((ts, ev))
    ids = set(totals) | {uid for uid, u in users.items() if u.get("is_member")} | set(membership)
    ids |= {x for x in (chat_id, channel_id) if x} & set(users)
    ids -= bot_ids

    for uid in ids:
        u = users.get(uid, {"id": uid, "kind": "user"})
        fj, exact, lj, left, secs = _membership(membership.get(uid, []), first_msg.get(uid), last_msg.get(uid),
                                                u.get("is_member"), u.get("member_since"), now)
        res.people[uid] = {
            "id": uid, "kind": u.get("kind") or "user", "first_name": u.get("first_name"),
            "last_name": u.get("last_name"), "username": u.get("username"), "is_bot": bool(u.get("is_bot")),
            "is_premium": bool(u.get("is_premium")), "avatar": u.get("avatar"),
            "is_member": bool(u.get("is_member")), "first_join": fj, "first_join_exact": exact, "last_join": lj,
            "left_at": left, "time_in_group_sec": int(secs), "first_msg_at": first_msg.get(uid),
            "last_msg_at": last_msg.get(uid), "hidden": uid in hidden_ids,
        }

    for uid in ids - hidden_ids:
        c = totals.get(uid, Counter())
        ts_list = times.get(uid, [])
        sessions = []
        if ts_list:
            start = prev = ts_list[0]
            for t in ts_list[1:]:
                if t - prev >= cfg.gap_min * 60:
                    sessions.append((start, prev))
                    start = t
                prev = t
            sessions.append((start, prev))
        # сессия из одного сообщения — считаем минутой
        durations = [max(60, b - a + 60) for a, b in sessions]
        adays = sorted(days_active.get(uid, ()))
        streak = best = 0
        prev_day = None
        for d in adays:
            streak = streak + 1 if prev_day is not None and d - prev_day == timedelta(days=1) else 1
            best = max(best, streak)
            prev_day = d
        hrs = hours[uid] if uid in hours else [0] * 24
        extra = {
            "hours": hrs, "weekdays": weekdays[uid] if uid in weekdays else [0] * 7,
            "mean_hour": _circular_mean_hour(hrs), "peak_hour": hrs.index(max(hrs)) if any(hrs) else None,
            "sessions": len(sessions),
            "avg_session_min": round(statistics.mean(durations) / 60, 1) if durations else None,
            "active_days": len(adays), "longest_streak": best,
            "active_min_per_day": round(sum(durations) / 60 / len(adays), 1) if adays else None,
            "median_reply_sec": int(statistics.median(reply_lat[uid])) if reply_lat.get(uid) else None,
            "conversations_started": started.get(uid, 0),
            # [реакция, сколько, эмодзи-аналог] — у премиум-реакций (custom:ID) аналог для сайта и нейросети
            "top_reactions": [[e, n, cache.reaction_label(e)] for e, n in top_reactions[uid].most_common(5)]
            if uid in top_reactions else [],
        }
        res.totals[uid] = {"c": dict(c), "extra": extra}

    res.periods = {k: dict(v) for k, v in periods.items() if k[0] in ids}
    res.pairs = {k: dict(v) for k, v in pairs.items() if k[0] in ids and k[1] in ids}

    # количественная сила связи
    weight = Counter()
    for (a, b), c in res.pairs.items():
        key = (min(a, b), max(a, b))
        weight[key] += sum(WEIGHTS[k] * c.get(k, 0) for k in PAIR_KEYS)
    for key, n in co.items():
        if key[0] in ids and key[1] in ids:
            weight[key] += WEIGHTS["co_episodes"] * n
    top = max(weight.values(), default=0)
    for key, w in weight.items():
        if w <= 0 or key[0] in hidden_ids or key[1] in hidden_ids:
            continue
        res.relations[key] = {"quant": round(math.log1p(w) / math.log1p(top), 4) if top else 0.0,
                              "co_episodes": co.get(key, 0), "weight": round(w, 2)}

    members = sum(1 for p in res.people.values()
                  if p["is_member"] and p["kind"] == "user" and not p["hidden"] and not p["is_bot"])
    res.group = {"chat_id": chat_id, "title": chat.get("title"), "username": chat.get("username"),
                 "channel_id": channel_id, "channel_title": chat.get("channel_title"), "members": members,
                 "messages": sum(t["c"].get("msgs", 0) for t in res.totals.values()), "first_date": group_first,
                 "tz": str(cfg.tz)}
    res.daily = {d: (daily_msgs[d], len(daily_users[d])) for d in daily_msgs}
    return res


def fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    days = seconds // 86400
    if days >= 365:
        y, rem = divmod(days, 365)
        return f"{y} г {rem // 30} мес"
    if days >= 30:
        return f"{days // 30} мес {days % 30} д"
    if days:
        return f"{days} д {seconds % 86400 // 3600} ч"
    if seconds >= 3600:
        return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    return f"{max(1, seconds // 60)} мин"
