"""Офлайн-проверки поиска /find: индекс, правки и удаления, гибридная выдача, фильтры, страница — без модели и GPU
(эмбеддер подменён: «мешок основ слов» → вектор).

    ~/.local/share/levbush/venv/bin/python -m pytest -q tests/test_search.py
"""
import array
import asyncio
import hashlib
import math
import sys
import time
from datetime import datetime

import pytest

from levbush import search as SR
from levbush.cache import Cache
from levbush.config import Config
from levbush.render import Renderer

CHAT = -1001234567890
A, B, BOT = 201, 202, 299
DIM = 64
NOW = int(time.time())
DAY = 86400
T0 = int(datetime(2026, 5, 10, 12, 0).timestamp())       # май 2026


class FakeEmbedder:
    """Вектор = сумма хэшей основ слов (синонимы — через SYN); картинка — по имени файла."""
    SYN = {"питер": "петербург", "еда": "шашлык", "кот": "котик"}

    def __init__(self, device="cpu", modality="vision"):
        self.device, self.modality = device, modality
        self.stem = SR.Stemmer()
        self.calls = 0
        self.alive = True

    def vec(self, text: str) -> bytes:
        v = [0.0] * DIM
        for w in self.stem.words(text):
            if w in SR.STOP or w in ("query", "document"):
                continue
            s = self.stem.stem(w)
            s = self.stem.stem(self.SYN.get(s, s))
            h = int(hashlib.md5(s.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return array.array("f", [x / n for x in v]).tobytes()

    async def embed(self, items):
        self.calls += 1
        out, errors = [], {}
        for i, it in enumerate(items):
            if "image" in it:
                if "битая" in it["image"]:
                    out.append(None)
                    errors[i] = "не картинка"
                else:
                    out.append(self.vec(it["image"].rsplit("/", 1)[-1].split(".")[0]))
            else:
                out.append(self.vec(it["text"]))
        return out, errors

    async def close(self):
        self.alive = False


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.media_dir = tmp_path / "media"
    cfg.session_file = tmp_path / "sess" / "s"
    cfg.embed_dim = DIM
    cfg.embed_model = str(tmp_path / "model")
    cfg.aliases = ""
    cfg.ensure_dirs()
    (tmp_path / "model").mkdir()
    cache = Cache(cfg.cache_db)
    media = cfg.media_dir
    from PIL import Image
    for name in ("кот", "море", "битая"):
        Image.new("RGB", (32, 32), (10, 200, 10)).save(media / f"{name}.jpg")
    (media / "план.txt").write_text("план поездки: вокзал в восемь утра, билеты у Бори")
    cache.set("chat", {"id": CHAT, "title": "Тест", "username": None})
    cache.upsert_user({"id": A, "first_name": "Аня", "username": "anya"})
    cache.upsert_user({"id": B, "first_name": "Боря", "username": "borya"})
    cache.upsert_user({"id": BOT, "first_name": "Помощник", "username": "helper_bot", "is_bot": 1})
    msgs = [
        dict(id=1, date=T0, sender_id=A, text="Едем в Питер на выходных, кто с нами?"),
        dict(id=2, date=T0 + 60, sender_id=B, text="да"),                                  # короткое — без вектора
        dict(id=3, date=T0 + 120, sender_id=B, media="voice", media_meta={"duration": 5}, media_state="ok",
             media_path=str(media / "v.ogg")),
        dict(id=4, date=T0 + 180, sender_id=A, text="смотрите кто пришёл", media="photo", media_state="ok",
             media_path=str(media / "кот.jpg"), grouped_id=77),
        dict(id=5, date=T0 + 181, sender_id=A, media="photo", media_state="ok", media_path=str(media / "море.jpg"),
             grouped_id=77),
        dict(id=6, date=T0 + 240, sender_id=B, media="document", media_state="ok", media_path=str(media / "план.txt"),
             media_meta={"file_name": "план.txt", "mime": "text/plain"}),
        dict(id=7, date=T0 + 40 * DAY, sender_id=BOT, text="Напоминание: шашлыки в субботу у реки"),
        dict(id=8, date=T0 + 300, sender_id=A, service="pin", text=None),                 # служебное — мимо
        dict(id=9, date=T0 + 360, sender_id=B, media="photo", media_state="ok", media_path=str(media / "битая.jpg")),
        dict(id=10, date=T0 + 41 * DAY, sender_id=A, text="Купила билеты на поезд до Петербурга"),
    ]
    with cache.tx() as db:
        for m in msgs:
            cache.upsert_message(m, db)
    cache.set_transcript(3, "я тоже хочу поехать, возьму палатку", 5, "test")
    return cfg, cache


def service(cfg, cache):
    return SR.SearchService(cfg, cache)


def embed(svc, emb=None):
    return asyncio.run(svc.indexer.embed(emb or FakeEmbedder()))


def find(svc, raw, qvec=True):
    q = SR.parse_query(raw, svc.cache, datetime.fromtimestamp(NOW, svc.cfg.tz))
    assert not q.error, q.error
    v = FakeEmbedder().vec(q.text) if qvec and q.text else None
    return [h.msg_id for h in svc.searcher.search(q, v)]


def test_sync_documents(env):
    cfg, cache = env
    svc = service(cfg, cache)
    assert svc.indexer.sync() == {"updated": 9, "removed": 0}              # служебное #8 не индексируется
    it = {r["msg_id"]: r for r in svc.sdb.db.execute("select * from items")}
    assert 8 not in it
    assert it[2]["sig"] is None and it[2]["body"] == "да"                   # только полнотекстовый
    assert it[1]["sig"] and it[3]["sig"]                                     # голосовое — по расшифровке
    assert "расшифровка: я тоже хочу поехать" in it[3]["body"]
    assert "текст файла: план поездки" in it[6]["body"] and "файл «план.txt»" in it[6]["body"]
    assert it[4]["image_state"] == "todo" and it[5]["image_state"] == "todo" and it[1]["image_state"] is None
    assert svc.indexer.sync() == {"updated": 0, "removed": 0}              # повторно — ничего
    assert svc.indexer.pending() == (6, 3)


def test_embed_and_hybrid(env):
    cfg, cache = env
    svc = service(cfg, cache)
    svc.indexer.sync()
    res = embed(svc)
    assert res["text"] == 6 and res["images"] == 3 and res["errors"] == 1
    c = svc.sdb.counts()
    assert c["text_vecs"] == 6 and c["images"] == 2 and c["images_error"] == 1 and svc.indexer.pending() == (0, 0)
    # «поездка» — основа совпадает с «поехать» не всегда; вектор + FTS находят и план, и голосовое
    hits = find(svc, "поездка")
    assert 6 in hits[:3]
    # синоним через вектор: «Петербург» ↔ «Питер»
    assert set(find(svc, "Петербург")[:2]) == {1, 10}
    # фото по смыслу: кот — альбом (#4, #5) одной строкой
    hits = find(svc, "котик")
    assert hits[0] in (4, 5) and not {4, 5} <= set(hits)
    # без вектора запроса — только по словам (основы: «шашлык» ↔ «шашлыки»)
    assert find(svc, "шашлык", qvec=False) == [7]


def test_filters(env):
    cfg, cache = env
    svc = service(cfg, cache)
    svc.indexer.sync()
    embed(svc)
    assert set(find(svc, "Петербург @anya")) <= {1, 4, 5, 10}
    assert 10 not in find(svc, "Петербург за:05.2026")                     # #10 — в июне
    assert 10 in find(svc, "Петербург с:15.06.2026")
    only_b = find(svc, "@borya за:05.2026")
    assert only_b and all(cache.message(i)["sender_id"] == B for i in only_b)
    assert find(svc, '"на выходных"', qvec=False) == [1]


def test_parse_query(env):
    cfg, cache = env
    now = datetime(2026, 9, 29, 15, 0, tzinfo=cfg.tz)
    q = SR.parse_query('шашлык @borya за:05.2026 "у реки"', cache, now)
    assert q.sender == B and q.text == "шашлык у реки" and q.phrases == ["у реки"]
    assert datetime.fromtimestamp(q.since, cfg.tz).date().isoformat() == "2026-05-01"
    assert datetime.fromtimestamp(q.until, cfg.tz).date().isoformat() == "2026-06-01"
    assert SR.parse_query("от:Ан", cache, now).sender == A                 # начало имени
    assert SR.parse_query("x с:01.05 по:20.06", cache, now).period_label == "с 01.05.2026 по 20.06.2026"
    assert SR.parse_query("x за:12.12", cache, now).since == int(datetime(2025, 12, 12, tzinfo=cfg.tz).timestamp())
    assert SR.parse_query("x @nobody", cache, now).error
    assert SR.parse_query("x за:вчера", cache, now).period_label == "вчера"
    assert SR.parse_query("x за:32.13", cache, now).error


def test_edit_and_delete(env):
    cfg, cache = env
    svc = service(cfg, cache)
    svc.indexer.sync()
    embed(svc)
    # правка: текст и edit_date — документ пересобирается, вектор пересчитывается, старые слова не находятся
    cache.db.execute("update messages set text = ?, edit_date = ? where id = 1",
                     ("Едем на дачу в субботу", T0 + 500))
    assert svc.indexer.sync()["updated"] == 1
    assert svc.indexer.pending() == (1, 0)
    assert 1 not in find(svc, "Питер", qvec=False)
    assert find(svc, "дача", qvec=False) == [1]
    embed(svc)
    assert svc.indexer.pending() == (0, 0)
    # правка сократила текст до пары букв — вектор удаляется
    cache.db.execute("update messages set text = 'ок', edit_date = ? where id = 10", (T0 + 600,))
    svc.indexer.sync()
    assert svc.sdb.db.execute("select count(*) from vec_text where msg_id = 10").fetchone()[0] == 0
    # новая расшифровка у голосового без изменения сообщения — тоже пересборка
    cache.set_transcript(3, "беру гитару", 5, "test")
    time.sleep(1)                                              # created — в секундах
    cache.db.execute("update transcripts set created = created + 1 where msg_id = 3")
    assert svc.indexer.sync()["updated"] == 1 and find(svc, "гитара", qvec=False) == [3]
    # удаление — из всех индексов сразу
    cache.db.execute("update messages set deleted = 1 where id in (4, 6)")
    assert svc.indexer.sync()["removed"] == 2
    for t in ("items", "vec_text", "vec_image"):
        assert svc.sdb.db.execute(f"select count(*) from {t} where msg_id in (4, 6)").fetchone()[0] == 0
    assert 6 not in find(svc, "план поездки")
    # вектор посчитан по старому тексту, а сообщение успели поправить — не записывается
    svc.indexer._put_vecs("vec_text", [(7, "устаревший", FakeEmbedder().vec("что угодно"))])
    assert svc.sdb.db.execute("select count(*) from items where msg_id = 7 and vec_sig = 'устаревший'").fetchone()[0] == 0


def test_format_page(env):
    cfg, cache = env
    svc = service(cfg, cache)
    svc.indexer.sync()
    embed(svc)
    q = SR.parse_query("шашлыки", cache, datetime.fromtimestamp(NOW, cfg.tz))
    hits = svc.searcher.search(q, FakeEmbedder().vec("шашлыки"))
    text = SR.format_page(hits, 0, 3, q, Renderer(cache, cfg), svc.sdb, svc.indexer.stem, str, True)
    assert "https://t.me/c/1234567890/7" in text
    assert "Помощник [бот]" in text                                         # бот помечен
    assert "<b>шашлыки</b>" in text
    assert "1–" in text and f"из {len(hits)}" in text
    empty = SR.format_page([], 0, 3, q, Renderer(cache, cfg), svc.sdb, svc.indexer.stem, str, False)
    assert "Ничего не нашёл" in empty and "только по словам" in empty
    sn = SR.snippet("а" * 300 + " нужное слово " + "б" * 300, {"нужн"}, svc.indexer.stem)
    assert sn.startswith("…") and "<b>нужное</b>" in sn and sn.endswith("…")


def test_gpu_switch_and_verdict(env, monkeypatch):
    cfg, cache = env
    svc = service(cfg, cache)
    svc.indexer.sync()
    gpu = FakeEmbedder("cuda")
    switched = []

    async def should_switch(cur):
        if cur.device == "cuda" and not switched:
            switched.append(cur)
            return FakeEmbedder("cpu")
        return None

    res = asyncio.run(svc.indexer.embed(gpu, should_switch=should_switch))
    assert not gpu.alive and svc.indexer.embedder.device == "cpu" and res["text"] == 6
    # где считать
    from levbush import gpu as G
    monkeypatch.setattr(G, "h3_busy", lambda: None)
    monkeypatch.setattr(G, "gpu_free_gib", lambda: 20.0)
    assert svc.gpu_verdict(10, "auto")[0] == "cpu"                          # очередь маленькая
    assert svc.gpu_verdict(10 ** 5, "auto")[0] == "cuda"
    assert svc.gpu_verdict(10, "cuda")[0] == "cuda"
    assert svc.gpu_verdict(10 ** 5, "cpu")[0] == "cpu"

    class Busy:
        users, label = 1, "Qwen 3.8 27B"

        def unit_active(self):
            return True

    assert svc.gpu_verdict(10 ** 5, "auto", [Busy()]) == ("cpu", "на GPU Qwen 3.8 27B")
    monkeypatch.setattr(G, "gpu_free_gib", lambda: 3.0)
    assert svc.gpu_verdict(10 ** 5, "cuda")[0] == "cpu"
    monkeypatch.setattr(G, "h3_busy", lambda: "H3: этап «denoise»")
    monkeypatch.setattr(G, "gpu_free_gib", lambda: 20.0)
    assert svc.gpu_verdict(10 ** 5, "cuda") == ("cpu", "H3: этап «denoise»")


def test_embedder_protocol(env, tmp_path):
    """Процесс-эмбеддер: строка «ready», пачки JSON, ошибки по индексам; без torch — заглушка вместо модели."""
    cfg, _ = env
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import sys, json, base64, array\n"
        "print('шум библиотек в stdout', file=sys.stderr)\n"
        "print(json.dumps({'ready': True, 'load_sec': 0}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    items = json.loads(line)['items']\n"
        "    vecs = [base64.b64encode(array.array('f', [1.0] * 4).tobytes()).decode() if 'text' in it else None"
        " for it in items]\n"
        "    errs = {str(i): 'нет' for i, it in enumerate(items) if 'text' not in it}\n"
        "    print(json.dumps({'vecs': vecs, 'errors': errs}), flush=True)\n")
    cfg.embed_python = sys.executable

    async def run():
        e = SR.Embedder(cfg, "cpu", "text", worker=str(stub))
        vecs, errors = await e.embed([{"text": "привет", "role": "query"}, {"image": "/нет.jpg"}])
        assert len(vecs[0]) == 16 and vecs[1] is None and errors == {1: "нет"}
        assert e.alive
        await e.close()
        assert not e.alive

    asyncio.run(run())


def test_file_lock(env):
    cfg, _ = env
    a, b = SR.FileLock(cfg.data_dir / "search.lock"), SR.FileLock(cfg.data_dir / "search.lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_fts_query(env):
    cfg, cache = env
    svc = service(cfg, cache)
    q = SR.parse_query('кто писал про "новый год" и поездку в Питер', cache, datetime.now(cfg.tz))
    fq = svc.searcher.fts_query(q)
    assert '"нов год"' in fq and '"поездк"*' in fq and '"питер"*' in fq
    assert "кто" not in fq and '"и"' not in fq
    # «фото кота» — ищут картинку: слово «фото» не ищется в тексте, вектор картинок весит больше
    q = SR.parse_query("фото кота", cache, datetime.now(cfg.tz))
    assert svc.searcher.wants_picture(q) and svc.searcher.fts_query(q) == '"кот"'
    assert not svc.searcher.wants_picture(SR.parse_query("фото", cache, datetime.now(cfg.tz)))


def test_text_batch_failure_is_retried(env):
    """Пачка текстов упала целиком (нехватка памяти) — ничего не помечается, очередь та же."""
    cfg, cache = env
    svc = service(cfg, cache)
    svc.indexer.sync()

    class Broken(FakeEmbedder):
        async def embed(self, items):
            return [None] * len(items), {i: "CUDA out of memory" for i in range(len(items))}

    with pytest.raises(SR.EmbedError):
        asyncio.run(svc.indexer.embed(Broken()))
    assert svc.indexer.pending() == (6, 3)
