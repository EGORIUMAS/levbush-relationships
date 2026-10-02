"""Пересказ: всё, что было в группе с указанного момента (не раньше RETELL_MAX_HOURS назад) до сейчас.

Модель — Qwen: сначала общий сервер пользователя (:8080, просыпается быстро), если там ничего нет — свой levbush-qwen.
Nemotron ради пересказа не поднимается (долго): Qwen получает картинки, кадры видео, расшифровки Parakeet и готовые
описания звука и видео, если Nemotron уже их сделал (ежедневный разбор или /describe).
Если всё не влезает в один запрос — пересказ по частям и итоговая сводка.
"""
from contextlib import asynccontextmanager
import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta

from . import llm as L
from .analyze import Analyzer
from .gpu import served_models
from .render import msg_link

log = logging.getLogger("levbush.retell")

PROMPT = (
    "Перескажи, что происходило в групповом чате «{title}» с {start} по {end}. Это для участника, который всё "
    "пропустил. Сначала итог в 1–2 предложениях, потом по темам в хронологическом порядке: кто что говорил, "
    "предлагал, решил, о чём спорили, чем закончилось, что важного прислали (фото и кадры видео ты видишь сам, у "
    "речи есть расшифровка, у части вложений — описание). Людей называй по имени. На ключевые сообщения ставь ссылки "
    "[→](msg:123) — на одно сообщение, без диапазонов. Пиши живо, по-русски, без воды. Разметка: **жирный**, списки «- », ссылки. Не длиннее {limit} "
    "знаков.")


def md_to_tg(text: str, chat: dict) -> str:
    """Упрощённый Markdown ответа → HTML Telegram."""
    text = html.escape(text, quote=False)
    # [→](msg:123) и диапазоны [→](msg:123-125) — ссылка на первое сообщение
    text = re.sub(r"\[([^\]]+)\]\(msg:(\d+)(?:\s*[-‑–—]\s*\d+)?\)",
                  lambda m: f'<a href="{msg_link(chat, int(m.group(2)))}">{m.group(1)}</a>', text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    return text.strip()


class Retell:
    def __init__(self, analyzer: Analyzer):
        self.a = analyzer
        self.cfg = analyzer.cfg
        self.cache = analyzer.cache

    def parse_since(self, arg: str, now: datetime) -> datetime | None:
        """«2ч», «30м», «90 мин», «1д», «14:30», «вчера 18:00», «2026-09-27 14:30»."""
        s = (arg or "").strip().lower().replace(",", " ")
        if not s:
            return None
        m = re.fullmatch(r"(\d+(?:[.]\d+)?)\s*(м|мин|минут[аы]?|m|min|ч|час|часа|часов|h|д|дн|день|дня|d)", s)
        if m:
            n = float(m.group(1))
            unit = m.group(2)[0]
            delta = {"м": timedelta(minutes=n), "m": timedelta(minutes=n), "ч": timedelta(hours=n),
                     "h": timedelta(hours=n), "д": timedelta(days=n), "d": timedelta(days=n)}[unit]
            return now - delta
        day = now.date()
        if s.startswith("вчера"):
            day -= timedelta(days=1)
            s = s[5:].strip() or "00:00"
        elif s.startswith("сегодня"):
            s = s[7:].strip() or "00:00"
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2})[:.](\d{2})", s)
        if m:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)), tzinfo=self.cfg.tz)
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
        if m:
            t = datetime(day.year, day.month, day.day, int(m.group(1)), int(m.group(2)), tzinfo=self.cfg.tz)
            if t > now:
                t -= timedelta(days=1)
            return t
        return None

    async def run(self, since_ts: int, limit_chars: int = 3500, progress=None) -> str:
        """progress(done, total) — чтобы бот показывал «часть 3/12»."""
        now = int(time.time())
        earliest = now - self.cfg.retell_max_hours * 3600
        if since_ts < earliest:
            raise ValueError(f"не дальше чем на {self.cfg.retell_max_hours} ч назад")
        rows = self.cache.messages_between(since_ts)
        rows = [m for m in rows if not m["service"]]
        if not rows:
            return "За это время в группе ничего не писали."
        await self.a.transcribe_pending([m["id"] for m in rows if m["media"]], report=False)
        rows = self.cache.messages_between(since_ts)      # с расшифровками
        rows = [m for m in rows if not m["service"]]

        async with self.server() as (llm, ctx):
            return await self._run(llm, ctx, rows, since_ts, now, limit_chars, progress)

    @asynccontextmanager
    async def server(self):
        """(клиент, контекст). Порядок: свой Qwen, если поднят → Nemotron, если поднят или поднимается (разбор описывает
        медиа — не ждать конца этапа) → общий :8080 → поднять свой Qwen. Чужие модели пересказ не гасит."""
        q, nem = self.a.qwen, self.a.mgr
        if q is None or not await q.is_up():
            if nem.unit_active():
                async with nem.use():
                    log.info("пересказ — на Nemotron (он уже поднят)")
                    yield L.LLM(nem.url, nem.model), self.cfg.llm_ctx
                return
        if q is None:
            async with nem.use():
                yield L.LLM(nem.url, nem.model), self.cfg.llm_ctx
            return
        if not await q.is_up():
            shared = await served_models(self.cfg.fallback_llm_url)
            if shared:
                log.info("пересказ — на общем сервере %s (%s)", self.cfg.fallback_llm_url, shared[0]["id"])
                yield (L.LLM(self.cfg.fallback_llm_url, shared[0]["id"], timeout=1800),
                       int(shared[0].get("max_model_len") or 131072))
                return
        async with q.use():
            yield L.LLM(q.url, q.model or q.fixed_model), q.ctx(self.cfg.qwen_ctx)

    async def _run(self, llm, ctx, rows, since_ts, now, limit_chars, progress) -> str:
        chat = self.a.chat
        title = chat.get("title") or "группа"
        fmt = lambda t: datetime.fromtimestamp(t, self.cfg.tz).strftime("%d.%m %H:%M")  # noqa: E731
        budget_tokens = min(self.cfg.retell_tokens, ctx - 12000)
        # части по бюджету (текст + медиа); у пересказа части крупнее, чем у разбора: думать почти не нужно
        parts, cur, size = [], [], 0
        for m in rows:
            n = int(len(self.a.r.compact_line(m)) / 2.8) + (self.a.media_cost(m) if m["media"] else 0)
            if cur and size + n > budget_tokens:
                parts.append(cur)
                cur, size = [], 0
            cur.append(m)
            size += n
        if cur:
            parts.append(cur)

        if len(parts) == 1:
            return md_to_tg(await self._one(llm, budget_tokens, parts[0], title, fmt(rows[0]["date"]), fmt(now),
                                            limit_chars), chat)
        # части параллельно (vLLM держит до LLM_SEQS запросов сразу), порядок сохраняется
        sem = asyncio.Semaphore(self.cfg.llm_parallel)
        done = 0

        async def one(part):
            nonlocal done
            async with sem:
                text = await self._one(llm, budget_tokens, part, title, fmt(part[0]["date"]),
                                       fmt(part[-1]["date"]), 2500)
            done += 1
            if progress:
                await progress(done, len(parts))
            return text

        partial = await asyncio.gather(*(one(p) for p in parts))
        joined = "\n\n".join(f"### Часть {i}: {fmt(p[0]['date'])}–{fmt(p[-1]['date'])}\n{t}"
                               for i, (p, t) in enumerate(zip(parts, partial), 1))
        prompt = (PROMPT.format(title=title, start=fmt(rows[0]["date"]), end=fmt(now), limit=limit_chars)
                  + "\n\nНиже пересказы последовательных частей переписки — сведи их в один связный пересказ, "
                    "сохрани ссылки на сообщения.\n\n" + joined)
        out = await llm.chat([{"role": "user", "content": prompt}], max_tokens=4000,
                             think=self.cfg.retell_think_budget > 0, think_budget=self.cfg.retell_think_budget)
        return md_to_tg(out, chat)

    async def _one(self, llm, budget_tokens, msgs, title, start, end, limit_chars) -> str:
        budget = self.a.budget(budget_tokens)
        content = await asyncio.to_thread(       # ffmpeg (кадры) — не в цикле бота
            self.a.interleave, [("Переписка (#номер ЧЧ:ММ Автор ↩кому ответ: текст):", msgs)], budget, True)
        content.append(L.text(PROMPT.format(title=title, start=start, end=end, limit=limit_chars)))
        think = dict(think=self.cfg.retell_think_budget > 0, think_budget=self.cfg.retell_think_budget)
        try:
            return await llm.chat([{"role": "user", "content": content}], max_tokens=4000, **think)
        except L.LLMError as exc:
            log.warning("пересказ с вложениями не прошёл (%s) — без вложений", exc)
            text = "\n".join(self.a.r.compact_line(m) for m in msgs)
            return await llm.chat(
                [{"role": "user", "content": "Переписка:\n\n" + text + "\n\n" +
                  PROMPT.format(title=title, start=start, end=end, limit=limit_chars)}], max_tokens=4000, **think)
