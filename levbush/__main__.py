"""levbush — командная строка.

  levbush login      вход Telethon (один раз, интерактивно) и проверка группы
  levbush run        бот + сбор + задания (это запускает systemd-юнит)
  levbush sync       докачать историю, участников, профили, медиа (бот должен быть остановлен)
  levbush stats      пересчитать статистику и выгрузить в базу
  levbush analyze    разбор нейросетью (если бот остановлен; при запущенном — /analyze в боте)
  levbush migrate    применить sql/ к базе (для локального Postgres)
  levbush web        этап 2: локальный API + сайт
  levbush status     что в кэше
  levbush index      индекс поиска /find: полнотекстовый + векторы (--device auto|cpu|cuda, --threads N)
  levbush find ЗАПРОС  поиск по истории из консоли (как /find)
"""
import argparse
import asyncio
import logging
import sys

from .config import cfg


def _setup_logging(verbose: bool):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "telethon", "apscheduler", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _login():
    from .cache import Cache
    from .tg_client import TG
    cfg.ensure_dirs()
    tg = TG(cfg, Cache(cfg.cache_db))
    try:
        await tg.connect(interactive=True)
        me = await tg.client.get_me()
        chat = tg.cache.get("chat")
        print(f"Вход: {me.first_name} (@{me.username}), id {me.id}")
        print(f"Группа: {chat['title']} ({chat['id']})" + (f", канал: {chat['channel_title']}" if chat.get("channel_id") else ""))
    finally:
        await tg.close()


async def _sync():
    from .cache import Cache
    from .tg_client import TG
    cfg.ensure_dirs()
    cache = Cache(cfg.cache_db)
    tg = TG(cfg, cache)
    await tg.connect()

    async def progress(done, total=None, what="сообщения"):
        print(f"\r{what}: {done}" + (f" / ~{total}" if total else "") + "   ", end="", flush=True)

    try:
        print(f"участников: {await tg.sync_participants()}")
        n = await tg.sync_history(progress=progress)
        print(f"\nсообщений: {n}")
        await tg.sync_profiles()
        total = 0
        while (k := await tg.download_pending(limit=500, progress=progress)):
            total += k
        print(f"\nмедиа: {total}")
    finally:
        await tg.close()


async def _stats():
    from . import stats as S
    from .cache import Cache
    from .remote import DB
    cache = Cache(cfg.cache_db)
    res = S.compute(cache, cfg)
    print(f"людей {len(res.people)}, пар {len(res.pairs)}, связей {len(res.relations)}, периодов {len(res.periods)}")
    db = DB(cfg)
    await db.connect()
    try:
        print("выгружено:", await db.push_stats(res, cfg))
    finally:
        await db.close()


async def _analyze(reason: str):
    import fcntl
    import os
    from .analyze import Analyzer
    from .cache import Cache
    from .gpu import LLMManager
    from .remote import DB
    fd = os.open(cfg.data_dir / "telethon.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("бот запущен — запусти разбор командой /analyze в боте")
    db = DB(cfg)
    await db.connect()
    async def say(text):
        print(text, flush=True)

    mgr = LLMManager(cfg, notify=say)
    qwen = LLMManager(cfg, notify=say, kind="qwen") if cfg.qwen_model_path else None
    if qwen:
        mgr.peers, qwen.peers = [qwen], [mgr]
    try:
        print(await Analyzer(cfg, Cache(cfg.cache_db), db, mgr, notify=say, qwen=qwen).run(reason))
    finally:
        for m in (mgr, qwen):
            if m and m.started_by_us:
                await m.stop()
        await db.close()


async def _migrate():
    from .remote import DB
    db = DB(cfg)
    await db.connect()
    try:
        await db.migrate()
        print("схема применена")
    finally:
        await db.close()


def _status():
    from .cache import Cache
    c = Cache(cfg.cache_db).db
    q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
    print(f"сообщений {q('select count(*) from messages')}, людей {q('select count(*) from users')}, "
          f"реакций {q('select count(*) from reactions')}, "
          f"медиа в очереди {q("select count(*) from messages where media_state = 'pending'")}, "
          f"расшифровок {q('select count(*) from transcripts')}, "
          f"разобрано окон {q("select count(*) from analyzed where status = 'done'")}")


async def _index(device: str, images: bool, threads: int | None):
    from .cache import Cache
    from .gpu import LLMManager
    from .search import SearchService
    if threads:
        cfg.embed_threads = threads
    svc = SearchService(cfg, Cache(cfg.cache_db))
    print("полнотекстовый:", svc.indexer.sync())
    text, img = svc.indexer.pending()
    # модели бота — чтобы не занять GPU, пока они работают (сами они про консольную индексацию не знают)
    servers = [LLMManager(cfg), LLMManager(cfg, kind="qwen")]
    dev, why = svc.gpu_verdict(text + (img if images else 0), device, servers)
    print(f"в очереди текстов {text}, картинок {img if images else 0} — {dev} ({why}), "
          f"примерно {svc.estimate(text, img if images else 0, dev) // 60} мин")

    async def progress(st):
        print(f"\r{st['stage']}: {st['done']}/{st['total']}, осталось ~{st.get('eta', 0) // 60} мин  "
              + st.get("note", ""), end="", flush=True)

    try:
        print("\n", await svc.run_index(device, servers=servers, progress=progress, images=images))
    finally:
        await svc.close()


async def _find(query: str):
    from datetime import datetime
    import html
    import re
    from .cache import Cache
    from .render import Renderer
    from .search import SearchService, format_page
    cache = Cache(cfg.cache_db)
    svc = SearchService(cfg, cache)
    try:
        svc.indexer.sync()
        q, hits, semantic = await svc.find(query, datetime.now(cfg.tz))
        if q.error:
            sys.exit(q.error)
        text = format_page(hits, 0, 15, q, Renderer(cache, cfg), svc.sdb, svc.indexer.stem,
                           lambda x: html.escape(str(x), quote=False), semantic)
        print(html.unescape(re.sub(r"<[^>]+>", "", text)))
    finally:
        await svc.close()


def main():
    p = argparse.ArgumentParser(prog="levbush", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["login", "run", "sync", "stats", "analyze", "migrate", "web", "status", "index",
                                        "find"])
    p.add_argument("query", nargs="*", help="find: запрос")
    p.add_argument("--device", default=None, help="index: auto | cpu | cuda (по умолчанию LEVBUSH_EMBED_DEVICE)")
    p.add_argument("--threads", type=int, default=None, help="index: потоков torch на CPU")
    p.add_argument("--no-images", action="store_true", help="index: без фото")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8095)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    _setup_logging(a.verbose)
    if a.command == "run":
        from .bot import main as run
        run()
    elif a.command == "login":
        asyncio.run(_login())
    elif a.command == "sync":
        asyncio.run(_sync())
    elif a.command == "stats":
        asyncio.run(_stats())
    elif a.command == "analyze":
        asyncio.run(_analyze("вручную (CLI)"))
    elif a.command == "migrate":
        asyncio.run(_migrate())
    elif a.command == "web":
        from .web import serve
        asyncio.run(serve(cfg, a.host, a.port))
    elif a.command == "status":
        _status()
    elif a.command == "index":
        asyncio.run(_index(a.device, not a.no_images, a.threads))
    elif a.command == "find":
        asyncio.run(_find(" ".join(a.query)))


if __name__ == "__main__":
    main()
