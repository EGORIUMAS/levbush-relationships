"""Смысловой поиск по истории группы (/find): векторы + полнотекстовый индекс, всё локально.

- База — отдельный файл `search.db` рядом с cache.db (SQLite): переписка не уходит из машины, индекс можно
  удалить и пересобрать, не трогая кэш. Векторы — в таблицах sqlite-vec (vec0, косинус, перебор в C): 150 тыс.
  векторов по 1024 — ~0,1 с на запрос, без отдельного сервера и без копии индекса в памяти бота; удаления и правки —
  в той же транзакции, что и полнотекстовый индекс (FTS5 по основам слов Snowball: «поездку» найдёт «поездка»).
- Модель — jina-embeddings-v5-omni-small-retrieval: текстовая башня — jina-embeddings-v5-text-small (Qwen3-0.6B,
  MMTEB 67,7, русский понимает), картинки — в том же пространстве, поэтому текстовый запрос находит и фото.
  Считает отдельный процесс системного python3 (embed_worker.py): torch не грузится в бота, память освобождается.
- Документ сообщения — текст + опрос/ссылка/имя файла + расшифровка Parakeet + описание Nemotron + текст файла.
  Короткое («да», «ахах») — только в полнотекстовом индексе: вектор у него шумит. Фото — отдельным вектором.
- Правки и удаления: у каждого сообщения «штамп» (правка, состояние медиа, расшифровка, описание, длина текста);
  изменился — документ пересобирается, вектор пересчитывается; удалённое убирается из всех индексов сразу.
- Ранжирование — RRF по трём спискам (вектор текста, вектор картинки, FTS), альбом — одной строкой.
"""
import asyncio
import base64
import fcntl
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .cache import Cache
from .config import ROOT, Config
from .render import MEDIA_RU, Renderer, file_text, msg_link

log = logging.getLogger("levbush.search")

MIN_VEC_LETTERS = 12        # короче (без расшифровки/описания) — только полнотекстовый поиск
BODY_LIMIT = 4000           # знаков документа в индексе; эмбеддер ещё и обрезает по токенам
RRF_K = 60
# косинус запроса с документом, ниже которого вектор в выдачу не берётся: у текста хорошие совпадения 0,4–0,65, у фото
# (другая модальность) — 0,25–0,3 при 0,15–0,2 у случайных (замер на истории группы)
MIN_SIM = {"text": 0.25, "image": 0.22, "frame": 0.19}   # кадр видео: чужие ~0,13–0,15, свой ~0,2–0,35
WEIGHTS = {"text": 1.0, "fts": 1.0, "image": 0.8,
           # то же среди сообщений человека, названного в запросе по имени («Вика ест арбуз»): сильный сигнал —
           # его кружок с арбузом выше чужих сообщений со словом «арбуз»
           "text_p": 1.5, "fts_p": 1.5, "image_p": 1.5}
ICONS = {"photo": "📷", "video": "🎬", "video_note": "⭕", "voice": "🎤", "audio": "🎵", "gif": "🎞",
         "document": "📄", "sticker": "🌀", "poll": "📊", "webpage": "🔗", "location": "📍"}
# служебные слова — в полнотекстовый запрос не идут (вектору они не мешают)
STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от
меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там
потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под
будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда
зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая много
разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между
где-то кто-то что-то какие какой-то говорил говорили писал писали сказал обсуждали the a an of to in and or is""".split())


# «фото еды», «скрин переписки»: ищут картинку — эти слова не ищем в тексте, а поднимаем вектор картинок
PICTURE_STEMS = {"фот", "фотк", "фотограф", "картинк", "скрин", "скриншот", "пикч", "изображен", "снимк", "снимок"}


def _sig(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# ================================================================ основы слов

class Stemmer:
    """Основы слов для FTS5 (у SQLite нет русского стеммера): ё → е, русские — Snowball russian, латиница — english."""

    def __init__(self):
        import snowballstemmer
        self.ru = snowballstemmer.stemmer("russian")
        self.en = snowballstemmer.stemmer("english")
        self._cache: dict[str, str] = {}

    def words(self, text: str) -> list[str]:
        return re.findall(r"\w+", (text or "").lower().replace("ё", "е"))

    def stem(self, w: str) -> str:
        s = self._cache.get(w)
        if s is None:
            if re.search(r"[а-я]", w):
                s = self.ru.stemWord(w)
            elif re.fullmatch(r"[a-z]+", w):
                s = self.en.stemWord(w)
            else:
                s = w
            if len(self._cache) < 500_000:
                self._cache[w] = s
        return s

    def text(self, text: str) -> str:
        return " ".join(self.stem(w) for w in self.words(text))


# ================================================================ документ сообщения

def letters(text: str) -> int:
    return sum(ch.isalpha() for ch in text or "")


def document(m, transcript: str | None, desc: str | None) -> str:
    """Что сообщение «говорит»: текст, подпись вложения, опрос, ссылка, файл, расшифровка речи, описание медиа."""
    parts = [m["text"]] if m["text"] else []
    kind = m["media"]
    if kind:
        meta = json.loads(m["media_meta"]) if m["media_meta"] else {}
        extra = []
        if kind == "poll":
            extra.append(f"опрос: {meta.get('question') or ''} — " + " / ".join(meta.get("options") or []))
        if kind == "webpage":
            extra += [x for x in (meta.get("site"), meta.get("title"), (meta.get("description") or "")[:300]) if x]
        if kind == "document" and meta.get("file_name"):
            extra.append(f"файл «{meta['file_name']}»")
        if kind == "audio" and (meta.get("title") or meta.get("performer")):
            extra.append(f"{meta.get('performer') or ''} — {meta.get('title') or ''}")
        if kind == "location" and meta.get("title"):
            extra.append(f"{meta['title']}, {meta.get('address') or ''}")
        if transcript:
            extra.append(f"расшифровка: {transcript}")
        if desc:
            extra.append(f"описание: {desc}")
        if kind == "document" and m["media_state"] == "ok" and m["media_path"]:
            body = file_text(m["media_path"], meta, 2000)
            if body:
                extra.append(f"текст файла: {body}")
        if extra:
            parts.append(f"[{MEDIA_RU.get(kind, kind)}] " + "; ".join(extra))
    return "\n".join(parts).strip()[:BODY_LIMIT]


VIDEO_KINDS = ("video", "video_note", "gif")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def is_video(path: str | None) -> bool:
    return bool(path) and path.lower().endswith(VIDEO_EXT)


def image_of(m) -> str | None:
    """Путь к картинке сообщения для векторного индекса: фото, картинки-файлы, а также видео, кружки и GIF — их
    воркер режет на кадры (вектор на кадр, сходство — по лучшему кадру: арбуз в первые секунды кружка иначе
    размывается). Стикеры — нет, их тысячи одинаковых."""
    if m["media_state"] != "ok" or not m["media_path"]:
        return None
    meta = json.loads(m["media_meta"]) if m["media_meta"] else {}
    mime = meta.get("mime") or ""
    picture = m["media"] == "photo" or m["media"] == "document" and mime.startswith("image/") and not mime.endswith("gif")
    video = (m["media"] in VIDEO_KINDS or m["media"] == "document" and mime.startswith("video/")) \
        and is_video(m["media_path"])
    return m["media_path"] if (picture or video) and Path(m["media_path"]).exists() else None


# ================================================================ база индекса

SCHEMA = """
create table if not exists meta (key text primary key, value text);
create table if not exists items (
    msg_id      integer primary key,
    stamp       text not null,      -- правка|медиа|расшифровка|описание|длина текста — изменилось → пересобрать
    body        text,               -- документ (оригинальный текст — для фрагмента в выдаче)
    sig         text,               -- хэш документа, если ему положен вектор; null — только FTS
    vec_sig     text,               -- хэш документа, по которому посчитан вектор
    image       text,               -- путь к картинке
    image_state text,               -- todo / ok / error
    date        integer not null,
    sender      integer,            -- с учётом псевдонимов (аноним-админ → человек)
    grouped_id  integer
);
create index if not exists items_date on items(date);
create index if not exists items_sender on items(sender, date);
create index if not exists items_image on items(image_state);
create virtual table if not exists fts using fts5(body, content='', contentless_delete=1,
                                                  tokenize='unicode61 remove_diacritics 2');
"""


class SearchDB:
    """search.db: соединение на поток (как Cache), sqlite-vec подгружается в каждое."""

    def __init__(self, path: Path, dim: int):
        self.path, self.dim = path, dim
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        db = self.db
        db.executescript(SCHEMA)
        old = self.meta("dim")
        if old and int(old) != dim:                 # сменили размерность (Matryoshka) — векторы заново
            log.warning("размерность векторов %s → %s: векторный индекс пересоздаётся", old, dim)
            db.execute("drop table if exists vec_text")
            db.execute("drop table if exists vec_image")
            db.execute("drop table if exists vec_frame")
            db.execute("update items set vec_sig = null, image_state = case when image is null then null else 'todo' end")
        for t in ("vec_text", "vec_image"):
            db.execute(f"create virtual table if not exists {t} using vec0(msg_id integer primary key, "
                       f"emb float[{dim}] distance_metric=cosine, sender integer, date integer)")
        # кадры видео: frame_id = msg_id * MAX_FRAMES + номер кадра
        db.execute(f"create virtual table if not exists vec_frame using vec0(frame_id integer primary key, "
                   f"emb float[{dim}] distance_metric=cosine, msg_id integer, sender integer, date integer)")
        self.set_meta("dim", dim)

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            import sqlite_vec
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("pragma journal_mode=wal")
            conn.execute("pragma synchronous=normal")
            self._local.conn = conn
        return conn

    def tx(self):
        return _Tx(self.db)

    def meta(self, key, default=None):
        row = self.db.execute("select value from meta where key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("insert into meta(key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
                        (key, None if value is None else str(value)))

    def counts(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "items": q("select count(*) from items"),
            "fts": q("select count(*) from items where body <> ''"),
            "text_vecs": q("select count(*) from items where sig is not null and vec_sig = sig"),
            "text_todo": q("select count(*) from items where sig is not null and vec_sig is not sig"),
            "images": q("select count(*) from items where image_state = 'ok'"),
            "frames": q("select count(*) from vec_frame"),
            "images_todo": q("select count(*) from items where image_state = 'todo'"),
            "images_error": q("select count(*) from items where image_state = 'error'"),
        }


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("begin immediate")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        self.db.execute("rollback" if exc_type else "commit")


def _unpack(blob: str | bytes) -> bytes:
    return base64.b64decode(blob) if isinstance(blob, str) else blob


# ================================================================ эмбеддер (отдельный процесс)

class EmbedError(RuntimeError):
    pass


class Embedder:
    """Процесс embed_worker.py: модель грузится один раз, пачки — строками JSON. Для запросов бот держит свой
    (CPU, только текст, гасится по простою), индексация запускает отдельный — на GPU или CPU."""

    def __init__(self, cfg: Config, device: str = "cpu", modality: str = "text", worker: str | None = None):
        self.cfg, self.device, self.modality = cfg, device, modality
        self.worker = worker or str(ROOT / "levbush" / "embed_worker.py")
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.last_used = 0.0
        self.load_sec = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        if self.alive:
            return
        cfg = self.cfg
        cmd = [cfg.embed_python, self.worker, "--model", cfg.embed_model, "--device", self.device,
               "--modality", self.modality, "--dim", str(cfg.embed_dim), "--threads", str(cfg.embed_threads),
               "--max-pixels", str(cfg.embed_max_pixels)]
        self.proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, limit=1 << 26)
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=600)
        if not line:
            await self.close()
            raise EmbedError("эмбеддер не запустился (см. журнал бота)")
        ready = json.loads(line)
        self.load_sec = ready.get("load_sec")
        log.info("эмбеддер (%s, %s) запущен за %s с", self.device, self.modality, self.load_sec)

    async def embed(self, items: list[dict]) -> tuple[list[bytes | None], dict]:
        """items: {"text", "role"} или {"image"} → (векторы float32 или None, ошибки по индексам)."""
        async with self.lock:
            await self.start()
            self.proc.stdin.write((json.dumps({"items": items}, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
            line = await self.proc.stdout.readline()
            self.last_used = time.monotonic()
            if not line:
                await self.close()
                raise EmbedError("эмбеддер упал (см. журнал бота)")
            out = json.loads(line)
            return [_unpack(v) if v else None for v in out["vecs"]], {int(k): v for k, v in out["errors"].items()}

    async def close(self):
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=20)
            except (asyncio.TimeoutError, OSError):
                self.proc.kill()
                await self.proc.wait()
        self.proc = None


# ================================================================ индексация

MAX_FRAMES = 16          # кадров на видео в vec_frame (берём VIDEO_FRAMES)


def _drop_frames(db, msg_id: int):
    for j in range(MAX_FRAMES):         # vec0 удаляет только по первичному ключу
        db.execute("delete from vec_frame where frame_id = ?", (msg_id * MAX_FRAMES + j,))


class Indexer:
    def __init__(self, cfg: Config, cache: Cache, sdb: SearchDB | None = None):
        self.cfg, self.cache = cfg, cache
        self.sdb = sdb or SearchDB(cfg.search_db, cfg.embed_dim)
        self.stem = Stemmer()
        self.r = Renderer(cache, cfg)
        self.state: dict = {}          # прогресс текущей индексации (для /index и /status)
        self.embedder: Embedder | None = None      # эмбеддер текущей индексации (мог смениться GPU → CPU)
        self._sync_lock = threading.Lock()

    def _stamps(self) -> dict[int, tuple]:
        """id → (штамп, удалено) по всем сообщениям кэша, кроме служебных."""
        rows = self.cache.db.execute(
            """select m.id, m.deleted, coalesce(m.edit_date, '') || '|' || coalesce(m.media_state, '') || '|' ||
                      coalesce(t.created, '') || '|' || coalesce(d.created, '') || '|' || length(coalesce(m.text, ''))
               from messages m left join transcripts t on t.msg_id = m.id left join media_desc d on d.msg_id = m.id
               where m.service is null""").fetchall()
        return {r[0]: (r[2], r[1]) for r in rows}

    def sync(self, chunk: int = 2000) -> dict:
        """Полнотекстовый индекс и учёт: новые, изменённые, удалённые сообщения. Без модели, быстро (вся история
        ~6 с, дальше ~0,3 с); векторы для изменённого ставятся в очередь (embed)."""
        with self._sync_lock:
            return self._sync(chunk)

    def _sync(self, chunk: int) -> dict:
        self.r = Renderer(self.cache, self.cfg)          # псевдонимы и чат могли смениться
        if self.sdb.meta("videos") != "1":
            # видео, кружки и GIF стали индексироваться кадрами — пересобрать их записи (штамп сброшен)
            ids = [r[0] for r in self.cache.db.execute(
                f"select id from messages where media in ({','.join('?' * len(VIDEO_KINDS))}) or "
                "(media = 'document' and media_meta like '%\"video/%')", VIDEO_KINDS)]
            with self.sdb.tx() as db:
                for k in range(0, len(ids), 900):
                    part = ids[k:k + 900]
                    db.execute(f"update items set stamp = '' where msg_id in ({','.join('?' * len(part))})", part)
            self.sdb.set_meta("videos", "1")
        stamps = self._stamps()
        have = {r[0]: r[1] for r in self.sdb.db.execute("select msg_id, stamp from items")}
        gone = [i for i in have if i not in stamps or stamps[i][1]]
        todo = [i for i, (st, deleted) in stamps.items() if not deleted and have.get(i) != st]
        for k in range(0, len(gone), chunk):
            with self.sdb.tx() as db:
                for i in gone[k:k + chunk]:
                    self._drop(db, i)
        for k in range(0, len(todo), chunk):
            ids = todo[k:k + chunk]
            marks = ",".join("?" * len(ids))
            msgs = self.cache.db.execute(f"select * from messages where id in ({marks})", ids).fetchall()
            tr = dict(self.cache.db.execute(f"select msg_id, text from transcripts where msg_id in ({marks})", ids).fetchall())
            ds = dict(self.cache.db.execute(f"select msg_id, text from media_desc where msg_id in ({marks})", ids).fetchall())
            with self.sdb.tx() as db:
                for m in msgs:
                    self._put(db, m, stamps[m["id"]][0], tr.get(m["id"]) or None, ds.get(m["id"]) or None)
        if todo or gone:
            log.info("поиск: обновлено %d, удалено %d", len(todo), len(gone))
        self.sdb.set_meta("synced", int(time.time()))
        return {"updated": len(todo), "removed": len(gone)}

    @staticmethod
    def _drop(db, msg_id: int):
        db.execute("delete from fts where rowid = ?", (msg_id,))
        db.execute("delete from vec_text where msg_id = ?", (msg_id,))
        db.execute("delete from vec_image where msg_id = ?", (msg_id,))
        _drop_frames(db, msg_id)
        db.execute("delete from items where msg_id = ?", (msg_id,))

    def _put(self, db, m, stamp: str, transcript, desc):
        body = document(m, transcript, desc)
        old = db.execute("select body, sig, vec_sig, image, image_state from items where msg_id = ?",
                         (m["id"],)).fetchone()
        sender = self.r.sender(m)
        if old is None or old["body"] != body:
            db.execute("delete from fts where rowid = ?", (m["id"],))
            if body:
                db.execute("insert into fts(rowid, body) values (?, ?)", (m["id"], self.stem.text(body)))
        rich = bool(transcript or desc) or (m["media"] == "document" and "текст файла:" in body)
        sig = _sig(body) if body and (letters(body) >= MIN_VEC_LETTERS or rich) else None
        if old is not None and old["vec_sig"] and sig is None:
            db.execute("delete from vec_text where msg_id = ?", (m["id"],))      # текст стёрли/сократили
        image = image_of(m)
        if image is None:
            db.execute("delete from vec_image where msg_id = ?", (m["id"],))
            _drop_frames(db, m["id"])
            state = None
        elif old is not None and old["image"] == image and old["image_state"] in ("ok", "error"):
            state = old["image_state"]
        else:
            state = "todo"
        db.execute("""insert into items(msg_id, stamp, body, sig, vec_sig, image, image_state, date, sender, grouped_id)
                      values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                      on conflict(msg_id) do update set stamp = excluded.stamp, body = excluded.body,
                      sig = excluded.sig, vec_sig = case when excluded.sig is null then null else vec_sig end,
                      image = excluded.image, image_state = excluded.image_state,
                      date = excluded.date, sender = excluded.sender, grouped_id = excluded.grouped_id""",
                   (m["id"], stamp, body, sig, old["vec_sig"] if old else None, image, state, m["date"], sender,
                    m["grouped_id"]))

    # ------------------------------------------------------------ векторы

    def pending(self) -> tuple[int, int]:
        c = self.sdb.counts()
        return c["text_todo"], c["images_todo"]

    def _put_vecs(self, table: str, rows: list[tuple]):
        """rows: (msg_id, sig или None для картинок, вектор или None при ошибке)."""
        with self.sdb.tx() as db:
            for msg_id, sig, vec in rows:
                it = db.execute("select sig, image, sender, date from items where msg_id = ?", (msg_id,)).fetchone()
                if it is None:
                    continue                                   # удалили, пока считали
                if table == "vec_text":
                    if it["sig"] != sig:
                        continue                               # поправили, пока считали — пересчитается
                    db.execute("delete from vec_text where msg_id = ?", (msg_id,))
                    if vec is not None:
                        db.execute("insert into vec_text(msg_id, emb, sender, date) values (?, ?, ?, ?)",
                                   (msg_id, vec, it["sender"], it["date"]))
                        db.execute("update items set vec_sig = ? where msg_id = ?", (sig, msg_id))
                else:
                    if it["image"] != sig:
                        continue
                    db.execute("delete from vec_image where msg_id = ?", (msg_id,))
                    _drop_frames(db, msg_id)
                    if vec is not None and is_video(sig):
                        size = self.sdb.dim * 4                 # воркер отдал кадры подряд
                        for j in range(min(MAX_FRAMES, len(vec) // size)):
                            db.execute("insert into vec_frame(frame_id, emb, msg_id, sender, date) "
                                       "values (?, ?, ?, ?, ?)", (msg_id * MAX_FRAMES + j, vec[j * size:(j + 1) * size],
                                                                  msg_id, it["sender"], it["date"]))
                    elif vec is not None:
                        db.execute("insert into vec_image(msg_id, emb, sender, date) values (?, ?, ?, ?)",
                                   (msg_id, vec, it["sender"], it["date"]))
                    db.execute("update items set image_state = ? where msg_id = ?",
                               ("ok" if vec is not None else "error", msg_id))

    async def embed(self, embedder: Embedder, images: bool = True, text_batch: int = 256, image_batch: int = 8,
                    progress=None, should_switch=None) -> dict:
        """Векторы для очереди: сначала тексты (новые — первыми), потом картинки. should_switch() → новый
        Embedder (например, GPU понадобился модели бота — дальше на CPU) или None."""
        done = {"text": 0, "images": 0, "errors": 0}
        t0 = time.time()
        total_text, total_img = self.pending()
        total_img = total_img if images and embedder.modality == "vision" else 0
        self.state = {"stage": "тексты", "done": 0, "total": total_text + total_img, "device": embedder.device,
                      "started": int(t0)}

        self.embedder = embedder

        async def maybe_switch():
            nonlocal embedder
            if should_switch:
                new = await should_switch(embedder)
                if new is not None:
                    await embedder.close()
                    embedder = self.embedder = new
                    self.state["device"] = embedder.device

        while True:
            await maybe_switch()
            rows = await asyncio.to_thread(lambda: self.sdb.db.execute(
                "select msg_id, body, sig from items where sig is not null and vec_sig is not sig "
                "order by msg_id desc limit ?", (text_batch,)).fetchall())
            if not rows:
                break
            vecs, errors = await embedder.embed([{"text": r["body"], "role": "document"} for r in rows])
            if errors:           # тексты падают только пачкой целиком (нехватка памяти и т.п.) — повторим в другой раз
                raise EmbedError(f"пачка текстов: {next(iter(errors.values()))}")
            await asyncio.to_thread(self._put_vecs, "vec_text", [(r["msg_id"], r["sig"], v) for r, v in zip(rows, vecs)])
            done["text"] += len(rows)
            await self._tick(done, progress)
        if images and embedder.modality == "vision":
            self.state["stage"] = "картинки"
            while True:
                await maybe_switch()
                rows = await asyncio.to_thread(lambda: self.sdb.db.execute(
                    "select msg_id, image from items where image_state = 'todo' order by msg_id desc limit ?",
                    (image_batch,)).fetchall())
                if not rows:
                    break
                vecs, errors = await embedder.embed([{"image": r["image"]} for r in rows])
                for i, e in errors.items():
                    log.warning("картинка #%s: %s", rows[i]["msg_id"], e)
                await asyncio.to_thread(self._put_vecs, "vec_image", [(r["msg_id"], r["image"], v)
                                                                     for r, v in zip(rows, vecs)])
                done["images"] += len(rows)
                done["errors"] += len(errors)
                await self._tick(done, progress)
        done["seconds"] = round(time.time() - t0, 1)
        self.state = {}
        return done

    async def _tick(self, done: dict, progress):
        self.state["done"] = done["text"] + done["images"]
        el = time.time() - self.state["started"]
        left = self.state["total"] - self.state["done"]
        self.state["eta"] = int(el / max(1, self.state["done"]) * left) if left > 0 else 0
        if progress:
            await progress(self.state)


class FileLock:
    """Одна индексация векторов за раз (бот и `levbush index` из консоли)."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def acquire(self) -> bool:
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


# ================================================================ запрос

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
          "декабрь"]


@dataclass
class Query:
    text: str = ""
    sender: int | None = None
    sender_label: str = ""
    since: int | None = None
    until: int | None = None
    period_label: str = ""
    phrases: list[str] = field(default_factory=list)
    error: str = ""
    # названные в запросе по имени (из «Как называют» в досье) — не фильтр, а подъём их сообщений
    people: list[int] = field(default_factory=list)
    name_words: set[str] = field(default_factory=set)


def _period(s: str, now: datetime) -> tuple[datetime, datetime, str] | None:
    """Дата или период → [начало, конец) в часовом поясе now: 12.05.2026, 12.05, 2026-05-12, 05.2026, 2026-05,
    2026, сегодня, вчера, неделя, месяц, год."""
    s = s.strip().lower().rstrip(".")
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("сегодня", "today"):
        return day0, day0 + timedelta(days=1), "сегодня"
    if s in ("вчера", "yesterday"):
        return day0 - timedelta(days=1), day0, "вчера"
    if s in ("неделю", "неделя", "week"):
        return now - timedelta(days=7), now + timedelta(minutes=1), "за неделю"
    if s in ("месяц", "month"):
        return now - timedelta(days=30), now + timedelta(minutes=1), "за месяц"
    if s in ("год", "year"):
        return now - timedelta(days=365), now + timedelta(minutes=1), "за год"
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = (2000 + int(y) if len(y) == 2 else int(y)) if y else now.year
        try:
            start = day0.replace(year=year, month=mo, day=d)
        except ValueError:
            return None
        if not y and start > now:
            start = start.replace(year=year - 1)
        return start, start + timedelta(days=1), f"{start:%d.%m.%Y}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            start = day0.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)))
        except ValueError:
            return None
        return start, start + timedelta(days=1), f"{start:%d.%m.%Y}"
    m = re.fullmatch(r"(\d{1,2})\.(\d{4})|(\d{4})-(\d{1,2})", s)
    if m:
        mo, year = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
        if not 1 <= mo <= 12:
            return None
        start = day0.replace(year=year, month=mo, day=1)
        end = start.replace(year=year + (mo == 12), month=mo % 12 + 1)
        return start, end, f"{MONTHS[mo - 1]} {year}"
    if re.fullmatch(r"\d{4}", s):
        start = day0.replace(year=int(s), month=1, day=1)
        return start, start.replace(year=int(s) + 1), f"{s} год"
    return None


def parse_query(raw: str, cache: Cache, now: datetime) -> Query:
    """Запрос и фильтры: @ник или от:имя — автор; с:/после:, по:/до:, за: — период; "фраза" — точное совпадение."""
    q = Query()
    words = []
    for tok in re.findall(r'"[^"]+"|«[^»]+»|\S+', raw or ""):
        low = tok.lower()
        if tok[0] in "\"«":
            q.phrases.append(tok[1:-1])
            words.append(tok[1:-1])
            continue
        key, _, val = low.partition(":")
        if tok.startswith("@") and len(tok) > 1 or key in ("от", "from", "автор") and val:
            name = tok[1:] if tok.startswith("@") else tok.split(":", 1)[1]
            uid = find_person(cache, name)
            if uid is None:
                q.error = f"не нашёл участника «{name}»"
                continue
            q.sender, q.sender_label = uid, name
            continue
        if key in ("с", "после", "since", "по", "до", "until", "за") and val:
            p = _period(tok.split(":", 1)[1], now)
            if p is None:
                q.error = f"не понял дату «{tok.split(':', 1)[1]}»"
                continue
            start, end, label = p
            if key in ("с", "после", "since"):
                q.since = int(start.timestamp())
                q.period_label = f"с {label}"
            elif key in ("по", "до", "until"):
                q.until = int(end.timestamp())
                q.period_label = (q.period_label + " " if q.period_label else "") + f"по {label}"
            else:
                q.since, q.until, q.period_label = int(start.timestamp()), int(end.timestamp()), label
            continue
        words.append(tok)
    q.text = " ".join(words).strip()
    return q


def find_person(cache: Cache, name: str) -> int | None:
    """Ник, id или начало имени/фамилии; из совпадений — тот, у кого больше сообщений."""
    name = name.strip().lstrip("@")
    if not name:
        return None
    if name.lstrip("-").isdigit():
        return int(name)
    u = cache.user_by_username(name)
    if u is not None:
        return u["id"]
    low = name.lower().replace("ё", "е")
    best, best_n = None, -1
    for u in cache.db.execute("select id, first_name, last_name from users").fetchall():
        full = " ".join(x for x in (u["first_name"], u["last_name"]) if x).lower().replace("ё", "е")
        if full.startswith(low) or any(p.startswith(low) for p in full.split()):
            n = cache.db.execute("select count(*) from messages where sender_id = ?", (u["id"],)).fetchone()[0]
            if n > best_n:
                best, best_n = u["id"], n
    return best


# ================================================================ поиск

@dataclass
class Hit:
    msg_id: int
    score: float
    via: set


class Searcher:
    def __init__(self, cfg: Config, cache: Cache, sdb: SearchDB, stem: Stemmer | None = None):
        self.cfg, self.cache, self.sdb = cfg, cache, sdb
        self.stem = stem or Stemmer()

    def fts_query(self, q: Query, skip: set[str] = frozenset()) -> str | None:
        """Основы слов через OR (bm25 поднимает совпавшие по нескольким словам), фразы — в кавычках;
        skip — слова, которые не искать (имя автора: в тексте его сообщений его нет)."""
        parts = []
        for ph in q.phrases:
            stems = [self.stem.stem(w) for w in self.stem.words(ph)]
            if stems:
                parts.append('"' + " ".join(stems) + '"')
        rest = q.text
        for ph in q.phrases:
            rest = rest.replace(ph, " ")
        for w in self.stem.words(rest):
            if w in STOP or len(w) < 2 or w in skip:
                continue
            s = self.stem.stem(w)
            if s in PICTURE_STEMS and self.wants_picture(q):
                continue
            parts.append(f'"{s}"*' if len(s) >= 4 else f'"{s}"')
        return " OR ".join(dict.fromkeys(parts)) or None

    def wants_picture(self, q: Query) -> bool:
        """В запросе «фото/картинка/скрин» и есть что-то ещё: ищут картинку, а не само слово «фото»."""
        stems = [self.stem.stem(w) for w in self.stem.words(q.text) if w not in STOP]
        return any(s in PICTURE_STEMS for s in stems) and any(s not in PICTURE_STEMS for s in stems)

    def _filters(self, q: Query, col_date: str, col_sender: str) -> tuple[str, list]:
        sql, args = "", []
        if q.sender is not None:
            sql += f" and {col_sender} = ?"
            args.append(q.sender)
        if q.since is not None:
            sql += f" and {col_date} >= ?"
            args.append(q.since)
        if q.until is not None:
            sql += f" and {col_date} < ?"
            args.append(q.until)
        return sql, args

    def _images(self, q: Query, qvec: bytes, k: int, sender: int | None = None) -> list[int]:
        """Фото и видео по сходству с запросом: у видео — лучший из кадров."""
        db = self.sdb.db
        flt, args = self._filters(q, "date", "sender")
        if sender is not None:
            flt, args = flt + " and sender = ?", [*args, sender]
        sims = [(1 - d, mid) for mid, d in db.execute(
            f"select msg_id, distance from vec_image where emb match ? and k = ?{flt} order by distance",
            (qvec, k, *args)).fetchall() if 1 - d >= MIN_SIM["image"]]
        best: dict[int, float] = {}
        for mid, d in db.execute(f"select msg_id, distance from vec_frame where emb match ? and k = ?{flt} "
                                 f"order by distance", (qvec, k * 4, *args)).fetchall():
            if 1 - d >= MIN_SIM["frame"] and mid not in best:
                best[mid] = 1 - d
        sims += [(v, mid) for mid, v in best.items()]
        return [mid for _, mid in sorted(sims, reverse=True)[:k]]

    def search(self, q: Query, qvec: bytes | None, k: int = 100, limit: int = 40) -> list[Hit]:
        """Гибрид: KNN по векторам текстов и картинок + FTS5, слияние RRF."""
        db = self.sdb.db
        lists: dict[str, list[int]] = {}
        if qvec is not None:
            flt, args = self._filters(q, "date", "sender")
            lists["text"] = [r[0] for r in db.execute(
                f"select msg_id, distance from vec_text where emb match ? and k = ?{flt} order by distance",
                (qvec, k, *args)).fetchall() if 1 - r[1] >= MIN_SIM["text"]]
            lists["image"] = self._images(q, qvec, k)
        fq = self.fts_query(q)
        if fq:
            flt, args = self._filters(q, "i.date", "i.sender")
            try:
                lists["fts"] = [r[0] for r in db.execute(
                    f"select f.rowid from fts f join items i on i.msg_id = f.rowid where fts match ?{flt} "
                    f"order by bm25(fts) limit ?", (fq, *args, k)).fetchall()]
            except sqlite3.OperationalError as exc:        # странный запрос — без полнотекстовой части
                log.warning("FTS «%s»: %s", fq, exc)
        elif qvec is None and (q.sender is not None or q.since is not None):
            # только фильтры: последние сообщения автора/периода
            flt, args = self._filters(q, "date", "sender")
            lists["fts"] = [r[0] for r in db.execute(
                f"select msg_id from items where body <> ''{flt} order by date desc limit ?", (*args, k)).fetchall()]
        # человек назван по имени — те же поиски среди его сообщений (имя в тексте не ищем)
        if q.sender is None:
            fq_p = self.fts_query(q, skip={w.lower() for w in q.name_words})
            for uid in q.people[:2]:
                if qvec is not None:
                    flt, args = self._filters(q, "date", "sender")
                    lists[f"text_p:{uid}"] = [r[0] for r in db.execute(
                        f"select msg_id, distance from vec_text where emb match ? and k = ?{flt} and sender = ? "
                        f"order by distance", (qvec, k, *args, uid)).fetchall() if 1 - r[1] >= MIN_SIM["text"]]
                    lists[f"image_p:{uid}"] = self._images(q, qvec, k, sender=uid)
                if fq_p:
                    flt, args = self._filters(q, "i.date", "i.sender")
                    try:
                        lists[f"fts_p:{uid}"] = [r[0] for r in db.execute(
                            f"select f.rowid from fts f join items i on i.msg_id = f.rowid where fts match ? "
                            f"and i.sender = ?{flt} order by bm25(fts) limit ?", (fq_p, uid, *args, k)).fetchall()]
                    except sqlite3.OperationalError as exc:
                        log.warning("FTS «%s»: %s", fq_p, exc)
        weights = dict(WEIGHTS, image=1.5, image_p=2.0) if self.wants_picture(q) else WEIGHTS
        scores: dict[int, Hit] = {}
        for name, ids in lists.items():
            kind = name.split(":")[0]
            for rank, mid in enumerate(ids):
                h = scores.setdefault(mid, Hit(mid, 0.0, set()))
                h.score += weights[kind] / (RRF_K + rank + 1)
                h.via.add(kind.removesuffix("_p"))
        hits = sorted(scores.values(), key=lambda h: -h.score)
        # альбом — одной строкой; удалённые (если индекс ещё не догнал) — мимо
        out, albums = [], set()
        for h in hits:
            it = db.execute("select grouped_id from items where msg_id = ?", (h.msg_id,)).fetchone()
            m = self.cache.db.execute("select deleted from messages where id = ?", (h.msg_id,)).fetchone()
            if it is None or m is None or m[0]:
                continue
            if it[0]:
                if it[0] in albums:
                    continue
                albums.add(it[0])
            out.append(h)
            if len(out) >= limit:
                break
        return out


# ================================================================ выдача

def snippet(body: str, stems: set[str], stem: Stemmer, width: int = 170) -> str:
    """HTML-фрагмент документа около первого совпадения, совпавшие слова — жирным."""
    body = re.sub(r"\s+", " ", body or "").strip()
    if not body:
        return ""
    words = list(re.finditer(r"\w+", body))
    hit_spans = [w.span() for w in words
                 if stems and any(stem.stem(w.group().lower().replace("ё", "е")).startswith(s) for s in stems)]
    start = 0
    if hit_spans and hit_spans[0][0] > width // 3:
        start = max(0, hit_spans[0][0] - width // 4)
        space = body.rfind(" ", 0, start)
        start = space + 1 if space > start - 30 else start
    end = min(len(body), start + width)
    if end < len(body):
        cut = body.rfind(" ", start, end)
        end = cut if cut > start + width // 2 else end
    out, pos = [], start
    for a, b in hit_spans:
        if a < start or b > end:
            continue
        out.append(html.escape(body[pos:a], quote=False))
        out.append("<b>" + html.escape(body[a:b], quote=False) + "</b>")
        pos = b
    out.append(html.escape(body[pos:end], quote=False))
    return ("…" if start > 0 else "") + "".join(out) + ("…" if end < len(body) else "")


def query_stems(q: Query, stem: Stemmer) -> set[str]:
    return {stem.stem(w) for w in stem.words(q.text) if w not in STOP and len(w) >= 2}


def format_page(hits: list[Hit], page: int, per_page: int, q: Query, r: Renderer, sdb: SearchDB, stem: Stemmer,
                esc, semantic: bool) -> str:
    """Страница выдачи (HTML): номер, ссылка «Имя · дата», значок вложения, фрагмент."""
    head = f"🔎 «{esc(q.text or '…')}»"
    if q.sender_label:
        head += f" · от {esc(r.short(q.sender))}"
    if q.period_label:
        head += f" · {esc(q.period_label)}"
    if q.people:
        head += " · 👤 " + ", ".join(esc(r.short(uid)) for uid in q.people)
    if not hits:
        return head + "\n\nНичего не нашёл." + ("" if semantic else "\n<i>Смысловой индекс ещё не готов — искал "
                                                                     "только по словам.</i>")
    stems = query_stems(q, stem)
    lines = [head, ""]
    first = page * per_page
    for n, h in enumerate(hits[first:first + per_page], first + 1):
        m = r.cache.message(h.msg_id)
        it = sdb.db.execute("select body from items where msg_id = ?", (h.msg_id,)).fetchone()
        if m is None:
            continue
        who = r.short(r.sender(m))
        dt = datetime.fromtimestamp(m["date"], r.cfg.tz)
        icon = ICONS.get(m["media"] or "", "")
        link = msg_link(r.chat, h.msg_id)
        lines.append(f"<b>{n}.</b> <a href=\"{link}\">{esc(who)} · {dt:%d.%m.%y %H:%M}</a>" + (f" {icon}" if icon else ""))
        frag = snippet(it["body"] if it else (m["text"] or ""), stems, stem)
        if not frag and "image" in h.via:
            frag = "<i>похожий кадр</i>" if (m["media"] or "") in VIDEO_KINDS else "<i>похожая картинка</i>"
        if frag:
            lines.append(frag)
        lines.append("")
    total = len(hits)
    lines.append(f"<i>{first + 1}–{min(total, first + per_page)} из {total}"
                 + ("" if semantic else " · только по словам: смысловой индекс ещё не готов") + "</i>")
    return "\n".join(lines).strip()


# ================================================================ для бота

class SearchService:
    """Всё, что нужно боту: индекс, эмбеддер запросов (текстовый, CPU, ~1,3 ГБ ОЗУ, гасится по простою),
    индексация — отдельным процессом на время прогона (с картинками — до 5,5 ГБ ОЗУ на CPU): загрузка модели из
    кэша страниц ~1 с, держать её между дозаливками раз в 5 мин дороже по памяти."""

    def __init__(self, cfg: Config, cache: Cache):
        self.cfg, self.cache = cfg, cache
        self.sdb = SearchDB(cfg.search_db, cfg.embed_dim)
        self.indexer = Indexer(cfg, cache, self.sdb)
        self.searcher = Searcher(cfg, cache, self.sdb, self.indexer.stem)
        self.qembed = Embedder(cfg, "cpu", "text")
        self.lock = FileLock(cfg.data_dir / "search.lock")
        self._idle_tasks: dict[int, asyncio.Task] = {}

    @property
    def semantic_possible(self) -> bool:
        return Path(self.cfg.embed_model).is_dir()

    @property
    def semantic(self) -> bool:
        """Векторный поиск имеет смысл: модель есть и хоть что-то проиндексировано."""
        if not self.semantic_possible:
            return False
        return bool(self.sdb.db.execute("select 1 from items where vec_sig is not null limit 1").fetchone())

    async def query_vec(self, text: str) -> bytes | None:
        if not text or not self.semantic:
            return None
        try:
            vecs, errors = await self.qembed.embed([{"text": text, "role": "query"}])
        except (EmbedError, OSError, asyncio.TimeoutError) as exc:
            log.warning("вектор запроса: %s", exc)
            return None
        self._touch(self.qembed)
        return vecs[0]

    def _touch(self, emb: Embedder):
        t = self._idle_tasks.get(id(emb))
        if t is None or t.done():
            self._idle_tasks[id(emb)] = asyncio.create_task(self._idle(emb))

    async def _idle(self, emb: Embedder):
        while emb.alive:
            await asyncio.sleep(30)
            if time.monotonic() - emb.last_used > self.cfg.embed_idle_min * 60 and not emb.lock.locked():
                await emb.close()
                log.info("эмбеддер (%s, %s) остановлен по простою", emb.device, emb.modality)

    async def find(self, raw: str, now: datetime, names=None) -> tuple[Query, list[Hit], bool]:
        """names(text) -> Counter{uid: n} — кто назван в тексте по имени из досье (Analyzer._name_hits)."""
        q = parse_query(raw, self.cache, now)
        if q.error:
            return q, [], False
        if names is not None and q.sender is None:
            q.people = [uid for uid, _ in names(q.text).most_common(2)]
            q.name_words = {w for w in re.findall(r"[^\W\d_]+", q.text) if names(w)}
        qvec = await self.query_vec(q.text)
        hits = await asyncio.to_thread(self.searcher.search, q, qvec)
        return q, hits, qvec is not None

    # ------------------------------------------------------------ индексация

    def gpu_verdict(self, work: int, pref: str | None = None, servers=()) -> tuple[str, str]:
        """Где считать: (cuda|cpu, почему). На GPU — только если там нет модели бота, не идёт генерация H3 и
        хватает свободной VRAM (общий vLLM-Qwen пользователя, если бодрствует, тоже её занимает)."""
        from .gpu import LLMManager, gpu_free_gib, h3_busy
        pref = (pref or self.cfg.embed_device).lower()
        if pref == "cpu":
            return "cpu", "выбран CPU"
        if pref == "auto" and work < self.cfg.embed_gpu_min:
            return "cpu", f"в очереди меньше {self.cfg.embed_gpu_min}"
        if (busy := h3_busy()):
            return "cpu", busy
        for m in servers:
            if m.users or m.unit_active():
                return "cpu", f"на GPU {m.label}"
        if LLMManager._start_lock is not None and LLMManager._start_lock.locked():
            return "cpu", "запускается модель бота"
        free = gpu_free_gib()
        if not free >= self.cfg.embed_need_gib:            # nan (нет nvidia-smi) — тоже CPU
            return "cpu", f"свободно {free:.1f} ГиБ VRAM, нужно {self.cfg.embed_need_gib:g}"
        return "cuda", f"GPU, свободно {free:.1f} ГиБ"

    def estimate(self, text: int, images: int, device: str) -> int:
        """Секунд на очередь. Замеры на CPU (Ryzen 9 9900X3D, bf16): 8 потоков — 25–33 текста/с, 20 потоков —
        ~50 текстов/с и 2,3 с на фото. GPU не мерили (занят) — оценка по размеру модели для RTX 5090, с запасом."""
        if device == "cuda":
            return int(30 + text / 1500 + images / 15)
        k = (8 / max(1, self.cfg.embed_threads)) ** 0.6
        return int(10 + text / 25 * k + images * 4 * k)

    async def _release_gpu(self):
        """Модели бота нужен GPU: индексация переезжает на CPU (между пачками, на GPU это секунды)."""
        e = self.indexer.embedder
        if e is None or e.device != "cuda" or not e.alive:
            return
        self._want_cpu = True
        for _ in range(120):
            e = self.indexer.embedder
            if e is None or e.device != "cuda" or not e.alive:
                return
            await asyncio.sleep(0.5)
        log.warning("индексация не освободила GPU за минуту")

    async def run_index(self, device: str | None = None, servers=(), progress=None, images: bool = True) -> dict:
        """Векторы для всего, что в очереди. Одна индексация за раз (файловый замок: бот и консоль)."""
        from .gpu import LLMManager, h3_busy
        if not Path(self.cfg.embed_model).is_dir():
            raise EmbedError(f"нет модели {self.cfg.embed_model}")
        if not self.lock.acquire():
            raise EmbedError("индексация уже идёт (в боте или в консоли)")
        self._want_cpu = False

        async def switch(cur: Embedder):
            if cur.device != "cuda":
                return None
            why = ("модели бота нужен GPU" if self._want_cpu else h3_busy()
                   or next((f"на GPU {m.label}" for m in servers if m.users or m.unit_active()), None))
            if not why:
                return None
            log.info("индексация: %s — дальше на CPU", why)
            self.indexer.state["note"] = f"переехала на CPU: {why}"
            return Embedder(self.cfg, "cpu", cur.modality)

        LLMManager.gpu_guests.append(self._release_gpu)
        try:
            await asyncio.to_thread(self.indexer.sync)
            text, img = self.indexer.pending()
            dev, why = self.gpu_verdict(text + (img if images else 0), device, servers)
            log.info("индексация: текстов %d, картинок %d — %s (%s)", text, img, dev, why)
            res = await self.indexer.embed(Embedder(self.cfg, dev, "vision" if images else "text"), images=images,
                                           progress=progress, should_switch=switch)
            res.update(device=dev, why=why)
            if not any(self.indexer.pending()):
                self.sdb.set_meta("backfilled", int(time.time()))
            return res
        finally:
            LLMManager.gpu_guests.remove(self._release_gpu)
            emb, self.indexer.embedder = self.indexer.embedder, None
            if emb is not None:
                await emb.close()
            self.indexer.state = {}
            self.lock.release()

    async def close(self):
        for t in self._idle_tasks.values():
            t.cancel()
        for emb in (self.qembed, self.indexer.embedder):
            if emb is not None:
                await emb.close()
