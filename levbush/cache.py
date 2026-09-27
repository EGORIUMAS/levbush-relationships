"""Локальный кэш группы (SQLite): сообщения, реакции, участники, входы/выходы, расшифровки речи.

Сюда пишут и Telethon (история, докачка), и бот (новые сообщения, реакции, входы/выходы).
В Supabase переписка не уходит — только агрегаты и результаты нейросети.
Идентификаторы — как в Bot API: пользователь > 0, канал/супергруппа — -100…
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
create table if not exists messages (
    id           integer primary key,
    date         integer not null,
    edit_date    integer,
    sender_id    integer,
    reply_to     integer,
    reply_peer   integer,
    top_id       integer,
    quote        text,
    fwd_from_id  integer,
    fwd_from_name text,
    fwd_date     integer,
    auto_fwd     integer not null default 0,
    text         text,
    entities     text,
    media        text,
    media_meta   text,
    media_path   text,
    media_state  text,            -- null — нет медиа; pending / ok / skip / error
    grouped_id   integer,
    via_bot      integer,
    service      text,
    service_data text,
    deleted      integer not null default 0,
    source       text
);
create index if not exists messages_date on messages(date);
create index if not exists messages_sender on messages(sender_id, date);
create index if not exists messages_reply on messages(reply_to);
create index if not exists messages_media_state on messages(media_state);

create table if not exists reactions (
    msg_id  integer not null,
    user_id integer not null,
    emoji   text not null,
    date    integer,
    primary key (msg_id, user_id, emoji)
);
create index if not exists reactions_user on reactions(user_id);

-- счётчики реакций, когда список «кто поставил» недоступен
create table if not exists reaction_counts (
    msg_id integer not null,
    emoji  text not null,
    count  integer not null,
    primary key (msg_id, emoji)
);

-- сообщения, для которых надо догрузить поимённый список реакций (очередь переживает перезапуск)
create table if not exists reaction_todo (
    msg_id integer primary key
);

create table if not exists users (
    id          integer primary key,
    kind        text not null default 'user',
    first_name  text,
    last_name   text,
    username    text,
    is_bot      integer not null default 0,
    is_premium  integer not null default 0,
    lang        text,
    bio         text,
    avatar      text,
    photo_id    integer,
    is_member   integer,
    member_since integer,          -- дата входа по данным Telegram (participant.date)
    name_src    text,              -- bot — имя/ник подтверждены ботом
    updated     integer
);
create index if not exists users_username on users(lower(username));

create table if not exists membership (
    user_id integer not null,
    date    integer not null,
    event   text not null,          -- join / leave
    source  text,
    primary key (user_id, date, event)
);

create table if not exists transcripts (
    msg_id  integer primary key,
    text    text not null,
    seconds real,
    model   text,
    created integer
);

create table if not exists analyzed (
    episode_id integer primary key,
    last_id    integer not null,
    status     text not null,       -- done / error
    error      text,
    updated    integer
);

create table if not exists kv (
    key   text primary key,
    value text
);
"""

MSG_FIELDS = ("id", "date", "edit_date", "sender_id", "reply_to", "reply_peer", "top_id", "quote", "fwd_from_id",
              "fwd_from_name", "fwd_date", "auto_fwd", "text", "entities", "media", "media_meta", "media_path",
              "media_state", "grouped_id", "via_bot", "service", "service_data", "deleted", "source")

USER_FIELDS = ("id", "kind", "first_name", "last_name", "username", "is_bot", "is_premium", "lang", "bio",
               "avatar", "photo_id", "is_member", "member_since", "name_src", "updated")


def _dump(value):
    """dict/list → JSON-текст, остальное как есть."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


class Cache:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.db.executescript(SCHEMA)   # сам управляет транзакцией
        cols = {r[1] for r in self.db.execute("pragma table_info(users)")}
        if "name_src" not in cols:      # кэш создан до появления колонки
            self.db.execute("alter table users add column name_src text")

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma journal_mode=wal")
            conn.execute("pragma synchronous=normal")
            conn.execute("pragma foreign_keys=on")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self):
        db = self.db
        db.execute("begin immediate")
        try:
            yield db
            db.execute("commit")
        except BaseException:
            db.execute("rollback")
            raise

    # ------------------------------------------------------------ kv

    def get(self, key, default=None):
        row = self.db.execute("select value from kv where key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("insert into kv(key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    # ------------------------------------------------------------ сообщения

    def upsert_message(self, row: dict, db=None):
        """Вставка или обновление. Локальный путь к файлу и состояние загрузки не затираются."""
        db = db or self.db
        row = {k: _dump(row.get(k)) for k in MSG_FIELDS}
        row["auto_fwd"] = row["auto_fwd"] or 0
        row["deleted"] = row["deleted"] or 0
        cols = ", ".join(MSG_FIELDS)
        marks = ", ".join("?" for _ in MSG_FIELDS)
        keep = {"id", "media_path", "media_state", "deleted"}
        updates = ", ".join(f"{k} = coalesce(excluded.{k}, {k})" if k in ("edit_date", "text", "entities", "media_meta")
                            else f"{k} = excluded.{k}" for k in MSG_FIELDS if k not in keep)
        db.execute(f"insert into messages({cols}) values ({marks}) on conflict(id) do update set {updates}, "
                   f"media_state = case when media_state is null then excluded.media_state else media_state end",
                   [row[k] for k in MSG_FIELDS])

    def max_message_id(self) -> int:
        return self.db.execute("select coalesce(max(id), 0) from messages").fetchone()[0]

    def min_message_id(self) -> int:
        return self.db.execute("select coalesce(min(id), 0) from messages").fetchone()[0]

    def message(self, msg_id: int):
        return self.db.execute("select * from messages where id = ?", (msg_id,)).fetchone()

    def messages_between(self, t0: int, t1: int | None = None):
        if t1 is None:
            return self.db.execute("select * from messages where date >= ? and not deleted order by date, id",
                                   (t0,)).fetchall()
        return self.db.execute("select * from messages where date >= ? and date < ? and not deleted order by date, id",
                               (t0, t1)).fetchall()

    def set_media(self, msg_id: int, state: str, path: str | None = None):
        self.db.execute("update messages set media_state = ?, media_path = coalesce(?, media_path) where id = ?",
                        (state, path, msg_id))

    # ------------------------------------------------------------ реакции

    def set_reactions(self, msg_id: int, pairs: list[tuple[int, str, int | None]], db=None):
        """Полный список реакций сообщения (user_id, emoji, date) — заменяет прежний."""
        db = db or self.db
        db.execute("delete from reactions where msg_id = ?", (msg_id,))
        db.executemany("insert or ignore into reactions(msg_id, user_id, emoji, date) values (?, ?, ?, ?)",
                       [(msg_id, uid, emoji, date) for uid, emoji, date in pairs])

    def set_reaction_counts(self, msg_id: int, counts: dict[str, int], db=None):
        db = db or self.db
        db.execute("delete from reaction_counts where msg_id = ?", (msg_id,))
        db.executemany("insert into reaction_counts(msg_id, emoji, count) values (?, ?, ?)",
                       [(msg_id, e, n) for e, n in counts.items()])

    def update_user_reactions(self, msg_id: int, user_id: int, emojis: list[str], date: int):
        """Реакции одного пользователя на сообщение (из message_reaction Bot API)."""
        with self.tx() as db:
            db.execute("delete from reactions where msg_id = ? and user_id = ?", (msg_id, user_id))
            db.executemany("insert or ignore into reactions(msg_id, user_id, emoji, date) values (?, ?, ?, ?)",
                           [(msg_id, user_id, e, date) for e in emojis])

    # ------------------------------------------------------------ участники

    def upsert_user(self, row: dict, db=None):
        db = db or self.db
        row = dict(row)
        weak = row.pop("names_weak", False)       # имя из контактов: не затирает уже известное
        exact = row.pop("names_exact", False)     # имя от бота (публичное): записывается как есть, даже пустое
        row.setdefault("updated", int(time.time()))
        cols = [k for k in USER_FIELDS if k in row]
        names = ("first_name", "last_name", "username")

        def upd(k):
            if exact and k in names:
                return f"{k} = excluded.{k}"
            if weak and k in names:
                return f"{k} = coalesce({k}, excluded.{k})"
            return f"{k} = coalesce(excluded.{k}, {k})"

        updates = ", ".join(upd(k) for k in cols if k != "id")
        db.execute(f"insert into users({', '.join(cols)}) values ({', '.join('?' for _ in cols)}) "
                   f"on conflict(id) do update set {updates}", [row[k] for k in cols])

    def user(self, uid: int):
        return self.db.execute("select * from users where id = ?", (uid,)).fetchone()

    def user_by_username(self, username: str):
        return self.db.execute("select * from users where lower(username) = lower(?)",
                               (username.lstrip("@"),)).fetchone()

    def aliases(self, spec: str) -> dict[int, int]:
        """LEVBUSH_ALIASES «-100…=@ник,-100…=123» → {id группы/канала: id человека}."""
        out = {}
        for part in (spec or "").split(","):
            if "=" not in part:
                continue
            src, dst = (x.strip() for x in part.split("=", 1))
            if not src.lstrip("-").isdigit():
                continue
            if dst.lstrip("-").isdigit():
                out[int(src)] = int(dst)
            elif (u := self.user_by_username(dst)) is not None:
                out[int(src)] = u["id"]
        return out

    def add_membership(self, uid: int, date: int, event: str, source: str, db=None):
        (db or self.db).execute("insert or ignore into membership(user_id, date, event, source) values (?, ?, ?, ?)",
                                (uid, date, event, source))

    # ------------------------------------------------------------ расшифровки

    def transcript(self, msg_id: int):
        row = self.db.execute("select text from transcripts where msg_id = ?", (msg_id,)).fetchone()
        return row[0] if row else None

    def set_transcript(self, msg_id: int, text: str, seconds: float | None, model: str):
        self.db.execute("insert or replace into transcripts(msg_id, text, seconds, model, created) values (?, ?, ?, ?, ?)",
                        (msg_id, text, seconds, model, int(time.time())))
