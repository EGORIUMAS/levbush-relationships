"""Разбор переписки Nemotron 3 Nano Omni: досье участников и карта связей.

Шаг разбора: Nemotron получает несколько последовательных окон переписки (бесед), перед ними — уже
разобранные окна за последние CONTEXT_HOURS часов как контекст, а также текущие досье участников и описания
связей между ними. Он сам анализирует окна и возвращает отредактированные досье и связи.

Первый прогон идёт такими шагами по всей истории. Дальше раз в день — то же самое: новые окна за сутки
и контекст за два последних дня. Медиа предварительно описывает Nemotron (по самому файлу; голос и кружки —
вместе с расшифровкой Parakeet), фотографии ещё и подаются в шаг напрямую.
"""
import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import llm as L
from .cache import Cache
from .config import ROOT, Config
from .dossier import (REL_NOTES, SECTIONS, apply_person, apply_relation, empty_dossier, empty_relation,
                      prompt_person, prompt_relation, render_person, render_relation)
from .episodes import Episode, is_closed, segment
from .gpu import LLMManager
from .media import Media
from .remote import DB, ts
from .render import Renderer

log = logging.getLogger("levbush.analyze")

RULES = """Правила:
- Опирайся только на то, что есть в переписке, вложениях и прошлых досье. Ничего не выдумывай.
- Факты подкрепляй номерами сообщений. Догадки помечай как догадки (certain=false).
- Не делай выводов о здоровье, ориентации, религии, политике, национальности и т. п., если человек сам прямо об этом не сказал.
- Пиши по-русски, коротко и по делу. Людей называй по имени, в JSON — по id.
- Голосовые и кружки: содержание бери из расшифровки (Parakeet), по звуку и видео оценивай тон и эмоции."""

def _arr(props: dict) -> dict:
    return {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": props,
                                       "required": list(props)}}


_INT, _STR, _BOOL = {"type": "integer"}, {"type": "string"}, {"type": "boolean"}
_MSGS = {"type": "array", "items": _INT}

# Ответ шага — пачка правок (как вызовы инструментов), а не переписанные досье
STEP_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "windows": _arr({"id": _INT, "summary": _STR, "topics": {"type": "array", "items": _STR}, "mood": _STR}),
        "add": _arr({"person": _INT, "section": {"type": "string", "enum": list(SECTIONS)}, "text": _STR,
                     "msgs": _MSGS, "certain": _BOOL}),
        "update": _arr({"person": _INT, "entry": _STR, "text": _STR, "msgs": _MSGS, "why": _STR}),
        "remove": _arr({"person": _INT, "entry": _STR, "msgs": _MSGS, "why": _STR}),
        "summaries": _arr({"person": _INT, "text": _STR}),
        "relations": _arr({"a": _INT, "b": _INT, "kind": _STR, "tone": _STR,
                           "closeness": {"type": "integer", "minimum": 0, "maximum": 10}, "summary": _STR}),
        "relation_events": _arr({"a": _INT, "b": _INT, "text": _STR, "msgs": _MSGS}),
        "relation_notes": _arr({"a": _INT, "b": _INT, "section": {"type": "string", "enum": list(REL_NOTES)},
                                "text": _STR, "msgs": _MSGS}),
    },
    "required": ["windows", "add", "update", "remove", "summaries", "relations", "relation_events",
                 "relation_notes"],
}

CHARS_PER_TOKEN = 2.8     # грубая оценка для русского текста

def _schema_hint(schema) -> str:
    return "Ответ — строго один JSON-объект по схеме:\n" + json.dumps(schema, ensure_ascii=False)


def _dt(ts_: int, cfg: Config) -> str:
    return datetime.fromtimestamp(ts_, cfg.tz).strftime("%Y-%m-%d")


class Analyzer:
    def __init__(self, cfg: Config, cache: Cache, db: DB, mgr: LLMManager, notify=None):
        self.cfg, self.cache, self.db, self.mgr = cfg, cache, db, mgr
        self.notify = notify
        self.r = Renderer(cache, cfg)
        self.chat = cache.get("chat", {}) or {}
        self.hidden = {x for x in (self.chat.get("id"), self.chat.get("channel_id")) if x}
        self.media = Media(cfg)
        self.state = {"state": "idle"}
        self.running = False
        self._llm: L.LLM | None = None
        self._text_cache: dict = {}
        self._tok_cache: dict = {}
        self.stat: dict = {}

    async def _progress(self, **kw):
        self.state.update(kw, updated=int(time.time()))
        try:
            await self.db.set_pass(self.state)
        except Exception:  # noqa: BLE001 — прогресс не критичен
            log.exception("set_pass")

    async def _say(self, text):
        log.info(text)
        if self.notify:
            try:
                await self.notify(text)
            except Exception:  # noqa: BLE001
                log.exception("notify")

    @property
    def llm(self) -> L.LLM:
        if self._llm is None or self._llm.model != self.mgr.model:
            self._llm = L.LLM(self.cfg.llm_url, self.mgr.model or "nemotron3-nano-omni")
        return self._llm

    def _is_person(self, uid) -> bool:
        if uid is None or uid in self.hidden:
            return False
        u = self.cache.user(uid)
        return not (u and u["is_bot"])

    # ================================================================ расшифровка

    async def transcribe_pending(self, ids: list[int] | None = None, report: bool = True) -> int:
        """Расшифровка Parakeet для речи без расшифровки (все или только ids)."""
        rows = self.cache.db.execute(
            """select m.id, m.media_path, m.media_meta from messages m left join transcripts t on t.msg_id = m.id
               where (m.media in ('voice', 'video_note', 'video', 'audio')
                      or (m.media = 'document' and (json_extract(m.media_meta, '$.mime') like 'audio/%'
                                                    or json_extract(m.media_meta, '$.mime') like 'video/%')))
               and m.media_state = 'ok' and t.msg_id is null and not m.deleted order by m.id""").fetchall()
        if ids is not None:
            wanted = set(ids)
            rows = [r for r in rows if r["id"] in wanted]
        jobs = []
        for r in rows:
            dur = (json.loads(r["media_meta"]) if r["media_meta"] else {}).get("duration") or 0
            if dur > 3 * 3600 or not r["media_path"] or not Path(r["media_path"]).exists():
                self.cache.set_transcript(r["id"], "", dur, "skip")
                continue
            jobs.append({"id": r["id"], "path": r["media_path"]})
        if not jobs:
            return 0
        if report:
            await self._progress(stage="расшифровка речи", done=0, total=len(jobs))
        proc = await asyncio.create_subprocess_exec(
            self.cfg.asr_python, str(ROOT / "levbush" / "asr_worker.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=1 << 24)
        proc.stdin.write("".join(json.dumps(j) + "\n" for j in jobs).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        done = 0
        async for line in proc.stdout:
            out = json.loads(line)
            if out.get("ready"):
                continue
            if "error" in out:
                self.cache.set_transcript(out["id"], "", None, "error")
            else:
                self.cache.set_transcript(out["id"], out["text"], out.get("seconds"), "parakeet-tdt-0.6b-v3")
            done += 1
            if report and done % 20 == 0:
                await self._progress(done=done)
        await proc.wait()
        return done

    # ================================================================ медиа

    # ================================================================ окна и шаги

    def load_messages(self):
        return self.cache.db.execute(
            "select * from messages where service is null and not deleted order by date, id").fetchall()

    def _persons(self, msgs) -> list[int]:
        seen = {}
        for m in msgs:
            s = self.r.sender(m)
            if not m["auto_fwd"] and self._is_person(s):
                seen.setdefault(s, None)
        return list(seen)

    def _text(self, e: Episode) -> str:
        key = (e.id, e.last_id)
        if key not in self._text_cache:
            self._text_cache[key] = "\n\n".join(self.r.line(m) for m in e.msgs)
        return self._text_cache[key]

    def _msg_tokens(self, m) -> int:
        return int(len(self.r.line(m)) / CHARS_PER_TOKEN) + (self.media.cost(m) if m["media"] else 0)

    def _tokens(self, e: Episode) -> int:
        """Оценка токенов окна: текст + медиа как есть."""
        key = (e.id, e.last_id)
        if key not in self._tok_cache:
            self._tok_cache[key] = sum(self._msg_tokens(m) for m in e.msgs)
        return self._tok_cache[key]

    def windows(self, msgs) -> list[Episode]:
        """Все окна истории: эпизоды, слишком длинные разрезаны по бюджету шага."""
        out = []
        limit = self.cfg.step_tokens
        for e in segment(msgs, self.cfg.gap_min, self.cfg.cast_window, self.r.alias):
            if self._tokens(e) <= limit:
                out.append(e)
                continue
            cur, size = [], 0
            for m in e.msgs:
                n = self._msg_tokens(m)
                if cur and size + n > limit * 0.8:
                    out.append(Episode(cur, self.r.alias))
                    cur, size = [], 0
                cur.append(m)
                size += n
            if cur:
                out.append(Episode(cur, self.r.alias))
        return out

    def _done(self) -> dict:
        return {r[0]: r[1] for r in self.cache.db.execute(
            "select episode_id, last_id from analyzed where status = 'done'")}

    def plan(self, wins: list[Episode], now: int) -> list[tuple[int, int]]:
        """Шаги: списки индексов новых окон [(from, to)), по несколько последовательных окон."""
        done = self._done()
        todo = [i for i, w in enumerate(wins) if done.get(w.id) != w.last_id and is_closed(w, now, self.cfg.gap_min)]
        steps = []
        k = 0
        while k < len(todo):
            first = todo[k]
            size, people, last = 0, set(), first
            while k < len(todo) and todo[k] == last and last - first < self.cfg.step_max_windows:
                w = wins[todo[k]]
                n = self._tokens(w)
                ppl = people | set(self._persons(w.msgs))
                if last > first and (size + n > self.cfg.step_tokens or len(ppl) > self.cfg.step_max_people):
                    break
                size, people = size + n, ppl
                k += 1
                last += 1
            steps.append((first, last))
        return steps

    def _context(self, wins: list[Episode], first: int) -> str:
        """Уже разобранные окна за CONTEXT_HOURS до шага (самые свежие, в пределах бюджета)."""
        start = wins[first].start
        budget = self.cfg.step_context_chars
        picked = []
        i = first - 1
        while i >= 0 and budget > 0 and start - wins[i].end <= self.cfg.context_hours * 3600:
            text = self._text(wins[i])
            if len(text) > budget:
                text = "… (начало окна опущено)\n\n" + text[-budget:]
            picked.append((wins[i], text))
            budget -= len(text)
            i -= 1
        return "\n\n".join(f"=== Окно #{w.id}, {self._span(w)} ===\n{t}" for w, t in reversed(picked))

    def _span(self, w: Episode) -> str:
        a = datetime.fromtimestamp(w.start, self.cfg.tz)
        b = datetime.fromtimestamp(w.end, self.cfg.tz)
        return f"{a:%Y-%m-%d %H:%M}–{b:%H:%M}" if a.date() == b.date() else f"{a:%Y-%m-%d %H:%M} – {b:%Y-%m-%d %H:%M}"

    def interleave(self, blocks, budget) -> list:
        """Окна переписки в запрос: текст сообщений, а вложения — прямо после своего сообщения, как есть."""
        parts, buf = [], []
        for header, msgs in blocks:
            buf.append(header)
            for m in msgs:
                buf.append(self.r.line(m))
                media = self.media.parts(m, budget) if m["media"] else []
                if media:
                    parts.append(L.text("\n\n".join(buf)))
                    buf = []
                    parts += media
        if buf:
            parts.append(L.text("\n\n".join(buf)))
        return parts

    def _interactions(self, msgs, people: set) -> set:
        """Пары, которые в этих окнах взаимодействовали: ответ, упоминание, реакция или оба активно писали."""
        pairs = set()
        by_user = {u["username"].lower(): uid for uid in people
                   if (u := self.cache.user(uid)) is not None and u["username"]}
        count = defaultdict(int)
        for m in msgs:
            s = self.r.sender(m)
            if s not in people:
                continue
            count[s] += 1
            targets = set()
            if m["reply_to"]:
                t = self.cache.message(m["reply_to"])
                if t is not None:
                    targets.add(self.r.sender(t))
            for e in json.loads(m["entities"]) if m["entities"] else []:
                if e.get("t") == "text_mention":
                    targets.add(e.get("u"))
                elif e.get("t") == "mention":
                    from .normalize import u16_slice
                    targets.add(by_user.get(u16_slice(m["text"], e["o"], e["l"]).lstrip("@").lower()))
            for (uid,) in self.cache.db.execute("select user_id from reactions where msg_id = ?", (m["id"],)):
                targets.add(uid)
            for t in targets:
                if t in people and t != s:
                    pairs.add((min(s, t), max(s, t)))
        active = [u for u, n in count.items() if n >= 2]
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                pairs.add((min(a, b), max(a, b)))
        return pairs

    async def _stats_text(self, uid: int) -> str:
        p = await self.db.call("api_person", uid)
        if not p:
            return ""
        from .stats import LABELS
        t = p["total"]
        c = t.get("c", {})
        keys = ("msgs", "replies", "quotes", "reactions", "reactions_recv", "voices", "video_notes", "media", "stickers")
        part = ", ".join(f"{LABELS[k]} {c.get(k, 0)}" for k in keys if c.get(k))
        extra = []
        if t.get("mean_hour") is not None:
            extra.append(f"обычно пишет около {t['mean_hour']:.0f} ч")
        if t.get("active_days"):
            extra.append(f"активных дней {t['active_days']}")
        return f"статистика за всё время: {part}" + ("; " + "; ".join(extra) if extra else "")

    async def _people_block(self, people: list[int], with_stats: bool, dossiers: dict) -> str:
        briefs = await self.db.people_brief(people)
        out = []
        for uid in people:
            u = self.cache.user(uid)
            b = briefs.get(uid, {})
            meta = [f"id {uid}"]
            if b.get("first_join"):
                meta.append(f"в группе с {b['first_join']:%Y-%m-%d}")
            if u and u["is_premium"]:
                meta.append("Premium")
            if u and u["bio"]:
                meta.append(f"био: «{u['bio']}»")
            if with_stats:
                st = await self._stats_text(uid)
                if st:
                    meta.append(st)
            out.append(f"### {self.r.name(uid)} — " + "; ".join(meta) + "\n" + prompt_person(dossiers.get(uid)))
        return "\n\n".join(out)

    def _relations_block(self, rows, active_pairs: set) -> str:
        out, size = [], 0
        for r in sorted(rows, key=lambda r: ((r["a"], r["b"]) not in active_pairs, -(r["strength"] or 0))):
            head = (f"### {self.r.name(r['a']).split(' (@')[0]} (id {r['a']}) ↔ "
                    f"{self.r.name(r['b']).split(' (@')[0]} (id {r['b']})")
            full = (r["a"], r["b"]) in active_pairs and size < self.cfg.step_relations_chars
            text = head + "\n" + (prompt_relation(r, r["data"]) if full else prompt_relation(r, None))
            size += len(text)
            out.append(text)
        return "\n\n".join(out)

    async def step(self, wins: list[Episode], first: int, last: int, now: int):
        new = wins[first:last]
        msgs = [m for w in new for m in w.msgs]
        people = sorted({p for w in new for p in self._persons(w.msgs)})
        if not people:
            for w in new:
                self._mark(w, "done")
            return
        as_of_ts = new[-1].end
        with_stats = now - as_of_ts < 3 * 86400
        active = self._interactions(msgs, set(people))
        context = self._context(wins, first)
        title = self.chat.get("title") or "группа"
        dossiers = {}
        for uid in people:
            row = await self.db.dossier(uid)
            dossiers[uid] = row["data"] if row and row["data"].get("entries") is not None else empty_dossier()
        rel_rows = [dict(r) for r in await self.db.pool.fetch(
            "select * from relations where a = any($1) and b = any($1) and (summary is not null or data <> '{}')",
            people)]
        head = "\n\n".join(x for x in [
            f"Ты ведёшь досье участников группового Telegram-чата «{title}» и карту их отношений. "
            "Ниже текущие досье и связи, затем новые окна переписки. Разбери окна и внеси в досье и связи правки.",
            RULES,
            "## Участники новых окон и их текущие досье (в квадратных скобках — id записей)\n\n"
            + await self._people_block(people, with_stats, dossiers),
            "## Текущие связи между ними\n\n" + (self._relations_block(rel_rows, active) or "Пока не описаны."),
            ("## Контекст: переписка перед новыми окнами (уже разобрана — только для понимания)\n\n" + context)
            if context else "",
            "## Новые окна переписки — разбери их (вложения идут прямо в переписке: фото, видео, кружки и "
            "голосовые — как есть; у речи есть расшифровка Parakeet)",
        ] if x)
        task = (
            "## Задача\n"
            "Верни только ПРАВКИ — то, что новые окна добавляют или меняют. Ничего не переписывай целиком; если "
            "нового нет — оставь списки пустыми. Даты ставятся автоматически по сообщениям из msgs, поэтому "
            "всегда указывай номера сообщений, на которых основана правка.\n"
            f"- windows — для каждого нового окна (id: {', '.join(str(w.id) for w in new)}): summary (о чём и чем "
            "кончилось, 2–4 предложения), topics, mood.\n"
            "- add — новая запись в досье человека (person — id). section: who (кто это), facts (факты о жизни: "
            "работа, учёба, город, семья, события), interests, character (черты, манера общения), role (роль в "
            "группе), timeline (заметное событие). Одна мысль — одна запись, коротко. certain=false для догадок.\n"
            "- update — запись изменилась или уточнилась: entry — её id (e12), text — новый текст, why — что "
            "произошло (например «переехал»). Старый текст и дата изменения сохранятся сами.\n"
            "- remove — запись устарела или оказалась неверной (entry, why). Она останется в досье зачёркнутой.\n"
            "- summaries — одна строка до 140 знаков «кто это в группе», если прежней нет или она устарела.\n"
            "- relations — для пар (a < b), чья связь проявилась в новых окнах и у которых что-то новое: kind "
            "(дружба, флирт, пара, соперничество, коллеги, перепалки, знакомые…), tone, closeness 0–10 (сила и "
            "близость; открытая вражда — тоже сильная связь), summary одной строкой.\n"
            "- relation_events — заметное событие между двумя людьми (помогли, поссорились, договорились, "
            "флиртовали, поздравили…), одной фразой.\n"
            "- relation_notes — заменить заметку о связи: section how (как общаются), bond (что их связывает), "
            "dynamics (к чему идёт) — 1–3 предложения.\n" + _schema_hint(STEP_SCHEMA))
        budget = self.media.budget(self.cfg.step_tokens)
        blocks = [(f"=== Окно #{w.id}, {self._span(w)} ===", w.msgs) for w in new]
        content = [L.text(head)] + self.interleave(blocks, budget) + [L.text(task)]
        chars = sum(len(p["text"]) for p in content if p["type"] == "text")
        media_tokens = self.cfg.step_tokens - budget.tokens
        est = int(chars / CHARS_PER_TOKEN) + media_tokens
        room = self.cfg.llm_ctx - est - 1000
        if room < 6000 and last - first > 1:
            raise _TooBig()
        max_tokens = max(4000, min(32000, room))
        try:
            out = await self.llm.chat([{"role": "user", "content": content}], STEP_SCHEMA, max_tokens=max_tokens,
                                      think=self.cfg.llm_think, audio_in_video=budget.audio_in_video)
        except L.LLMError as exc:
            if last - first > 1:
                raise _TooBig() from exc
            if media_tokens:
                log.warning("окно #%s с вложениями не прошло (%s) — повторяю без вложений", new[0].id, exc)
                text_only = head + "\n\n" + "\n\n".join(f"{h}\n{self._text(w)}" for (h, _), w in zip(blocks, new))
                out = await self.llm.chat([{"role": "user", "content": text_only + "\n\n" + task}], STEP_SCHEMA,
                                          max_tokens=max_tokens, think=self.cfg.llm_think)
            else:
                raise
        log.info("шаг #%s: оценка %d ток., медиа %d ток., не влезло вложений %d", new[0].id, est, media_tokens,
                 len(budget.dropped))
        await self._apply(out, new, set(people), datetime.fromtimestamp(as_of_ts, timezone.utc), dossiers,
                          {(r["a"], r["b"]): r for r in rel_rows})
        for w in new:
            self._mark(w, "done")

    def _day_of(self, fallback: datetime):
        """Дата правки — по самому раннему из указанных сообщений, иначе по концу шага."""
        def day(msgs):
            dates = [m["date"] for x in (msgs or [])[:8] if (m := self.cache.message(x)) is not None]
            when = datetime.fromtimestamp(min(dates), self.cfg.tz) if dates else fallback.astimezone(self.cfg.tz)
            return when.strftime("%Y-%m-%d")
        return day

    async def _apply(self, out: dict, new: list[Episode], people: set, as_of: datetime, dossiers: dict, rels: dict):
        by_id = {w.id: w for w in new}
        for item in out.get("windows", []):
            w = by_id.get(item.get("id"))
            if w is None:
                continue
            await self.db.save_episode(w.id, w.last_id, ts(w.start), ts(w.end), len(w.msgs), self._persons(w.msgs),
                                       item.get("summary", ""), item.get("topics", []), item.get("mood", ""),
                                       {"topics": item.get("topics", []), "mood": item.get("mood", "")})
        day = self._day_of(as_of)
        for uid in people:
            data = dossiers[uid]
            if apply_person(data, out, uid, day):
                await self.db.save_dossier(uid, as_of, data.get("summary") or None, render_person(data, self.chat), data)
                self.stat["dossiers"] += 1
        touched = {(min(o["a"], o["b"]), max(o["a"], o["b"]))
                   for key in ("relations", "relation_events", "relation_notes") for o in out.get(key, [])
                   if o.get("a") in people and o.get("b") in people and o.get("a") != o.get("b")}
        fields = {(min(o["a"], o["b"]), max(o["a"], o["b"])): o for o in out.get("relations", [])}
        for a, b in sorted(touched):
            row = rels.get((a, b)) or {}
            data = row["data"] if row.get("data") and "events" in row["data"] else empty_relation()
            n = apply_relation(data, out, a, b, day)
            f = fields.get((a, b))
            if not n and not f:
                continue
            llm = max(0, min(10, f["closeness"])) / 10 if f else row.get("llm_score")
            await self.db.save_relation(a, b, as_of, llm, f["kind"][:60] if f else row.get("kind"),
                                        f["tone"][:60] if f else row.get("tone"),
                                        f["summary"][:300] if f else row.get("summary"),
                                        render_relation(data, self.chat), data)
            self.stat["relations"] += 1

    def _mark(self, w: Episode, status: str, error: str | None = None):
        self.cache.db.execute(
            "insert or replace into analyzed(episode_id, last_id, status, error, updated) values (?, ?, ?, ?, ?)",
            (w.id, w.last_id, status, error, int(time.time())))

    async def _run_step(self, wins, first, last, now):
        """Шаг; если не влез в контекст — делим пополам."""
        try:
            await self.step(wins, first, last, now)
        except _TooBig:
            mid = (first + last) // 2
            await self._run_step(wins, first, mid, now)
            await self._run_step(wins, mid, last, now)

    # ================================================================ прогон

    async def run(self, reason: str = "daily") -> dict:
        if self.running:
            raise RuntimeError("разбор уже идёт")
        self.running = True
        started = time.time()
        self.stat = {"transcribed": 0, "windows": 0, "steps": 0, "dossiers": 0, "relations": 0}
        self._text_cache, self._tok_cache = {}, {}
        try:
            self.state = {"state": "running", "reason": reason, "started": int(started)}
            await self._progress(stage="подготовка")
            self.stat["transcribed"] = await self.transcribe_pending()
            now = int(time.time())
            msgs = self.load_messages()
            wins = self.windows(msgs)
            steps = self.plan(wins, now)
            if not steps:
                await self._progress(state="done", stage="нечего разбирать")
                return self.stat
            await self._say(f"🧠 Разбор ({reason}): окон {sum(b - a for a, b in steps)}, шагов {len(steps)}")
            async with self.mgr.use():
                for i, (a, b) in enumerate(steps, 1):
                    when = datetime.fromtimestamp(wins[a].start, self.cfg.tz)
                    await self._progress(stage=f"шаг {i}/{len(steps)}: переписка за {when:%Y-%m-%d}",
                                         done=i - 1, total=len(steps))
                    try:
                        await self._run_step(wins, a, b, now)
                        self.stat["windows"] += b - a
                    except Exception as exc:  # noqa: BLE001 — окно с ошибкой повторится в следующий раз
                        log.exception("шаг %d", i)
                        for w in wins[a:b]:
                            self._mark(w, "error", str(exc)[:300])
                    self.stat["steps"] += 1
            self.state = {"state": "done", "finished": int(time.time()), "reason": reason, **self.stat}
            await self.db.set_pass(self.state, map_updated=True)
            errors = self.cache.db.execute("select count(*) from analyzed where status = 'error'").fetchone()[0]
            await self._say(f"✅ Разбор готов за {(time.time() - started) / 60:.0f} мин: окон {self.stat['windows']}, "
                            f"шагов {self.stat['steps']}, расшифровок "
                            f"{self.stat['transcribed']}, правок досье {self.stat['dossiers']}, связей "
                            f"{self.stat['relations']}" + (f". Окон с ошибкой: {errors} — повторю в следующий раз"
                                                          if errors else ""))
            self.cache.db.execute("delete from analyzed where status = 'error'")
            return self.stat
        except Exception as exc:
            self.state = {"state": "error", "error": str(exc)[:500], "updated": int(time.time())}
            await self.db.set_pass(self.state)
            await self._say(f"❌ Разбор упал: {exc}")
            raise
        finally:
            self.running = False
            self._text_cache = {}


class _TooBig(Exception):
    """Шаг не влез в контекст модели — разделить."""
