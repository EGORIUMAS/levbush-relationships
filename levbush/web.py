"""Этап 2: локальный API сайта вместо Supabase Edge Function (контракт тот же — docs/api.md) + раздача web/.

`levbush web --port 8095` → https://<хост>/api?q=… (TLS — через Caddy). В web/config.js: api: "/api".
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web

from .config import ROOT, Config
from .remote import DB

log = logging.getLogger("levbush.web")


def _check(fields: dict, key: bytes) -> bool:
    got = fields.get("hash", "")
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields) if k != "hash")
    want = hmac.new(key, dcs.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, got)


def auth_user(header: str | None, token: str, login_max_age=30 * 86400, webapp_max_age=86400) -> int | None:
    if not header or " " not in header:
        return None
    scheme, payload = header.split(" ", 1)
    now = time.time()
    if scheme.lower() == "tma":
        fields = dict(parse_qsl(payload.strip(), keep_blank_values=True))
        key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        if not _check(fields, key) or now - int(fields.get("auth_date", 0)) > webapp_max_age:
            return None
        try:
            return int(json.loads(fields["user"])["id"])
        except (KeyError, ValueError):
            return None
    if scheme.lower() == "tglogin":
        try:
            obj = json.loads(base64.b64decode(payload.strip()).decode())
        except ValueError:
            return None
        fields = {k: str(v) for k, v in obj.items() if v is not None}
        if not _check(fields, hashlib.sha256(token.encode()).digest()):
            return None
        if now - int(fields.get("auth_date", 0)) > login_max_age:
            return None
        return int(fields["id"])
    return None


def make_app(cfg: Config, db: DB) -> web.Application:
    def reply(body, status=200):
        return web.json_response(body, status=status, headers={"Cache-Control": "no-store",
                                                               "Access-Control-Allow-Origin": "*"},
                                 dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str))

    async def api(request: web.Request):
        if request.method == "OPTIONS":
            return web.Response(status=204, headers={"Access-Control-Allow-Origin": "*",
                                                     "Access-Control-Allow-Headers": "authorization",
                                                     "Access-Control-Allow-Methods": "GET, OPTIONS"})
        uid = auth_user(request.headers.get("Authorization"), cfg.bot_token)
        if not uid:
            return reply({"error": "auth"}, 401)
        if uid != cfg.admin_id and not await db.is_member(uid):
            return reply({"error": "not_member"}, 403)
        q = request.query

        def num(name):
            v = q.get(name, "")
            return int(v) if v.lstrip("-").isdigit() else None

        kind = q.get("q")
        if kind == "graph":
            data = await db.call("api_graph")
            return reply({**data, "me": uid})
        calls = {"person": ("api_person", ("id",)), "relation": ("api_relation", ("a", "b"))}
        if kind not in calls:
            return reply({"error": "unknown_query"}, 400)
        fn, names = calls[kind]
        args = [num(n) for n in names]
        if any(a is None for a in args):
            return reply({"error": "args"}, 400)
        data = await db.call(fn, *args)
        return reply(data) if data is not None else reply({"error": "not_found"}, 404)

    async def index(request):
        return web.FileResponse(ROOT / "web" / "index.html")

    app = web.Application()
    app.router.add_route("*", "/api", api)
    app.router.add_get("/", index)
    app.router.add_static("/", ROOT / "web")
    return app


async def serve(cfg: Config, host: str, port: int):
    db = DB(cfg)
    await db.connect()
    runner = web.AppRunner(make_app(cfg, db))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("API и сайт: http://%s:%d/", host, port)
    try:
        import asyncio
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await db.close()
