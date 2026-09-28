"""Пересобрать Markdown досье и связей из их данных (после правок в render_person / render_relation).

Данные (data) не меняются — только текст content / description. Запуск: python -m levbush.rerender
"""
import asyncio

from .cache import Cache
from .config import cfg
from .dossier import render_person, render_relation
from .remote import DB


async def main():
    chat = Cache(cfg.cache_db).get("chat", {}) or {}
    db = DB(cfg)
    await db.connect()
    n = m = 0
    for r in await db.pool.fetch("select user_id, data, updated_at from dossiers where data ? 'entries'"):
        # если бот успел обновить досье между чтением и записью — не затираем
        res = await db.pool.execute("update dossiers set content = $1 where user_id = $2 and updated_at = $3",
                                    render_person(r["data"], chat), r["user_id"], r["updated_at"])
        n += res.endswith("1")
    for r in await db.pool.fetch("select a, b, data, updated_at from relations where data ? 'events'"):
        res = await db.pool.execute("update relations set description = $1 where a = $2 and b = $3 and updated_at = $4",
                                    render_relation(r["data"], chat), r["a"], r["b"], r["updated_at"])
        m += res.endswith("1")
    print(f"пересобрано: досье {n}, связей {m}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
