"""Офлайн-проверки без Telegram, базы и GPU: синтетическая группа → статистика, окна, шаги, запрос с медиа.

    ~/.local/share/levbush/venv/bin/python -m pytest -q tests/
"""
import asyncio
import base64
import hashlib
import hmac
import json
import subprocess
import time
from collections import Counter
import urllib.parse
from datetime import datetime, timedelta

import pytest

from levbush import stats as S
from levbush.analyze import Analyzer
from levbush.cache import Cache
from levbush.config import Config
from levbush.episodes import segment
from levbush.llm import LLMError
from levbush.retell import Retell, md_to_tg
from levbush.web import auth_user

CHAT = -1001234567890
CHANNEL = -1009876543210
A, B, C, D = 101, 102, 103, 104
NOW = int(time.time())


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("lb")
    cfg = Config()
    cfg.data_dir = tmp / "data"
    cfg.media_dir = tmp / "media"
    cfg.session_file = tmp / "sess" / "s"
    cfg.ensure_dirs()
    cache = Cache(cfg.cache_db)
    media = tmp / "media"
    from PIL import Image
    Image.new("RGB", (64, 48), (200, 30, 30)).save(media / "p.jpg")
    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "libopus", str(media / "v.ogg"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=240x240:d=2", "-f", "lavfi", "-i", "sine=d=2", "-shortest",
           "-c:v", "libx264", "-c:a", "aac", str(media / "vn.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x120:d=1", "-c:v", "libx264", str(media / "g.mp4"))
    Image.new("RGB", (100, 140), (255, 255, 255)).save(media / "doc.pdf")
    (media / "notes.txt").write_text("список покупок: хлеб, молоко")

    cache.set("chat", {"id": CHAT, "title": "Тест", "username": None, "channel_id": CHANNEL, "channel_title": "Канал"})
    for uid, name, uname in ((A, "Аня", "anya"), (B, "Боря", "borya"), (C, "Вика", None), (D, "Гоша", "gosha")):
        cache.upsert_user({"id": uid, "first_name": name, "username": uname, "is_member": 0 if uid == D else 1})
    cache.upsert_user({"id": CHANNEL, "kind": "channel", "first_name": "Канал"})
    base = NOW - 5 * 86400
    cache.add_membership(A, base - 30 * 86400, "join", "t")
    cache.add_membership(D, base - 10 * 86400, "join", "t")
    cache.add_membership(D, base - 5 * 86400, "leave", "t")          # вышел и вернулся
    cache.add_membership(D, base - 2 * 86400, "join", "t")
    cache.add_membership(D, base + 3 * 86400, "leave", "t")

    msgs = [
        dict(id=1, date=base, sender_id=CHANNEL, text="Пост канала", auto_fwd=1, fwd_date=base),
        dict(id=2, date=base + 60, sender_id=A, text="Привет всем! @borya смотри", reply_to=1,
             entities=[{"t": "mention", "o": 13, "l": 6}]),
        dict(id=3, date=base + 120, sender_id=B, text="Привет, Аня", reply_to=2),
        dict(id=4, date=base + 180, sender_id=A, text="вот фото", media="photo", media_path=str(media / "p.jpg"),
             media_state="ok"),
        dict(id=5, date=base + 240, sender_id=B, text=None, reply_to=4, quote="вот", media="voice",
             media_meta={"duration": 3}, media_path=str(media / "v.ogg"), media_state="ok"),
        dict(id=6, date=base + 300, sender_id=C, media="video_note", media_meta={"duration": 2},
             media_path=str(media / "vn.mp4"), media_state="ok"),
        dict(id=7, date=base + 360, sender_id=A, text="переслала", fwd_from_id=C, fwd_date=base - 100),
        # перерыв > 30 мин — новая беседа
        dict(id=8, date=base + 3 * 3600, sender_id=D, text="есть кто?", media="gif", media_meta={"duration": 1},
             media_path=str(media / "g.mp4"), media_state="ok"),
        dict(id=9, date=base + 3 * 3600 + 60, sender_id=C, text="я тут", reply_to=8),
        dict(id=10, date=base + 3 * 3600 + 120, sender_id=D, media="document",
             media_meta={"file_name": "doc.pdf", "mime": "application/pdf"}, media_path=str(media / "doc.pdf"),
             media_state="ok"),
        dict(id=11, date=base + 3 * 3600 + 180, sender_id=C, media="document",
             media_meta={"file_name": "notes.txt", "mime": "text/plain"}, media_path=str(media / "notes.txt"),
             media_state="ok"),
        dict(id=12, date=NOW - 3600, sender_id=A, text="сегодняшнее", edit_date=NOW - 3500),
        dict(id=13, date=NOW - 3500, sender_id=B, text="ага https://example.com", reply_to=12),
    ]
    with cache.tx() as db:
        for m in msgs:
            cache.upsert_message(m, db)
    cache.set_reactions(3, [(A, "❤", base + 130)])
    cache.set_reactions(12, [(B, "😂", NOW - 3400), (C, "😂", NOW - 3400)])
    cache.set_reactions(1, [(A, "👍", base + 10)])       # реакция на пост канала: без «получено»
    cache.set_transcript(5, "привет это голосовое", 3, "test")
    cache.set_transcript(6, "кружок с приветом", 2, "test")
    return cfg, cache


def test_stats(env):
    cfg, cache = env
    res = S.compute(cache, cfg, now=NOW)
    ta, tb, tc = res.totals[A]["c"], res.totals[B]["c"], res.totals[C]["c"]
    assert ta["msgs"] == 4 and ta["comments"] == 1 and ta["forwards"] == 1 and ta["photos"] == 1
    assert ta["mentions"] == 1 and tb["mentions_recv"] == 1
    assert tb["replies"] == 2 and tb["quotes"] == 1 and tb["voices"] == 1 and tb["voice_sec"] == 3 and tb["links"] == 1
    assert tc["video_notes"] == 1 and tc["documents"] == 1
    assert ta["reactions"] == 2 and ta["reactions_recv"] == 2 and tb["reactions_recv"] == 1
    assert ta["edits"] == 1
    assert res.pairs[(B, A)] == {"replies": 2, "quotes": 1, "reactions": 1} and res.pairs[(A, C)]["forwards"] == 1
    assert res.pairs[(A, B)]["mentions"] == 1 and res.pairs[(A, B)]["reactions"] == 1
    assert CHANNEL not in res.totals and res.people[CHANNEL]["hidden"]
    # сегодняшний день в периодах
    day12 = datetime.fromtimestamp(NOW - 3600, cfg.tz).date()      # день сообщения #12 (у полуночи — вчера)
    assert res.periods[(A, "d", day12)]["msgs"] == 1
    # время в группе с выходом и возвратом: 5 дней + 5 дней
    assert res.people[D]["time_in_group_sec"] == 10 * 86400 and not res.people[D]["is_member"]
    assert res.people[A]["first_join"] == NOW - 35 * 86400
    # у Вики нет события входа — вход выведен из первого сообщения
    assert res.people[C]["first_join_exact"] is False
    assert res.relations[(A, B)]["quant"] == 1.0
    assert res.group["messages"] == 12 and res.group["members"] == 3


def test_windows_and_plan(env):
    cfg, cache = env
    an = Analyzer(cfg, cache, db=None, mgr=None)
    msgs = an.load_messages()
    eps = segment(msgs, cfg.gap_min, cfg.cast_window)
    assert [e.id for e in eps] == [1, 8, 12]
    wins = an.windows(msgs)
    steps = an.plan(wins, NOW)
    assert steps == [(0, 3)]          # все три окна закрыты и влезают в один шаг
    an._mark(wins[0], "done")
    assert an.plan(wins, NOW) == [(1, 3)]
    ctx = an._context(wins, 1, 30000)
    assert "Окно #1" in ctx


def test_interleave_media(env):
    cfg, cache = env
    an = Analyzer(cfg, cache, db=None, mgr=None)
    wins = an.windows(an.load_messages())
    budget = an.media.budget(10 ** 6)
    parts = an.interleave([(f"=== Окно #{w.id} ===", w.msgs) for w in wins[:2]], budget)
    kinds = [p["type"] for p in parts]
    assert kinds.count("image_url") >= 1 + 1 + 1       # фото, кадры немого GIF, страница PDF
    # голосовое + звук кружка отдельной дорожкой (use_audio_in_video не используется: ломается от голосовых)
    assert kinds.count("audio_url") == 2 and kinds.count("video_url") == 1
    assert not budget.audio_in_video
    text = "\n".join(p["text"] for p in parts if p["type"] == "text")
    assert "расшифровка: «привет это голосовое»" in text
    assert "текст файла:\nсписок покупок" in text
    # вложение идёт сразу после своего сообщения
    i = next(i for i, p in enumerate(parts) if p["type"] == "audio_url")
    assert "#5" in parts[i - 1]["text"]
    # voice передаётся как есть (ogg), без перекодирования
    assert parts[i]["audio_url"]["url"].endswith("v.ogg")


class FakeLLM:
    model = None

    def __init__(self):
        self.calls = []

    async def count(self, text):
        return None

    async def chat(self, messages, schema=None, **kw):
        self.calls.append((messages, schema, kw))
        return {"windows": [{"id": 1, "summary": "знакомство", "topics": ["привет"], "mood": "тепло"}],
                "add": [{"person": A, "section": "facts", "text": "живёт в Саратове", "msgs": [2], "certain": True},
                        {"person": 999, "section": "facts", "text": "чужой", "msgs": [], "certain": True},
                        {"person": D, "section": "facts", "text": "со слов Ани: уехал на море", "msgs": [2],
                         "certain": False}],
                "update": [{"person": D, "entry": "e1", "text": "нельзя", "msgs": [], "why": ""}], "remove": [],
                "summaries": [{"person": A, "text": "активная участница"}],
                "relations": [{"a": A, "b": B, "kind": "дружба", "tone": "тёплый", "closeness": 7,
                               "summary": "друзья"}],
                "relation_events": [{"a": B, "b": A, "text": "Боря ответил на приветствие", "msgs": [3]}],
                "relation_notes": [{"a": A, "b": B, "section": "how", "text": "по-дружески", "msgs": [3]}]}


class FakeDB:
    def __init__(self):
        self.saved = []

    async def people_brief(self, ids):
        return {}

    async def person_ids(self):
        return {A, B, D, 104}

    async def ensure_people(self, users):
        self.saved.append(("people", [u["id"] for u in users]))

    async def dossier(self, uid):
        return None

    async def relation(self, a, b):
        return None

    async def call(self, fn, *a):
        return None

    async def save_episode(self, *a):
        self.saved.append(("episode", a[0]))

    async def save_dossier(self, uid, as_of, summary, content, data):
        self.saved.append(("dossier", uid, content, data, summary))

    async def save_relation(self, a, b, as_of, llm, kind, tone, summary, description, data):
        self.saved.append(("relation", a, b, llm, kind, description, data))

    class pool:
        @staticmethod
        async def fetch(*a):
            return []


def test_step(env):
    cfg, cache = env
    db = FakeDB()
    an = Analyzer(cfg, cache, db=db, mgr=None)
    fake = FakeLLM()
    fake.base, fake.model = cfg.llm_url, None
    an._llm = fake
    an.mgr = type("M", (), {"model": None, "url": cfg.llm_url, "fixed_model": "", "label": "Nemotron"})()
    an.stat = {"dossiers": 0, "relations": 0}
    an._text_cache, an._tok_cache = {}, {}
    wins = an.windows(an.load_messages())
    asyncio.run(an.step(wins, 0, 1, NOW))
    messages, schema, kw = fake.calls[0]
    content = messages[0]["content"]
    text = "\n".join(p["text"] for p in content if p["type"] == "text")
    assert "## Новые окна переписки" in text and "Досье пока нет" in text and "Верни только ПРАВКИ" in text
    assert kw["audio_in_video"] is False and "add" in schema["properties"]
    kinds = [x[0] for x in db.saved]
    assert kinds.count("dossier") == 2 and kinds.count("relation") == 1 and ("episode", 1) in db.saved
    assert "## Остальные люди группы" in text and "Гоша (@gosha), id 104" in text
    absent = next(x for x in db.saved if x[0] == "dossier" and x[1] == D)
    assert [e["text"] for e in absent[3]["entries"]] == ["со слов Ани: уехал на море"]
    assert absent[3]["entries"][0]["certain"] is False and absent[3]["entries"][0]["basis"] == "косвенно"
    _, uid, md, data, summary = next(x for x in db.saved if x[0] == "dossier" and x[1] == A)
    base_day = datetime.fromtimestamp(NOW - 5 * 86400 + 60, cfg.tz).strftime("%Y-%m-%d")
    assert uid == A and summary == "активная участница"
    assert data["entries"][0]["since"] == base_day                 # дата — по сообщению #2
    assert "живёт в Саратове — *с " + base_day in md and "https://t.me/c/1234567890/2" in md
    rel = next(x for x in db.saved if x[0] == "relation")
    assert rel[1:5] == (A, B, 0.7, "дружба") and "Боря ответил на приветствие" in rel[5]


class TooLongLLM(FakeLLM):
    """Первый запрос — «не влезает в контекст», дальше как обычно."""
    async def chat(self, messages, schema=None, **kw):
        if not self.calls:
            self.calls.append(None)
            raise LLMError("HTTP 400: This model's maximum context length is 131072 tokens")
        return await super().chat(messages, schema, **kw)


def test_step_splits_window(env):
    cfg, cache = env
    db = FakeDB()
    an = Analyzer(cfg, cache, db=db, mgr=None)
    an._llm = TooLongLLM()
    an._llm.base, an._llm.model = cfg.llm_url, None
    an.mgr = type("M", (), {"model": None, "url": cfg.llm_url, "fixed_model": "", "label": "Nemotron"})()
    an.stat = {"dossiers": 0, "relations": 0}
    an._text_cache, an._tok_cache = {}, {}
    wins = an.windows(an.load_messages())
    asyncio.run(an._run_step(wins, 0, 1, NOW))
    assert len(an._llm.calls) == 3                     # окно целиком не влезло → две половины
    done = cache.db.execute("select episode_id, last_id from analyzed where status = 'done'").fetchall()
    assert [tuple(r) for r in done] == [(wins[0].id, wins[0].last_id)]


def test_dossier_ops():
    from levbush.dossier import apply_person, empty_dossier, prompt_person, render_person
    d = empty_dossier()
    day = lambda msgs: "2025-0%d-01" % (msgs[0] if msgs else 9)  # noqa: E731
    apply_person(d, {"add": [{"person": 1, "section": "facts", "text": "живёт в Москве", "msgs": [1],
                              "certain": True},
                             {"person": 1, "section": "interests", "text": "шахматы", "msgs": [2], "certain": False}]},
                 1, day)
    assert "[e1] живёт в Москве (с 2025-01-01)" in prompt_person(d) and "шахматы (косвенно)" in prompt_person(d)
    apply_person(d, {"update": [{"person": 1, "entry": "e1", "text": "живёт в Саратове", "msgs": [3],
                                 "why": "переехал"}],
                     "remove": [{"person": 1, "entry": "e2", "msgs": [4], "why": "бросил"}]}, 1, day)
    md = render_person(d, {"id": CHAT})
    assert "живёт в Саратове — *с 2025-01-01, обновлено 2025-03-01*" in md and "было" not in md
    assert d["entries"][0]["prev"] == "живёт в Москве"               # прежний текст — в данных, как история
    assert "~~шахматы~~ — *устарело 2025-04-01: бросил*" in md
    assert "шахматы" not in prompt_person(d)                        # устаревшее нейросети не показываем
    assert apply_person(d, {"update": [{"person": 2, "entry": "e1", "text": "x", "msgs": [], "why": ""}]}, 1, day) == 0


def test_retell_parse():
    cfg = Config()
    r = Retell.__new__(Retell)
    r.cfg = cfg
    now = datetime(2026, 9, 27, 15, 0, tzinfo=cfg.tz)
    assert r.parse_since("2ч", now) == now - timedelta(hours=2)
    assert r.parse_since("30 мин", now) == now - timedelta(minutes=30)
    assert r.parse_since("14:30", now).hour == 14
    assert r.parse_since("16:00", now).day == 26                 # в будущем → вчера
    assert r.parse_since("вчера 20:00", now).day == 26
    assert r.parse_since("ерунда", now) is None


def test_md_to_tg():
    out = md_to_tg("**Итог**: <b> [→](msg:5)\n- пункт", {"id": CHAT})
    assert "<b>Итог</b>" in out and "&lt;b&gt;" in out and 'href="https://t.me/c/1234567890/5"' in out
    assert "• пункт" in out


def test_web_auth():
    token = "123456:TEST"
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42})}
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, "\n".join(f"{k}={fields[k]}" for k in sorted(fields)).encode(),
                              hashlib.sha256).hexdigest()
    assert auth_user("tma " + urllib.parse.urlencode(fields), token) == 42
    assert auth_user("tma " + urllib.parse.urlencode(fields) + "x", token) is None
    login = {"id": 7, "first_name": "Аня", "auth_date": int(time.time())}
    dcs = "\n".join(f"{k}={login[k]}" for k in sorted(login))
    login["hash"] = hmac.new(hashlib.sha256(token.encode()).digest(), dcs.encode(), hashlib.sha256).hexdigest()
    hdr = "tglogin " + base64.b64encode(json.dumps(login, ensure_ascii=False).encode()).decode()
    assert auth_user(hdr, token) == 7


def test_tg_pacing(env, monkeypatch):
    from telethon import errors
    from levbush.tg_client import TG
    cfg, cache = env
    tg = TG(cfg, cache)
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr("levbush.tg_client.asyncio.sleep", fake_sleep)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise errors.FloodWaitError(request=None, capture=30)
        return "ok"

    async def go():
        assert await tg.req("reactions", flaky) == "ok"
        await tg.pace("reactions")

    asyncio.run(go())
    assert len(calls) == 2 and tg.floods == 1 and tg.slow == 2.0
    assert any(s >= 30 * 1.1 for s in slept)                      # ждали FloodWait с запасом
    assert slept[-1] >= cfg.tg_reaction_delay * 2 * 0.9           # и дальше идём в два раза медленнее


def test_alias_bots_contact_names(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.aliases = f"{CHAT}=@levbush"
    cache = Cache(tmp_path / "c.db")
    cache.set("chat", {"id": CHAT, "title": "Т", "channel_id": CHANNEL})
    LEV, BOT = 501, 502
    cache.upsert_user({"id": LEV, "first_name": "Лев", "username": "levbush", "is_member": 1})
    cache.upsert_user({"id": BOT, "first_name": "Модератор", "is_bot": 1, "is_member": 1})
    cache.upsert_user({"id": A, "first_name": "Аня", "is_member": 1})
    t0 = NOW - 86400
    with cache.tx() as db:
        for m in (dict(id=1, date=t0, sender_id=CHAT, text="пишу от имени группы"),
                  dict(id=2, date=t0 + 60, sender_id=A, text="ответ Льву", reply_to=1),
                  dict(id=3, date=t0 + 120, sender_id=BOT, text="я бот"),
                  dict(id=4, date=t0 + 180, sender_id=A, text="ответ боту", reply_to=3)):
            cache.upsert_message(m, db)
    cache.set_reactions(3, [(A, "👍", t0 + 200)])
    res = S.compute(cache, cfg, now=NOW)
    assert res.totals[LEV]["c"]["msgs"] == 1 and res.totals[LEV]["c"]["replies_recv"] == 1
    assert res.pairs[(A, LEV)]["replies"] == 1
    assert BOT not in res.totals and BOT not in res.people and (A, BOT) not in res.pairs
    assert res.totals[A]["c"]["replies"] == 1 and "reactions_recv" not in res.totals[A]["c"]
    assert res.group["members"] == 2 and res.group["messages"] == 3
    an = Analyzer(cfg, cache, db=None, mgr=None)
    assert an._persons(an.load_messages()) == [LEV, A]
    assert "Лев (@levbush) [пишет от имени группы] (id 501)" in an.r.line(cache.message(1))
    assert "Модератор [бот] (id 502)" in an.r.line(cache.message(3))
    assert " Модератор [бот]: я бот" in an.r.compact_line(cache.message(3))
    # имена: от Telethon — никогда для контактов и никогда поверх известного; бот пишет как есть
    from telegram import User as BotUser
    from levbush.normalize import ptb_user_row, tt_user_row
    from telethon.tl import types as t
    cache.upsert_user(tt_user_row(t.User(id=LEV, first_name="Лев F & P", contact=True)))
    assert cache.user(LEV)["first_name"] == "Лев"
    cache.upsert_user(tt_user_row(t.User(id=777, first_name="Контакт", username="real", contact=True)))
    assert cache.user(777)["first_name"] is None and cache.user(777)["username"] == "real"
    cache.upsert_user(tt_user_row(t.User(id=A, first_name="Другое имя")))
    assert cache.user(A)["first_name"] == "Аня"
    cache.upsert_user(ptb_user_row(BotUser(LEV, "Лев", False, last_name=None, username="levbush")))
    cache.upsert_user({"id": LEV, "last_name": "F & P"})          # что-то записало фамилию…
    cache.upsert_user(ptb_user_row(BotUser(LEV, "Лев", False)))    # …бот стирает: у профиля её нет
    u = cache.user(LEV)
    assert u["last_name"] is None and u["name_src"] == "bot"


def test_discussed(env):
    cfg, cache = env
    an = Analyzer(cfg, cache, db=None, mgr=None)
    base = {"auto_fwd": 0, "reply_peer": None, "fwd_from_id": None, "entities": None, "reply_to": None}
    msgs = [dict(base, id=900, sender_id=A, text="помнишь, что Гоша писал?", reply_to=8),         # ответ на старое D
            dict(base, id=901, sender_id=B, text="@anya и @gosha", entities=json.dumps(
                [{"t": "mention", "o": 0, "l": 5}, {"t": "mention", "o": 8, "l": 6}]))]
    assert an._discussed(msgs, {A, B}) == [D]          # A пишет сама — не «обсуждаемая»; D — дважды


def test_name_mentions(env):
    cfg, cache = env
    from levbush.dossier import apply_person, empty_dossier, prompt_person, render_person
    an = Analyzer(cfg, cache, db=None, mgr=None)
    an._names = {D: ["Гоша", "Жора"], B: ["Боря"], 103: ["Вика"]}
    an._build_name_index()
    base = {"auto_fwd": 0, "reply_peer": None, "fwd_from_id": None, "entities": None, "reply_to": None, "quote": None}
    msgs = [dict(base, id=950, sender_id=A, text="Жоры сегодня не было, а Гоше передай привет"),   # падежи
            dict(base, id=951, sender_id=A, text="Бор и боровик — не про Борю, а Викторина — не Вика?")]
    got = an._discussed(msgs, {A})
    assert got[0] == D and B in got
    assert 103 in got                    # «Вика» в конце фразы — точное совпадение
    assert an._name_hits("боровик борщ") == Counter()   # короткая основа «бор» не срабатывает
    # операция names: пополняет «Как называют», без повторов (ё = е)
    d = empty_dossier()
    n = apply_person(d, {"names": [{"person": 1, "name": "Лёва"}, {"person": 1, "name": "лева"},
                                   {"person": 2, "name": "чужое"}]}, 1, lambda m: "2025-01-01")
    assert n == 1 and d["names"] == ["Лёва"]
    assert prompt_person(d).startswith("Как называют: Лёва") and "**Как называют:** Лёва" in render_person(d, {})


def test_profile_name_filter(env):
    cfg, cache = env
    an = Analyzer(cfg, cache, db=None, mgr=None)
    cache.upsert_user({"id": 555, "first_name": "Magor Gûl", "username": "levbush"})
    assert an._is_profile_name(555, "Magor Gûl") and an._is_profile_name(555, "levbush")
    assert an._is_profile_name(555, "magor gûl 🇷🇺")          # регистр и эмодзи не спасают дословный повтор
    assert not an._is_profile_name(555, "Магор") and not an._is_profile_name(555, "Лёва")


def test_wanted_media():
    from levbush.normalize import wanted
    assert wanted("photo", {}) and wanted("voice", {}) and wanted("sticker", {"mime": "image/webp"})
    assert not wanted("sticker", {"mime": "application/x-tgsticker"})
    assert wanted("document", {"mime": "application/pdf"}) and wanted("document", {"file_name": "notes.TXT"})
    assert wanted("document", {"mime": "video/mp4"}) and wanted("document", {"mime": "image/png"})
    for bad in ({"file_name": "a.zip"}, {"file_name": "x.docx"}, {"mime": "application/octet-stream"},
                {"file_name": "app.apk", "mime": "application/vnd.android.package-archive"}):
        assert not wanted("document", bad)
    assert not wanted("poll", {}) and not wanted("webpage", {})


def test_maintenance_guard(tmp_path):
    from levbush.bot import Levbush
    lb = Levbush.__new__(Levbush)
    lb.cfg = Config()
    lb.cfg.admin_id = 1
    lb.cache = Cache(tmp_path / "c.db")
    replies, calls = [], []

    class Msg:
        async def reply_text(self, text, **kw):
            replies.append(text)

    def upd(uid):
        return type("U", (), {"effective_user": type("X", (), {"id": uid})(), "effective_message": Msg(),
                              "callback_query": None})()

    async def handler(update, ctx):
        calls.append(update.effective_user.id)

    guarded = lb.guard(handler)

    async def go():
        await guarded(upd(2), None)                        # режима нет — участнику можно
        lb.cache.set("maintenance", {"since": 0, "reason": "обновляю базу"})
        await guarded(upd(2), None)                        # участнику нельзя
        await guarded(upd(1), None)                        # админу можно
        lb.cache.set("maintenance", None)
        await guarded(upd(2), None)

    asyncio.run(go())
    assert calls == [2, 1, 2]
    assert len(replies) == 1 and "техобслуживание: обновляю базу" in replies[0]


def test_bidi_close():
    from levbush.bot import bidi_close, esc
    nick = 'maxim("⁧("'
    assert bidi_close(nick) == nick + "⁩" and bidi_close("Аня") == "Аня"
    assert bidi_close("a⁧b⁩") == "a⁧b⁩"          # уже закрыт — не трогаем
    assert esc(nick).endswith("⁩")


def test_md_ranges():
    out = md_to_tg("шутка [→](msg:253580‑253581), команда `/stats`", {"id": CHAT})
    assert 'href="https://t.me/c/1234567890/253580"' in out and "<code>/stats</code>" in out


def test_invisible_name(tmp_path):
    from levbush.render import Renderer, visible
    assert not visible("️" * 9) and not visible("ㅤ") and visible("ㅤа̀dmin") and visible("🐸")
    cfg = Config()
    cfg.data_dir = tmp_path
    cache = Cache(tmp_path / "c.db")
    cache.upsert_user({"id": 9, "first_name": "️" * 9, "username": "yMep"})
    assert Renderer(cache, cfg).name(9) == "yMep (@yMep)"


def test_qwen_media(env):
    """Qwen: картинки как есть; видео — кадры + описание Nemotron; голосовое — только описание (текст в строке)."""
    cfg, cache = env
    qwen = type("Q", (), {"model": "qwen", "url": cfg.qwen_url, "fixed_model": "qwen", "label": "Qwen"})()
    an = Analyzer(cfg, cache, db=None, mgr=None, qwen=qwen)
    msgs = {m["id"]: m for m in an.load_messages()}
    voice = next(m for m in msgs.values() if m["media"] == "voice")
    note = next(m for m in msgs.values() if m["media"] == "video_note")
    cache.set_media_desc(voice["id"], "весёлый тон, смех", "nemotron")
    cache.set_media_desc(note["id"], "парень машет рукой", "nemotron")
    b = an.media.budget(10 ** 6)
    parts = an.media_parts(voice, b)
    assert [p["type"] for p in parts] == ["text"] and "весёлый тон" in parts[0]["text"]
    parts = an.media_parts(note, b)
    kinds = [p["type"] for p in parts]
    assert kinds.count("image_url") == cfg.video_frames and "audio_url" not in kinds and "video_url" not in kinds
    assert "парень машет рукой" in parts[-1]["text"] and "Parakeet" in parts[-1]["text"]
    photo = next(m for m in msgs.values() if m["media"] == "photo")
    assert [p["type"] for p in an.media_parts(photo, b)] == ["text", "image_url"]
    # оценка для планирования — без нарезки кадров
    assert an.media_cost(note) >= cfg.video_frames * 500
    assert [m["id"] for m in an._undescribed()] and voice["id"] not in [m["id"] for m in an._undescribed()]


def test_progress_text_and_eta():
    from levbush.bot import Levbush
    assert Analyzer._eta([10, 20], 3) == 45 and Analyzer._eta([], 0) == 0 and Analyzer._eta([], 5) is None
    st = {"state": "running", "stage": "шаги разбора (Qwen 3.8 27B)", "done": 42, "total": 857, "eta": 7200,
          "step_date": "2026-05-01", "started": 0}
    text = Levbush.progress_text(type("B", (), {})(), st)
    assert "42/857 (4 %)" in text and "переписка за 2026-05-01" in text and "осталось примерно 2 ч" in text
    assert Levbush.progress_text(None, {"state": "done"}) == ""


def test_basis_marks():
    from levbush.dossier import apply_person, empty_dossier, render_person
    d = empty_dossier()
    day = lambda msgs: "2025-01-01"  # noqa: E731
    apply_person(d, {"add": [{"person": 1, "section": "quirks", "text": "пьёт квас литрами", "msgs": [], "basis": "сам"},
                             {"person": 1, "section": "facts", "text": "со слов Ани: переехал", "msgs": [],
                              "basis": "со слов других"}]}, 1, day)
    md = render_person(d, {})
    assert "## Мелочи и привычки" in md and "пьёт квас литрами — *с" in md and "*(со слов других)*" in md
    apply_person(d, {"update": [{"person": 1, "entry": "e2", "text": "переехал в Казань", "msgs": [], "why": "подтвердил",
                                 "basis": "сам"}]}, 1, day)
    assert d["entries"][1]["basis"] == "сам" and d["entries"][1]["certain"]
