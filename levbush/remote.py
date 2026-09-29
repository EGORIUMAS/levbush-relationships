"""База (Supabase Postgres сейчас, локальный Postgres потом) через asyncpg.

Строка подключения — LEVBUSH_DATABASE_URL. Для Supabase — Transaction pooler, порт 6543: через VPN sbx
порт 5432 рвётся сразу после рукопожатия. Подготовленные запросы поэтому выключены (statement_cache_size=0).
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from .config import ROOT, Config
from .stats import retention_cutoffs

log = logging.getLogger("levbush.db")


def ts(value):
    """unix-время → datetime UTC (или None)."""
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def strength(quant: float, llm: float | None) -> float:
    """Сила связи; нет оценки нейросети — она считается за 0."""
    return round(0.4 * quant + 0.6 * (llm or 0.0), 4)


def _h(obj) -> str:
    return hashlib.blake2b(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode(),
                           digest_size=12).hexdigest()


async def _init_conn(conn):
    await conn.set_type_codec("jsonb", encoder=lambda v: json.dumps(v, ensure_ascii=False, default=str),
                              decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=lambda v: json.dumps(v, ensure_ascii=False, default=str),
                              decoder=json.loads, schema="pg_catalog")


class DB:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: asyncpg.Pool | None = None
        self._hash_file = cfg.data_dir / "pushed.json"
        self._hashes: dict = {}

    async def connect(self):
        if not self.cfg.database_url:
            raise RuntimeError("не задана LEVBUSH_DATABASE_URL")
        # statement_cache_size=0 — совместимо с пулером Supabase в любом режиме
        self.pool = await asyncpg.create_pool(self.cfg.database_url, min_size=1, max_size=5,
                                              statement_cache_size=0, init=_init_conn, command_timeout=120)
        if self._hash_file.exists():
            try:
                self._hashes = json.loads(self._hash_file.read_text())
            except ValueError:
                self._hashes = {}

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def migrate(self):
        """Схема и функции API (идемпотентно) — для локального Postgres или после правок sql/."""
        async with self.pool.acquire() as conn:
            for name in ("schema.sql", "api.sql"):
                await conn.execute((ROOT / "sql" / name).read_text())

    # ------------------------------------------------------------ ключи для Edge Function

    async def set_app_secrets(self, bot_token: str, admin_id: int):
        login_key = hashlib.sha256(bot_token.encode()).hexdigest()
        webapp_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).hexdigest()
        rows = [("login_key", login_key), ("webapp_key", webapp_key), ("admin_id", str(admin_id))]
        await self.pool.executemany(
            "insert into app_secrets(name, value) values ($1, $2) on conflict(name) do update set value = excluded.value",
            rows)

    # ------------------------------------------------------------ выгрузка статистики

    def _changed(self, table: str, key, row) -> bool:
        k = f"{table}:{key}"
        h = _h(row)
        if self._hashes.get(k) == h:
            return False
        self._hashes[k] = h
        return True

    def _save_hashes(self):
        tmp = self._hash_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._hashes))
        tmp.replace(self._hash_file)

    async def push_stats(self, res, cfg: Config) -> dict:
        """Выгружает результат stats.compute. Возвращает число отправленных строк по таблицам."""
        sent = {}
        people = [p for uid, p in res.people.items() if self._changed("people", uid, p)]
        totals = [(uid, t) for uid, t in res.totals.items() if self._changed("total", uid, t)]
        periods = [(k, c) for k, c in res.periods.items() if self._changed("period", f"{k[0]}:{k[1]}:{k[2]}", c)]
        pairs = [(k, c) for k, c in res.pairs.items() if self._changed("pair", f"{k[0]}:{k[1]}", c)]
        rels = [(k, r) for k, r in res.relations.items() if self._changed("rel", f"{k[0]}:{k[1]}", r)]
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                if people:
                    await conn.executemany(
                        """insert into people(id, kind, first_name, last_name, username, is_bot, is_premium, avatar,
                               is_member, first_join, first_join_exact, last_join, left_at, time_in_group_sec,
                               first_msg_at, last_msg_at, hidden, updated_at)
                           values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17, now())
                           on conflict(id) do update set kind=excluded.kind, first_name=excluded.first_name,
                               last_name=excluded.last_name, username=excluded.username, is_bot=excluded.is_bot,
                               is_premium=excluded.is_premium, avatar=coalesce(excluded.avatar, people.avatar),
                               is_member=excluded.is_member, first_join=excluded.first_join,
                               first_join_exact=excluded.first_join_exact, last_join=excluded.last_join,
                               left_at=excluded.left_at, time_in_group_sec=excluded.time_in_group_sec,
                               first_msg_at=excluded.first_msg_at, last_msg_at=excluded.last_msg_at,
                               hidden=excluded.hidden, updated_at=now()""",
                        [(p["id"], p["kind"], p["first_name"], p["last_name"], p["username"], p["is_bot"],
                          p["is_premium"], p["avatar"], p["is_member"], ts(p["first_join"]), p["first_join_exact"],
                          ts(p["last_join"]), ts(p["left_at"]), p["time_in_group_sec"], ts(p["first_msg_at"]),
                          ts(p["last_msg_at"]), p["hidden"]) for p in people])
                if totals:
                    await conn.executemany(
                        """insert into stats_total(user_id, c, extra, updated_at) values ($1, $2, $3, now())
                           on conflict(user_id) do update set c=excluded.c, extra=excluded.extra, updated_at=now()""",
                        [(uid, t["c"], t["extra"]) for uid, t in totals])
                if periods:
                    await conn.executemany(
                        """insert into stats_period(user_id, ptype, pstart, c) values ($1, $2, $3, $4)
                           on conflict(user_id, ptype, pstart) do update set c=excluded.c""",
                        [(k[0], k[1], k[2], c) for k, c in periods])
                if pairs:
                    await conn.executemany(
                        """insert into pair_stats(src, dst, c) values ($1, $2, $3)
                           on conflict(src, dst) do update set c=excluded.c""",
                        [(k[0], k[1], c) for k, c in pairs])
                if rels:
                    await conn.executemany(
                        """insert into relations(a, b, quant, co_episodes, strength) values ($1, $2, $3, $4, 0.4 * $3)
                           on conflict(a, b) do update set quant=excluded.quant, co_episodes=excluded.co_episodes,
                               strength = 0.4 * excluded.quant + 0.6 * coalesce(relations.llm_score, 0)""",
                        [(k[0], k[1], r["quant"], r["co_episodes"]) for k, r in rels])
                # чего больше нет в пересчёте (боты, исключённые): удалить; связи с описанием нейросети — оставить
                await conn.execute("delete from people where not (id = any($1::bigint[]))", list(res.people))
                await conn.execute("delete from stats_total where not (user_id = any($1::bigint[]))", list(res.totals))
                await conn.execute("delete from stats_period where not (user_id = any($1::bigint[]))", list(res.totals))
                srcs, dsts = zip(*res.pairs) if res.pairs else ((), ())
                await conn.execute(
                    """delete from pair_stats p where not exists (select 1 from unnest($1::bigint[], $2::bigint[]) u(s, d)
                       where u.s = p.src and u.d = p.dst)""", list(srcs), list(dsts))
                ra, rb = zip(*res.relations) if res.relations else ((), ())
                await conn.execute(
                    """delete from relations r where r.llm_score is null and r.description is null and not exists
                       (select 1 from unnest($1::bigint[], $2::bigint[]) u(a, b) where u.a = r.a and u.b = r.b)""",
                    list(ra), list(rb))
                # старые периоды
                today = datetime.fromtimestamp(res.computed_at, cfg.tz).date()
                cd, cw, cm = retention_cutoffs(today, cfg)
                await conn.execute("""delete from stats_period where (ptype = 'd' and pstart < $1)
                                      or (ptype = 'w' and pstart < $2) or (ptype = 'm' and pstart < $3)""", cd, cw, cm)
                g = res.group
                await conn.execute(
                    """insert into group_info(id, chat_id, title, username, channel_id, channel_title, members, messages,
                           first_date, tz, stats_updated_at, updated_at)
                       values (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, now(), now())
                       on conflict(id) do update set chat_id=excluded.chat_id, title=excluded.title,
                           username=excluded.username, channel_id=excluded.channel_id,
                           channel_title=excluded.channel_title, members=excluded.members, messages=excluded.messages,
                           first_date=excluded.first_date, tz=excluded.tz, stats_updated_at=now(), updated_at=now()""",
                    g["chat_id"], g["title"], g["username"], g["channel_id"], g["channel_title"], g["members"],
                    g["messages"], ts(g["first_date"]), g["tz"])
                daily = [(d, n, a) for d, (n, a) in res.daily.items() if self._changed("daily", d.isoformat(), [n, a])]
                if daily:
                    await conn.executemany(
                        """insert into group_daily(day, msgs, active) values ($1, $2, $3)
                           on conflict(day) do update set msgs=excluded.msgs, active=excluded.active""", daily)
                await conn.execute("delete from group_daily where day < $1", today - timedelta(days=365))
        except BaseException:
            self._hashes = {}   # не знаем, что дошло, — в следующий раз отправим всё
            raise
        live = ({f"people:{k}" for k in res.people} | {f"total:{k}" for k in res.totals}
                | {f"period:{k[0]}:{k[1]}:{k[2]}" for k in res.periods} | {f"pair:{k[0]}:{k[1]}" for k in res.pairs}
                | {f"rel:{k[0]}:{k[1]}" for k in res.relations})
        self._hashes = {k: v for k, v in self._hashes.items() if k in live or k.startswith("daily:")}
        self._save_hashes()
        sent.update(people=len(people), totals=len(totals), periods=len(periods), pairs=len(pairs), relations=len(rels))
        return sent

    def forget_hashes(self):
        self._hashes = {}
        if self._hash_file.exists():
            self._hash_file.unlink()

    async def set_pass(self, state: dict, map_updated: bool = False):
        await self.pool.execute(
            """insert into group_info(id, pass) values (1, $1)
               on conflict(id) do update set pass = excluded.pass,
                   map_updated_at = case when $2 then now() else group_info.map_updated_at end""",
            state, map_updated)

    # ------------------------------------------------------------ чтение для бота

    async def call(self, fn: str, *args):
        """select api_*(...) → dict/list/None"""
        marks = ", ".join(f"${i + 1}" for i in range(len(args)))
        return await self.pool.fetchval(f"select {fn}({marks})", *args)

    async def is_member(self, uid: int) -> bool:
        return bool(await self.pool.fetchval("select is_member from people where id = $1", uid))

    async def find_person(self, query: str):
        q = query.strip().lstrip("@")
        if q.lstrip("-").isdigit():
            return await self.pool.fetchrow("select id, person_name(p) as name from people p where id = $1", int(q))
        return await self.pool.fetchrow(
            """select id, person_name(p) as name from people p where lower(username) = lower($1)
               or lower(person_name(p)) = lower($1) or lower(first_name) = lower($1)
               order by is_member desc limit 1""", q)

    # ------------------------------------------------------------ досье и связи (пишет разбор)

    async def dossier(self, uid: int):
        return await self.pool.fetchrow("select * from dossiers where user_id = $1", uid)

    async def save_dossier(self, uid: int, as_of: datetime, summary: str, content: str, data: dict):
        await self.pool.execute(
            """insert into dossiers(user_id, as_of, summary, content, data, updated_at)
               values ($1, $2, $3, $4, $5, now())
               on conflict(user_id) do update set as_of=excluded.as_of, summary=excluded.summary,
                   content=excluded.content, data=excluded.data, updated_at=now()""",
            uid, as_of, summary, content, data)

    async def relation(self, a: int, b: int):
        return await self.pool.fetchrow("select * from relations where a = $1 and b = $2", min(a, b), max(a, b))

    async def save_relation(self, a: int, b: int, as_of: datetime, llm_score: float | None, kind: str | None,
                            tone: str | None, summary: str | None, description: str, data: dict):
        a, b = min(a, b), max(a, b)
        async with self.pool.acquire() as conn, conn.transaction():
            quant = await conn.fetchval("select quant from relations where a = $1 and b = $2", a, b) or 0.0
            await conn.execute(
                """insert into relations(a, b, strength, quant, llm_score, kind, tone, summary, description, data,
                       as_of, updated_at)
                   values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
                   on conflict(a, b) do update set strength=excluded.strength, llm_score=excluded.llm_score,
                       kind=excluded.kind, tone=excluded.tone, summary=excluded.summary,
                       description=excluded.description, data=excluded.data, as_of=excluded.as_of,
                       updated_at=now()""",
                a, b, strength(quant, llm_score), quant, llm_score, kind, tone, summary, description, data, as_of)

    async def save_episode(self, ep_id: int, last_id: int, started: datetime, ended: datetime, n: int,
                           participants: list[int], summary: str, topics: list[str], mood: str, result: dict):
        await self.pool.execute(
            """insert into episodes(id, last_msg_id, started_at, ended_at, n_msgs, participants, summary, topics, mood,
                   result, analyzed_at) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
               on conflict(id) do update set last_msg_id=excluded.last_msg_id, ended_at=excluded.ended_at,
                   n_msgs=excluded.n_msgs, participants=excluded.participants, summary=excluded.summary,
                   topics=excluded.topics, mood=excluded.mood, result=excluded.result, analyzed_at=now()""",
            ep_id, last_id, started, ended, n, participants, summary, topics, mood, result)

    async def episodes_since(self, after: datetime | None, until: datetime):
        if after is None:
            return await self.pool.fetch("select * from episodes where ended_at <= $1 order by started_at", until)
        return await self.pool.fetch(
            "select * from episodes where ended_at > $1 and ended_at <= $2 order by started_at", after, until)

    async def person_ids(self) -> set[int]:
        return {r["id"] for r in await self.pool.fetch("select id from people")}

    async def ensure_people(self, users: list[dict]):
        """Заготовка строки people (досье ссылается на неё); полную запишет выгрузка статистики."""
        if users:
            await self.pool.executemany(
                """insert into people(id, kind, first_name, last_name, username, is_bot, is_premium, is_member)
                   values ($1, $2, $3, $4, $5, $6, $7, $8) on conflict(id) do nothing""",
                [(u["id"], u["kind"] or "user", u["first_name"], u["last_name"], u["username"], bool(u["is_bot"]),
                  bool(u["is_premium"]), bool(u["is_member"])) for u in users])

    async def people_brief(self, ids: list[int]) -> dict:
        rows = await self.pool.fetch(
            """select p.id, person_name(p) as name, p.username, p.first_join, p.is_member, d.summary
               from people p left join dossiers d on d.user_id = p.id where p.id = any($1)""", ids)
        return {r["id"]: dict(r) for r in rows}

    async def relations_among(self, ids: list[int]):
        return await self.pool.fetch(
            "select a, b, kind, tone, summary, strength from relations where a = any($1) and b = any($1) "
            "and summary is not null", ids)
