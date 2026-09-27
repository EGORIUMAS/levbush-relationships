"""Проверка на настоящем Postgres (миграция, выгрузка, api_*, версии, локальный API).

    docker run -d --rm --name levbush-pgtest -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17-alpine
    LEVBUSH_TEST_DB=postgresql://postgres:test@127.0.0.1:55432/postgres python -m pytest -q tests/test_db.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta

import pytest

from levbush import stats as S
from levbush.remote import DB
from test_offline import A, B, D, NOW, env  # noqa: F401 — фикстура

URL = os.environ.get("LEVBUSH_TEST_DB")
pytestmark = pytest.mark.skipif(not URL, reason="нет LEVBUSH_TEST_DB")


def run(coro):
    return asyncio.run(coro)


def test_push_and_api(env):  # noqa: F811
    cfg, cache = env
    cfg.database_url = URL

    async def go():
        db = DB(cfg)
        await db.connect()
        try:
            async with db.pool.acquire() as c:
                await c.execute("drop schema public cascade; create schema public;")
            await db.migrate()
            await db.migrate()                                   # идемпотентно
            res = S.compute(cache, cfg, now=NOW)
            sent = await db.push_stats(res, cfg)
            assert sent["people"] == len(res.people) and sent["periods"] > 0
            again = await db.push_stats(res, cfg)                # ничего не изменилось — ничего не шлём
            assert again["people"] == 0 and again["totals"] == 0
            g = await db.call("api_graph")
            assert {n["id"] for n in g["nodes"]} == {A, B, 103, D}
            assert any(link["a"] == A and link["b"] == B for link in g["links"])
            assert g["group"]["title"] == "Тест"
            p = await db.call("api_person", A)
            assert p["total"]["c"]["msgs"] == 4 and len(p["periods"]["d"]) == 30
            assert p["periods"]["d"][0]["c"].get("msgs") == 1
            assert p["avg_per_day"]["msgs"] > 0
            top = await db.call("api_top", "msgs", "a", 5)
            assert top[0]["id"] == A

            # досье и связь — одна строка, без версий
            when = datetime(2026, 9, 21, tzinfo=cfg.tz)
            await db.save_dossier(A, when, "s1", "c1", {"entries": [], "next": 1, "summary": "s1"})
            await db.save_dossier(A, when + timedelta(days=3), "s2", "c2", {"entries": [], "next": 2, "summary": "s2"})
            p = await db.call("api_person", A)
            assert p["dossier"]["content"] == "c2" and "dossier_versions" not in p
            assert (await db.dossier(A))["data"]["next"] == 2

            await db.save_relation(A, B, when, 0.8, "дружба", "тёплый", "s", "d", {"events": []})
            r = await db.call("api_relation", B, A)
            assert r["relation"]["kind"] == "дружба" and r["a"]["id"] == B and "versions" not in r
            assert abs(r["relation"]["strength"] - (0.4 * 1.0 + 0.6 * 0.8)) < 1e-3
            # пересчёт статистики не затирает оценку нейросети
            db.forget_hashes()
            await db.push_stats(res, cfg)
            r = await db.call("api_relation", A, B)
            assert r["relation"]["llm_score"] == 0.8 and r["relation"]["description"] == "d"

            await db.set_pass({"state": "running", "done": 1}, map_updated=True)
            assert (await db.call("api_group"))["pass"]["state"] == "running"
            await db.set_app_secrets("123:T", 42)

            # локальный API (этап 2)
            from aiohttp.test_utils import TestClient, TestServer
            from levbush.web import make_app
            cfg.bot_token, cfg.admin_id = "123:T", 0
            fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": A})}
            key = hmac.new(b"WebAppData", b"123:T", hashlib.sha256).digest()
            fields["hash"] = hmac.new(key, "\n".join(f"{k}={fields[k]}" for k in sorted(fields)).encode(),
                                      hashlib.sha256).hexdigest()
            hdr = {"Authorization": "tma " + urllib.parse.urlencode(fields)}
            async with TestClient(TestServer(make_app(cfg, db))) as cl:
                resp = await cl.get("/api?q=graph", headers=hdr)
                assert resp.status == 200 and (await resp.json())["me"] == A
                resp = await cl.get(f"/api?q=relation&a={A}&b={B}", headers=hdr)
                assert (await resp.json())["relation"]["kind"] == "дружба"
                assert (await cl.get("/api?q=graph")).status == 401
                resp = await cl.get("/")
                assert resp.status == 200 and "<html" in (await resp.text()).lower()
            # вышедший участник не пускается
            fields2 = dict(fields, user=json.dumps({"id": D}))
            fields2.pop("hash")
            fields2["hash"] = hmac.new(key, "\n".join(f"{k}={fields2[k]}" for k in sorted(fields2)).encode(),
                                       hashlib.sha256).hexdigest()
            async with TestClient(TestServer(make_app(cfg, db))) as cl:
                resp = await cl.get("/api?q=graph", headers={"Authorization": "tma " + urllib.parse.urlencode(fields2)})
                assert resp.status == 403
        finally:
            await db.close()

    run(go())
