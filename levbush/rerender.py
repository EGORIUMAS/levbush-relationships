"""Пересобрать Markdown досье и связей из их данных (после правок в render_person / render_relation).

В данных меняется только одно: из текстов вырезаются иероглифы (dossier.clean). Запуск: python -m levbush.rerender
"""
import asyncio

from .cache import Cache
from .config import cfg
from .dossier import clean, render_person, render_relation
from .remote import DB


def _clean_person(data: dict) -> dict:
    for e in data.get("entries", []):
        e["text"] = clean(e["text"])
        if e.get("prev"):
            e["prev"] = clean(e["prev"])
    return data


def _clean_relation(data: dict) -> dict:
    for e in data.get("events", []):
        e["text"] = clean(e["text"])
    for k in ("how", "bond", "dynamics"):
        if data.get(k):
            data[k] = clean(data[k])
    return data


async def main():
    chat = Cache(cfg.cache_db).get("chat", {}) or {}
    db = DB(cfg)
    await db.connect()
    n = m = 0
    for r in await db.pool.fetch("select user_id, data, updated_at from dossiers where data ? 'entries'"):
        # если бот успел обновить досье между чтением и записью — не затираем
        data = _clean_person(r["data"])
        res = await db.pool.execute(
            "update dossiers set content = $1, data = $4, summary = $5 where user_id = $2 and updated_at = $3",
            render_person(data, chat), r["user_id"], r["updated_at"], data, clean(data.get("summary") or "") or None)
        n += res.endswith("1")
    for r in await db.pool.fetch("select a, b, data, updated_at from relations where data ? 'events'"):
        data = _clean_relation(r["data"])
        res = await db.pool.execute(
            "update relations set description = $1, data = $5 where a = $2 and b = $3 and updated_at = $4",
            render_relation(data, chat), r["a"], r["b"], r["updated_at"], data)
        m += res.endswith("1")
    print(f"пересобрано: досье {n}, связей {m}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
