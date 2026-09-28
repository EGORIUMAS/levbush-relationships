"""Levbush Relationships — бот: статистика, досье, связи, пересказ, карта.

Один процесс: PTB — команды и новые сообщения, правки, входы/выходы (privacy mode выключен); Telethon — история,
докачка пропущенного, реакции поимённо, удаления, медиа, аватарки; задания — статистика, ежедневный разбор.
"""
import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta

from telegram import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, InlineKeyboardButton,
                      InlineKeyboardMarkup, MenuButtonWebApp, Update, WebAppInfo)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (AIORateLimiter, Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler,
                          ContextTypes, MessageHandler, MessageReactionHandler, filters)

from . import stats as S
from .analyze import Analyzer
from .cache import Cache
from .config import Config, cfg
from .gpu import LLMManager, served_models
from .normalize import from_ptb, ptb_chat_row, ptb_user_row, reaction_key
from .remote import DB
from .retell import Retell, md_to_tg
from .tg_client import TG, SessionBusy

log = logging.getLogger("levbush.bot")

PERIODS = {"d": "сегодня", "w": "эта неделя", "m": "этот месяц", "a": "всё время", "avg": "в среднем за день"}
CARD_ROWS = [
    ("msgs", "replies", "quotes", "comments", "forwards"),
    ("reactions", "reactions_recv", "mentions", "mentions_recv", "replies_recv", "quotes_recv"),
    ("media", "photos", "videos", "gifs", "documents", "audios"),
    ("video_notes", "voices", "stickers", "polls", "links"),
    ("words", "chars", "edits"),
]
TOP_METRICS = ["msgs", "replies", "quotes", "reactions", "reactions_recv", "media", "video_notes", "voices",
               "stickers", "forwards", "mentions", "words"]


def bidi_close(text: str) -> str:
    """Закрывает незакрытые управляющие символы направления (ник maxim("⁧(" прячет U+2067 RLI): иначе весь текст
    после имени до конца строки переворачивается справа налево. Сам ник выглядит так же."""
    isolates = sum(text.count(c) for c in "\u2066\u2067\u2068") - text.count("\u2069")
    embeds = sum(text.count(c) for c in "\u202a\u202b\u202d\u202e") - text.count("\u202c")
    return text + "\u202c" * max(0, embeds) + "\u2069" * max(0, isolates)


def esc(x) -> str:
    return html.escape(bidi_close(str(x)), quote=False)


def split_html(text: str, limit: int = 4000) -> list[str]:
    """Режет длинный HTML по абзацам (теги внутри абзаца не разрываются)."""
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append((cur + para[:cut]).strip())
            cur, para = "", para[cut:]
        if len(cur) + len(para) + 2 > limit:
            out.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    return out or [""]


class Levbush:
    def __init__(self, config: Config):
        self.cfg = config
        config.ensure_dirs()
        self.cache = Cache(config.cache_db)
        self.db = DB(config)
        self.tg = TG(config, self.cache)
        self.tg_ok = False
        self.mgr = LLMManager(config, notify=self.notify_admin)                    # Nemotron: звук и видео
        self.qwen = (LLMManager(config, notify=self.notify_admin, kind="qwen")      # Qwen: досье, связи, пересказ
                     if config.qwen_model_path else None)
        if self.qwen:
            self.mgr.peers, self.qwen.peers = [self.qwen], [self.mgr]
        self.progress_msg = None                  # последнее сообщение админу о разборе — в нём прогресс
        self.progress_base = ""                   # его текст без блока прогресса
        self.progress_edit = 0.0
        self.analyzer: Analyzer | None = None
        self.retell: Retell | None = None
        self.app: Application | None = None
        self.dirty = True
        self.last_stats = 0.0
        self.retell_lock = asyncio.Lock()
        self.bg: set[asyncio.Task] = set()

    # ================================================================ служебное

    @property
    def initiated(self) -> bool:
        return bool(self.cache.get("initiated"))

    @property
    def chat(self) -> dict:
        return self.cache.get("chat", {}) or {}

    @property
    def chat_id(self) -> int | None:
        cid = self.chat.get("id")
        if cid:
            return cid
        return int(self.cfg.group) if self.cfg.group.lstrip("-").isdigit() else None

    async def notify_admin(self, text: str):
        """Сообщение админу. Пока идёт разбор, внизу — блок прогресса, и дальше обновляется именно это (последнее)
        сообщение, а не первое: уведомления о моделях не уносят прогресс вверх."""
        if not (self.app and self.cfg.admin_id):
            return
        prog = self.progress_text(self.analyzer.state) if self.analyzer and self.analyzer.running else ""
        try:
            msg = await self.app.bot.send_message(self.cfg.admin_id, esc(text) + (f"\n\n{prog}" if prog else ""),
                                                  parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramError as exc:
            log.warning("админу не отправилось: %s", exc)
            return
        if prog:
            self.progress_msg, self.progress_base, self.progress_edit = msg, esc(text), time.monotonic()

    def spawn(self, coro, name: str):
        task = asyncio.create_task(coro, name=name)
        self.bg.add(task)

        def done(t):
            self.bg.discard(t)
            if not t.cancelled() and t.exception():
                log.error("задача %s упала", name, exc_info=t.exception())
                asyncio.create_task(self.notify_admin(f"❌ {name}: {t.exception()}"))

        task.add_done_callback(done)
        return task

    @property
    def servers(self) -> list[LLMManager]:
        return [m for m in (self.mgr, self.qwen) if m]

    def progress_text(self, st: dict) -> str:
        """Этап разбора, сколько сделано и сколько примерно осталось."""
        if not st or st.get("state") != "running":
            return ""
        out = f"🧠 {esc(st.get('stage') or 'разбор')}"
        if st.get("total"):
            done, total = st.get("done") or 0, st["total"]
            out += f": {done}/{total} ({100 * done // max(1, total)} %)"
        if st.get("step_date"):
            out += f", переписка за {st['step_date']}"
        if st.get("eta") is not None:
            out += f"\n⏱ осталось примерно {S.fmt_duration(st['eta']) if st['eta'] else 'меньше минуты'}"
        if st.get("started"):
            out += f" (идёт {S.fmt_duration(time.time() - st['started'])})"
        return out

    async def show_progress(self, st: dict):
        """Прогресс разбора — в последнем сообщении админу о разборе, правится не чаще раза в 30 с."""
        if not self.app or not self.cfg.admin_id:
            return
        text = self.progress_text(st)
        if not text:
            self.progress_msg = None
            return
        now = time.monotonic()
        stage_changed = self.progress_msg is not None and getattr(self, "_progress_stage", None) != st.get("stage")
        if self.progress_msg is not None and not stage_changed and now - self.progress_edit < 30:
            return
        self.progress_edit, self._progress_stage = now, st.get("stage")
        base = self.progress_base if self.progress_msg is not None else ""
        body = (base + "\n\n" if base else "") + text
        try:
            if self.progress_msg is None:
                self.progress_msg = await self.app.bot.send_message(self.cfg.admin_id, body, parse_mode=ParseMode.HTML)
                self.progress_base = ""
            else:
                await self.progress_msg.edit_text(body, parse_mode=ParseMode.HTML)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("прогресс: %s", exc)
        except TelegramError as exc:
            log.warning("прогресс: %s", exc)

    def busy(self, name: str) -> bool:
        return any(t.get_name() == name for t in self.bg)

    async def allowed(self, update: Update) -> bool:
        """Участники группы (и админ). В самой группе — все."""
        user = update.effective_user
        chat = update.effective_chat
        if user is None:
            return False
        if user.id == self.cfg.admin_id or (chat and chat.id == self.chat_id):
            return True
        try:
            return await self.db.is_member(user.id)
        except Exception:  # noqa: BLE001
            return False

    def is_admin(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id == self.cfg.admin_id)

    def map_button(self, update: Update, frag: str = "", start: str = "") -> InlineKeyboardButton | None:
        if not self.cfg.webapp_url:
            return None
        private = update.effective_chat and update.effective_chat.type == ChatType.PRIVATE
        if private:
            # в Mini App хвост # занят данными входа (tgWebAppData) — человек/связь передаются в ?p= / ?r=
            return InlineKeyboardButton("🗺 Карта", web_app=WebAppInfo(
                self.cfg.webapp_url + (f"?{frag}" if frag else "")))
        url = self.cfg.webapp_url + (f"#{frag}" if frag else "")
        if self.cfg.webapp_name and self.app:
            link = f"https://t.me/{self.app.bot.username}/{self.cfg.webapp_name}" + (f"?startapp={start}" if start else "")
            return InlineKeyboardButton("🗺 Карта", url=link)
        return InlineKeyboardButton("🗺 Карта", url=url)

    # ================================================================ запуск

    async def post_init(self, app: Application):
        self.app = app
        await self.db.connect()
        await self.db.set_app_secrets(self.cfg.bot_token, self.cfg.admin_id)
        try:
            await self.tg.connect()
            self.tg_ok = True
        except (SessionBusy, RuntimeError) as exc:
            log.error("Telethon: %s", exc)
            await self.notify_admin(f"⚠️ Telethon не подключён: {exc}\nИстория и медиа не качаются, "
                                    f"бот собирает только новые сообщения.")
        skipped, removed = self.cache.skip_unwanted_media()
        if skipped:
            log.info("вложения, которые нейросеть не примет: сняты с очереди %d, удалено файлов %d", skipped, removed)
        self.analyzer = Analyzer(self.cfg, self.cache, self.db, self.mgr, notify=self.notify_admin, qwen=self.qwen)
        self.analyzer.on_progress = self.show_progress
        self.retell = Retell(self.analyzer)
        await app.bot.set_my_commands([
            BotCommand("stats", "статистика: /stats [@ник] или ответом"),
            BotCommand("me", "моя статистика"),
            BotCommand("top", "топ участников: /top [метрика] [d|w|m|a]"),
            BotCommand("pair", "пара: /pair @a [@b] или ответом"),
            BotCommand("dossier", "досье: /dossier [@ник] или ответом"),
            BotCommand("links", "связи человека"),
            BotCommand("retell", "пересказ: /retell 2ч | 14:30 | вчера 20:00 или ответом"),
            BotCommand("text", "расшифровка голосового или кружка (ответом; в личке — просто пришли)"),
            BotCommand("map", "карта связей"),
            BotCommand("help", "справка"),
        ], scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands([
            BotCommand("retell", "пересказ: /retell 2ч или ответом на сообщение"),
            BotCommand("text", "расшифровать голосовое или кружок (ответом)"),
            BotCommand("stats", "статистика"), BotCommand("top", "топ участников"),
            BotCommand("pair", "пара"), BotCommand("dossier", "досье"), BotCommand("map", "карта связей"),
        ], scope=BotCommandScopeAllGroupChats())
        if self.cfg.webapp_url:
            try:
                await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("Карта", WebAppInfo(self.cfg.webapp_url)))
            except TelegramError as exc:
                log.warning("кнопка меню: %s", exc)
        jq = app.job_queue
        jq.run_repeating(self.job_stats, interval=self.cfg.stats_interval, first=20)
        jq.run_repeating(self.job_media, interval=90, first=60)
        jq.run_repeating(self.job_reactions, interval=60, first=45)
        jq.run_repeating(self.job_participants, interval=6 * 3600, first=600)
        hh, mm = (int(x) for x in self.cfg.daily_at.split(":"))
        jq.run_daily(self.job_daily, time=datetime.now(self.cfg.tz).replace(hour=hh, minute=mm, second=0,
                                                                           microsecond=0).timetz())
        if self.tg_ok and self.initiated:
            self.tg.start_live(on_change=self.mark_dirty)
            if self.maintenance:
                await self.notify_admin("🔧 Бот запущен в режиме техобслуживания — /maintenance off, чтобы выйти")
            else:
                self.spawn(self.startup_sync(), "синхронизация")
                if self.cache.get("analysis_running"):         # разбор прервал перезапуск — продолжаем
                    await self.notify_admin("🔄 Продолжаю прерванный разбор")
                    self.spawn(self.analyzer.run("продолжение после перезапуска"), "разбор")
        elif self.tg_ok:
            await self.notify_admin("✅ Бот запущен, Telethon подключён к «" + str(self.chat.get("title")) + "».\n"
                                    "Сбор ещё не запускался — /initiate, когда будешь готов.")

    async def post_stop(self, app: Application):
        """Задачи отменяем, пока бот ещё на связи: пересказ успеет написать, что прерван."""
        tasks = list(self.bg)
        was_running = bool(self.analyzer and self.analyzer.running)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=10)
        if was_running and not self.maintenance:
            # отмена могла прийти ошибкой (модель уже гасится) и сбросить флаг — разбор всё равно продолжить
            self.cache.set("analysis_running", "продолжение после перезапуска")

    async def post_shutdown(self, app: Application):
        for t in list(self.bg):
            t.cancel()
        for mgr in self.servers:
            if mgr._idle_task and not mgr._idle_task.done():
                mgr._idle_task.cancel()
            if mgr.started_by_us:
                await mgr.stop()
        await self.tg.close()
        await self.db.close()

    async def startup_sync(self):
        first = not self.cache.get("history_synced")
        msg = None
        if first:
            msg = await self.app.bot.send_message(self.cfg.admin_id, "📥 Первый запуск: качаю историю группы…") \
                if self.cfg.admin_id else None
        last_edit = 0.0

        async def progress(done, total=None, what="сообщения"):
            nonlocal last_edit
            if msg is None or time.monotonic() - last_edit < 10:
                return
            last_edit = time.monotonic()
            text = f"📥 {what}: {done}" + (f" из ~{total}" if total else "")
            try:
                await msg.edit_text(text)
            except TelegramError:
                pass

        await self.tg.sync_participants()
        await self.tg.forget_contact_names()
        await self.refresh_names()
        n = await self.tg.sync_history(progress=progress)
        await self.refresh_names()                 # авторы старых сообщений появились только теперь
        self.dirty = True
        await self.push_stats(force=True)
        if first and msg:
            await msg.edit_text(f"📥 История скачана: {n} сообщений. Качаю медиа и профили…")
        await self.tg.sync_profiles()
        while await self.tg.download_pending(limit=500, progress=progress if first else None):
            pass
        self.dirty = True
        await self.push_stats(force=True)
        if first and self.cfg.admin_id:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧠 Запустить разбор", callback_data="an:run"),
                                        InlineKeyboardButton("Позже", callback_data="an:later")]])
            await self.app.bot.send_message(
                self.cfg.admin_id,
                f"✅ Кэш готов: {self.cache.db.execute('select count(*) from messages').fetchone()[0]} сообщений, "
                f"медиа скачано. Статистика уже на карте.\n\nЗапустить первый разбор истории нейросетью "
                f"(Nemotron займёт ~28 ГиБ VRAM на всё время прогона; пока идёт генерация H3, будет ждать)?",
                reply_markup=kb)

    # ================================================================ задания

    async def push_stats(self, force: bool = False):
        if not (self.dirty or force):
            return
        self.dirty = False
        await self.resolve_custom_emoji()
        res = await asyncio.to_thread(S.compute, self.cache, self.cfg)
        sent = await self.db.push_stats(res, self.cfg)
        self.last_stats = time.time()
        log.info("статистика выгружена: %s", sent)

    async def job_stats(self, ctx: ContextTypes.DEFAULT_TYPE):
        if self.maintenance:
            return
        try:
            await self.push_stats()
        except Exception:  # noqa: BLE001
            log.exception("статистика")
            self.dirty = True

    def mark_dirty(self):
        self.dirty = True

    async def job_reactions(self, ctx):
        if self.tg_ok and self.initiated and not self.maintenance and not self.busy("синхронизация"):
            try:
                await self.tg.flush_reactions()
            except Exception:  # noqa: BLE001
                log.exception("реакции")

    async def job_media(self, ctx):
        if self.tg_ok and self.initiated and not self.maintenance and not self.busy("синхронизация"):
            try:
                await self.tg.download_pending(limit=200)
            except Exception:  # noqa: BLE001
                log.exception("медиа")

    async def refresh_names(self) -> int:
        """Имена и ники — от бота (getChatMember): у Telethon для контактов имена из записной книжки."""
        ids = [r[0] for r in self.cache.db.execute(
            """select id from users u where kind = 'user' and not is_bot and (is_member = 1
               or exists (select 1 from messages m where m.sender_id = u.id)
               or exists (select 1 from reactions r where r.user_id = u.id))""")]
        n = 0
        for uid in ids:
            try:
                cm = await self.app.bot.get_chat_member(self.chat_id, uid)
            except TelegramError:
                continue                           # никогда не был в группе / удалён — остаётся имя от Telethon
            before = self.cache.user(uid)
            self.cache.upsert_user(ptb_user_row(cm.user))
            after = self.cache.user(uid)
            if any(before[k] != after[k] for k in ("first_name", "last_name", "username")):
                n += 1
            await asyncio.sleep(0.1)
        if n:
            self.dirty = True
        log.info("имена от бота: сменилось %d из %d", n, len(ids))
        return n

    async def job_participants(self, ctx):
        if self.tg_ok and self.initiated and not self.maintenance and not self.busy("синхронизация"):
            try:
                await self.tg.sync_participants()
                await self.refresh_names()
                self.dirty = True
            except Exception:  # noqa: BLE001
                log.exception("участники")

    async def job_daily(self, ctx):
        if self.initiated and not self.maintenance and not self.busy("ежедневный разбор"):
            self.spawn(self.daily(), "ежедневный разбор")

    @property
    def analysis_started(self) -> bool:
        """Первый прогон нейросетью запускает только админ (/analyze или кнопка); до этого — без разбора."""
        return bool(self.cache.get("analysis_started"))

    async def daily(self):
        if self.tg_ok:
            await self.tg.sync_history()
            await self.tg.refresh_reactions(int(time.time()) - 3 * 86400)
            await self.tg.sync_participants()
            await self.tg.forget_contact_names()
            changed = await self.refresh_names()          # имена и ники — только от бота
            prof = await self.tg.sync_profiles()           # аватарки и био — раз в сутки на человека
            log.info("ежедневная проверка профилей: сменилось имён/ников %d, аватарок %d, био %d",
                     changed, prof["avatars"], prof["bios"])
            while await self.tg.download_pending(limit=500):
                pass
        self.dirty = True
        await self.push_stats(force=True)
        if self.analysis_started:
            await self.analyzer.run("daily")
        else:
            log.info("ежедневный разбор пропущен: первый прогон ещё не запускали (/analyze)")

    # ================================================================ сбор

    def in_group(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == self.chat_id

    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = update.effective_message
        if m is None or not self.in_group(update) or not self.initiated:
            return
        row = from_ptb(m)
        if row is None:
            return
        with self.cache.tx() as db:
            if m.from_user:
                self.cache.upsert_user(ptb_user_row(m.from_user), db)
            if m.sender_chat:
                self.cache.upsert_user(ptb_chat_row(m.sender_chat), db)
            if m.forward_origin is not None:
                o = m.forward_origin
                if o.type == "user":
                    self.cache.upsert_user(ptb_user_row(o.sender_user), db)
            self.cache.upsert_message(row, db)
            date = row["date"]
            for u in m.new_chat_members or ():
                self.cache.upsert_user({**ptb_user_row(u), "is_member": 1}, db)
                self.cache.add_membership(u.id, date, "join", "bot", db)
            if m.left_chat_member:
                self.cache.upsert_user({**ptb_user_row(m.left_chat_member), "is_member": 0}, db)
                self.cache.add_membership(m.left_chat_member.id, date, "leave", "bot", db)
        self.dirty = True

    async def on_reaction(self, update: Update, ctx):
        r = update.message_reaction
        if r is None or r.chat.id != self.chat_id:
            return
        who = r.user.id if r.user else (r.actor_chat.id if r.actor_chat else None)
        if who is None:
            return
        if r.user:
            self.cache.upsert_user(ptb_user_row(r.user))
        emojis = [k for k in (reaction_key(x) for x in r.new_reaction) if k]
        self.cache.update_user_reactions(r.message_id, who, emojis, int(r.date.timestamp()))
        self.dirty = True

    async def on_reaction_count(self, update: Update, ctx):
        r = update.message_reaction_count
        if r is None or r.chat.id != self.chat_id:
            return
        counts = {k: rc.total_count for rc in r.reactions if (k := reaction_key(rc.type))}
        self.cache.set_reaction_counts(r.message_id, counts)
        self.dirty = True

    async def on_member(self, update: Update, ctx):
        cm = update.chat_member
        if cm is None or cm.chat.id != self.chat_id:
            return
        was, now = cm.difference().get("status", (None, None))
        member_states = {"member", "administrator", "creator", "restricted"}
        user = cm.new_chat_member.user
        date = int(cm.date.timestamp())
        if now in member_states and was not in member_states:
            self.cache.upsert_user({**ptb_user_row(user), "is_member": 1})
            self.cache.add_membership(user.id, date, "join", "bot")
        elif was in member_states and now not in member_states:
            self.cache.upsert_user({**ptb_user_row(user), "is_member": 0})
            self.cache.add_membership(user.id, date, "leave", "bot")
        self.dirty = True

    # ================================================================ команды: разбор аргументов

    async def target(self, update: Update, args: list[str], default_self=True):
        """(id, имя) из аргумента @ник/имени/id, из ответа или сам пользователь."""
        m = update.effective_message
        if args:
            row = await self.db.find_person(" ".join(args))
            if row:
                return row["id"], row["name"]
            return None, None
        if m and m.reply_to_message:
            r = m.reply_to_message
            uid = r.sender_chat.id if r.sender_chat else (r.from_user.id if r.from_user else None)
            if uid:
                row = await self.db.find_person(str(uid))
                return uid, row["name"] if row else (r.from_user.full_name if r.from_user else str(uid))
        if default_self and update.effective_user:
            u = update.effective_user
            return u.id, u.full_name
        return None, None

    async def reply(self, update: Update, text: str, markup=None):
        m = update.effective_message
        chunks = split_html(text)
        for i, chunk in enumerate(chunks):
            await m.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                               reply_markup=markup if i == len(chunks) - 1 else None)

    # ================================================================ команды

    async def cmd_help(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        text = (
            "<b>Levbush Relationships</b> — статистика группы, досье и карта связей.\n\n"
            "/stats [@ник] — статистика (или ответом на сообщение)\n/me — моя статистика\n"
            "/top [метрика] [d|w|m|a] — топ: " + ", ".join(TOP_METRICS) + "\n"
            "/pair @a [@b] — кто кого: ответы, цитаты, упоминания, реакции\n"
            "/dossier [@ник] — досье, /links [@ник] — связи\n"
            f"/retell 2ч | 30м | 14:30 | вчера 20:00 — пересказ (не дальше {self.cfg.retell_max_hours} ч), "
            "или ответом на сообщение — с него\n/text — расшифровка голосового или кружка (ответом на него; в личке "
            "бота можно просто прислать голосовое)\n/map — карта связей")
        if self.is_admin(update):
            text += ("\n\nАдмин: /initiate (запуск сбора), /status, /analyze [stop], /describe (описать новые видео и "
                     "голосовые Nemotron'ом сейчас), /sync, "
                     "/maintenance on [причина] | off — техобслуживание")
        btn = self.map_button(update)
        await self.reply(update, text, InlineKeyboardMarkup([[btn]]) if btn else None)

    def card(self, p: dict, period: str) -> str:
        person = p["person"]
        head = f"📊 <b>{esc(person['name'])}</b>" + (f" (@{esc(person['username'])})" if person.get("username") else "")
        if period == "a":
            c = p["total"].get("c", {})
        elif period == "avg":
            c = p.get("avg_per_day", {})
        else:
            lst = p["periods"].get(period) or []
            c = lst[0]["c"] if lst else {}
        lines = [head + f" — {PERIODS[period]}"]
        for row in CARD_ROWS:
            items = []
            for k in row:
                v = c.get(k, 0)
                if v:
                    v = f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else v
                    items.append(f"{S.LABELS[k]} <b>{v}</b>")
            if items:
                lines.append(" · ".join(items))
        if c.get("voice_sec") or c.get("video_note_sec"):
            lines.append(f"голосовых {S.fmt_duration(c.get('voice_sec'))} · кружков {S.fmt_duration(c.get('video_note_sec'))}")
        if len(lines) == 1:
            lines.append("Активности нет.")
        if period in ("a", "avg"):
            t = p["total"]
            fj = person.get("first_join")
            facts = []
            if fj:
                facts.append(f"первый вход {fj[:10]}" + ("" if person.get("first_join_exact", True) else " (≈ по 1-му сообщению)"))
            facts.append(f"в группе {S.fmt_duration(person.get('time_in_group_sec'))}"
                         + ("" if person.get("is_member") else ", сейчас вышел"))
            if t.get("mean_hour") is not None:
                facts.append(f"обычно пишет около {int(t['mean_hour']):02d}:{int(t['mean_hour'] % 1 * 60):02d}, "
                             f"пик в {t.get('peak_hour')} ч")
            if t.get("avg_session_min"):
                facts.append(f"сессия в среднем {t['avg_session_min']} мин, активен ~{t.get('active_min_per_day')} мин "
                             f"в день, когда пишет")
            if t.get("median_reply_sec") is not None:
                facts.append(f"отвечает в среднем за {S.fmt_duration(t['median_reply_sec'])}")
            if t.get("active_days"):
                facts.append(f"активных дней {t['active_days']}, лучшая серия {t.get('longest_streak')} дн., "
                             f"начал бесед {t.get('conversations_started', 0)}")
            lines.append("")
            lines += [f"• {esc(f)}" for f in facts]
            if t.get("top_reactions"):
                lines.append("• любимые реакции: " + " ".join(f"{self.reaction_html(*r)}×{r[1]}"
                                                             for r in t["top_reactions"]))
        return "\n".join(lines)

    @staticmethod
    def reaction_html(key: str, n=None, alt: str | None = None) -> str:
        """Премиум-реакция — настоящим кастомным эмодзи (tg-emoji), с обычным аналогом на случай, если не покажется."""
        if key.startswith("custom:"):
            return f'<tg-emoji emoji-id="{key[7:]}">{esc(alt or "⭐")}</tg-emoji>'
        return "⭐" if key == "paid" else esc(key)

    async def resolve_custom_emoji(self):
        """Узнаёт у Telegram обычные эмодзи-аналоги премиум-реакций (getCustomEmojiStickers, до 200 за раз)."""
        known = self.cache.get("custom_emoji") or {}
        ids = [r[0][7:] for r in self.cache.db.execute(
            "select distinct emoji from reactions where emoji like 'custom:%'") if r[0][7:] not in known]
        for i in range(0, len(ids), 200):
            try:
                stickers = await self.app.bot.get_custom_emoji_stickers(ids[i:i + 200])
            except TelegramError as exc:
                log.warning("премиум-эмодзи: %s", exc)
                return
            for st in stickers:
                known[st.custom_emoji_id] = st.emoji or "⭐"
        if ids:
            self.cache.set("custom_emoji", known)
            self.dirty = True

    def card_kb(self, update: Update, uid: int, period: str):
        row = [InlineKeyboardButton(("• " if k == period else "") + t, callback_data=f"st:{uid}:{k}")
               for k, t in (("d", "День"), ("w", "Неделя"), ("m", "Месяц"), ("a", "Всё"), ("avg", "Ср/день"))]
        row2 = [InlineKeyboardButton("📄 Досье", callback_data=f"ds:{uid}"),
                InlineKeyboardButton("🔗 Связи", callback_data=f"ln:{uid}")]
        btn = self.map_button(update, f"p={uid}", f"p{uid}")
        if btn:
            row2.append(btn)
        return InlineKeyboardMarkup([row, row2])

    async def cmd_stats(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        uid, name = await self.target(update, ctx.args)
        if uid is None:
            await self.reply(update, "Не нашёл такого участника.")
            return
        p = await self.db.call("api_person", uid)
        if not p:
            await self.reply(update, f"По {esc(name)} пока нет данных.")
            return
        await self.reply(update, self.card(p, "a"), self.card_kb(update, uid, "a"))

    async def cmd_me(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        uid = update.effective_user.id
        p = await self.db.call("api_person", uid)
        if not p:
            await self.reply(update, "По тебе пока нет данных.")
            return
        await self.reply(update, self.card(p, "a"), self.card_kb(update, uid, "a"))

    async def cmd_top(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        metric, period = "msgs", "d"
        for a in ctx.args:
            if a in S.LABELS:
                metric = a
            elif a in ("d", "w", "m", "a"):
                period = a
        await self.send_top(update, metric, period)

    async def top_text(self, metric: str, period: str) -> str:
        rows = await self.db.call("api_top", metric, period, 15)
        title = f"🏆 {S.LABELS.get(metric, metric)} — {PERIODS[period]}"
        if not rows:
            return title + "\n\nПока пусто."
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(r['name'])} — <b>{r['n']}</b>" for i, r in enumerate(rows)]
        return title + "\n\n" + "\n".join(lines)

    def top_kb(self, metric: str, period: str):
        prow = [InlineKeyboardButton(("• " if k == period else "") + t, callback_data=f"tp:{metric}:{k}")
                for k, t in (("d", "День"), ("w", "Неделя"), ("m", "Месяц"), ("a", "Всё"))]
        mrows, row = [], []
        for k in TOP_METRICS:
            row.append(InlineKeyboardButton(("• " if k == metric else "") + S.LABELS[k], callback_data=f"tp:{k}:{period}"))
            if len(row) == 3:
                mrows.append(row)
                row = []
        if row:
            mrows.append(row)
        return InlineKeyboardMarkup([prow] + mrows)

    async def send_top(self, update: Update, metric: str, period: str):
        await self.reply(update, await self.top_text(metric, period), self.top_kb(metric, period))

    async def cmd_pair(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        args = ctx.args
        m = update.effective_message
        a = b = None
        if len(args) >= 2:
            ra, rb = await self.db.find_person(args[0]), await self.db.find_person(" ".join(args[1:]))
            a, b = (ra["id"] if ra else None), (rb["id"] if rb else None)
        elif len(args) == 1:
            r = await self.db.find_person(args[0])
            a, b = update.effective_user.id, (r["id"] if r else None)
            if m.reply_to_message:
                t, _ = await self.target(update, [], default_self=False)
                a, b = t, (r["id"] if r else None)
        elif m.reply_to_message:
            t, _ = await self.target(update, [], default_self=False)
            a, b = update.effective_user.id, t
        if not a or not b or a == b:
            await self.reply(update, "Нужны двое: /pair @a @b, /pair @b (я и он) или ответом на сообщение.")
            return
        await self.reply(update, *(await self.pair_text(update, a, b)))

    async def pair_text(self, update: Update, a: int, b: int):
        r = await self.db.call("api_relation", a, b)
        na, nb = r["a"]["name"] if r["a"] else a, r["b"]["name"] if r["b"] else b
        keys = [("replies", "ответы"), ("quotes", "цитаты"), ("mentions", "упоминания"), ("reactions", "реакции"),
                ("forwards", "пересылки")]
        lines = [f"🔗 <b>{esc(na)}</b> ↔ <b>{esc(nb)}</b>", ""]
        for k, t in keys:
            x, y = r["ab"].get(k, 0), r["ba"].get(k, 0)
            if x or y:
                lines.append(f"{t}: {x} → · ← {y}")
        rel = r.get("relation")
        if rel:
            lines.append("")
            lines.append(f"сила связи <b>{rel['strength']:.2f}</b>" + (f" · {esc(rel['kind'])}" if rel.get("kind") else "")
                         + (f" · {esc(rel['tone'])}" if rel.get("tone") else ""))
            if rel.get("summary"):
                lines.append(f"<i>{esc(rel['summary'])}</i>")
            if rel.get("description"):
                lines.append("")
                lines.append(md_to_tg(rel["description"], self.chat))
        elif len(lines) == 2:
            lines.append("Не взаимодействовали.")
        btn = self.map_button(update, f"r={a}-{b}", f"r{a}_{b}")
        return "\n".join(lines), InlineKeyboardMarkup([[btn]]) if btn else None

    @staticmethod
    def rich_md(text: str) -> str:
        """Markdown для rich-сообщения: $, ==, ||, < Rich Markdown понимает как разметку (формулы, выделение,
        спойлер, HTML) — экранируем."""
        text = re.sub(r"([$<])", r"\\\1", text)
        return text.replace("==", "=\\=").replace("||", "|\\|")

    @staticmethod
    def md_escape(text: str) -> str:
        return re.sub(r"([\\`*_\[\]()#~>|$<=!])", r"\\\1", bidi_close(str(text)))

    async def dossier_md(self, uid: int) -> str | None:
        """Досье в Markdown для rich-сообщения (None — нет такого участника)."""
        p = await self.db.call("api_person", uid)
        if not p:
            return None
        d = p.get("dossier")
        name = self.md_escape(p["person"]["name"])
        if not d:
            return f"📄 **{name}**\n\nДосье пока нет — нейросеть ещё не разбирала его переписку."
        return f"# 📄 {name}\n*досье на {d['as_of'][:10]}*\n\n" + self.rich_md(d["content"] or "")

    async def send_rich(self, message, md: str, fallback_html: str, markup=None):
        """Rich-сообщение (Bot API 10.1, sendRichMessage) ответом на message; не вышло — обычным HTML."""
        kwargs = {"chat_id": message.chat_id, "rich_message": {"markdown": md},
                  "reply_parameters": {"message_id": message.message_id, "allow_sending_without_reply": True}}
        if markup is not None:
            kwargs["reply_markup"] = markup.to_dict()
        if getattr(self, "_rich_ok", True) and len(md) <= 32000:
            try:
                return await self.app.bot.do_api_request("sendRichMessage", api_kwargs=kwargs)
            except TelegramError as exc:
                if "not found" in str(exc).lower() and "method" in str(exc).lower():
                    self._rich_ok = False
                log.warning("rich-сообщение не прошло (%s) — обычным HTML", exc)
        chunks = split_html(fallback_html)
        for i, chunk in enumerate(chunks):
            await message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                     reply_markup=markup if i == len(chunks) - 1 else None)

    async def dossier_text(self, uid: int) -> str:
        p = await self.db.call("api_person", uid)
        if not p:
            return "Нет такого участника."
        d = p.get("dossier")
        name = esc(p["person"]["name"])
        if not d:
            return f"📄 <b>{name}</b>\n\nДосье пока нет — нейросеть ещё не разбирала его переписку."
        head = f"📄 <b>{name}</b> — досье на {d['as_of'][:10]}"
        return head + "\n\n" + md_to_tg(d["content"] or "", self.chat)

    async def cmd_dossier(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        uid, name = await self.target(update, ctx.args)
        if uid is None:
            await self.reply(update, "Не нашёл такого участника.")
            return
        btn = self.map_button(update, f"p={uid}", f"p{uid}")
        md = await self.dossier_md(uid)
        markup = InlineKeyboardMarkup([[btn]]) if btn else None
        if md is None:
            await self.reply(update, "Нет такого участника.")
            return
        await self.send_rich(update.effective_message, md, await self.dossier_text(uid), markup)

    async def links_text(self, uid: int) -> str:
        p = await self.db.call("api_person", uid)
        if not p:
            return "Нет такого участника."
        rels = p.get("relations") or []
        lines = [f"🔗 Связи: <b>{esc(p['person']['name'])}</b>", ""]
        for r in rels[:25]:
            bar = "█" * max(1, round(r["strength"] * 10))
            extra = " · ".join(x for x in (r.get("kind"), r.get("tone")) if x)
            lines.append(f"{bar} <b>{esc(r['name'])}</b> {r['strength']:.2f}" + (f" — {esc(extra)}" if extra else ""))
            if r.get("summary"):
                lines.append(f"   <i>{esc(r['summary'])}</i>")
        if not rels:
            lines.append("Связей пока нет.")
        return "\n".join(lines)

    async def cmd_links(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        uid, _ = await self.target(update, ctx.args)
        if uid is None:
            await self.reply(update, "Не нашёл такого участника.")
            return
        await self.reply(update, await self.links_text(uid))

    async def cmd_map(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        btn = self.map_button(update)
        if not btn:
            await self.reply(update, "Адрес карты не настроен (LEVBUSH_WEBAPP_URL).")
            return
        await self.reply(update, "🗺 Карта связей группы:", InlineKeyboardMarkup([[btn]]))

    async def cmd_retell(self, update: Update, ctx):
        if not await self.allowed(update):
            return
        m = update.effective_message
        now = datetime.now(self.cfg.tz)
        since = None
        if m.reply_to_message:
            since = m.reply_to_message.date.astimezone(self.cfg.tz)
        elif ctx.args:
            since = self.retell.parse_since(" ".join(ctx.args), now)
        if since is None:
            await self.reply(update, "С какого момента? /retell 2ч · /retell 30м · /retell 14:30 · "
                                     "/retell вчера 20:00 — или ответь командой на сообщение.")
            return
        if now - since > timedelta(hours=self.cfg.retell_max_hours):
            await self.reply(update, f"Пересказ — не дальше чем на {self.cfg.retell_max_hours} ч назад.")
            return
        if not self.initiated:
            await self.reply(update, "Сбор переписки ещё не запущен — пересказывать нечего.")
            return
        if self.retell_lock.locked():
            await self.reply(update, "⏳ Уже готовлю другой пересказ, подожди немного.")
            return
        status = await m.reply_text(f"⏳ Готовлю пересказ с {since:%d.%m %H:%M}…")
        self.spawn(self._retell(update, status, int(since.timestamp())), "пересказ")

    async def _retell(self, update: Update, status, since_ts: int):
        async with self.retell_lock:
            try:
                if self.tg_ok:
                    # новые сообщения и так приходят вживую; медиа отрезка — сразу, мимо очереди начального сбора
                    await self.tg.download_since(since_ts)
                if self.qwen and not await self.qwen.is_up() and not await served_models(self.cfg.fallback_llm_url):
                    await status.edit_text(f"⏳ Поднимаю {self.qwen.label} (~1–2 мин), потом перескажу…")
                last = 0.0

                async def progress(done, total):
                    nonlocal last
                    if time.monotonic() - last > 5 or done == total:
                        last = time.monotonic()
                        try:
                            await status.edit_text(f"⏳ Пересказываю: часть {done}/{total}" +
                                                   (" — свожу в один текст…" if done == total else ""))
                        except TelegramError:
                            pass

                text = await self.retell.run(since_ts, progress=progress)
            except asyncio.CancelledError:
                why = ("бот ушёл на техобслуживание" if self.maintenance
                       else "бот перезапускается — попробуй ещё раз через минуту")
                await asyncio.shield(status.edit_text(f"🔧 Пересказ прерван: {why}."))
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("пересказ")
                await status.edit_text(f"❌ Не получилось: {esc(exc)}", parse_mode=ParseMode.HTML)
                return
            # готовый пересказ — новым сообщением ответом на команду (придёт уведомление), «Готовлю…» — удалить
            asked = update.effective_message
            for chunk in split_html(text):
                try:
                    await asked.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                except BadRequest as exc:
                    if "not found" in str(exc).lower():          # команду успели удалить — просто в чат
                        await status.chat.send_message(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    else:
                        await asked.reply_text(re.sub(r"<[^>]+>", "", chunk)[:4000])
            try:
                await status.delete()
            except TelegramError:
                pass

    # ------------------------------------------------------------ расшифровка голосовых и кружков

    @staticmethod
    def speech_of(m):
        """Голосовое, кружок, аудио или видео сообщения — (объект файла, подпись) или None."""
        for attr, label in (("voice", "голосовое"), ("video_note", "кружок"), ("audio", "аудио"), ("video", "видео")):
            obj = getattr(m, attr, None)
            if obj:
                return obj, label
        return None

    async def cmd_text(self, update: Update, ctx):
        """/text ответом на голосовое или кружок — расшифровка Parakeet (для всех участников группы)."""
        if not await self.allowed(update):
            return
        m = update.effective_message
        target = m.reply_to_message if m.reply_to_message and self.speech_of(m.reply_to_message) else m
        if not self.speech_of(target):
            await self.reply(update, "Ответь командой /text на голосовое или кружок — пришлю расшифровку. "
                                     "В личке можно просто прислать или переслать мне голосовое.")
            return
        await self.transcribe_reply(target, m)

    async def on_private_speech(self, update: Update, ctx):
        """Голосовое или кружок в личке — сразу расшифровка (только участникам группы)."""
        m = update.effective_message
        if m is None or update.effective_chat.type != ChatType.PRIVATE or not self.speech_of(m):
            return
        if not await self.allowed(update) or (self.maintenance and not self.is_admin(update)):
            return
        await self.transcribe_reply(m, m)

    async def transcribe_reply(self, target, answer_to):
        obj, label = self.speech_of(target)
        dur = getattr(obj, "duration", None) or 0
        in_group = target.chat_id == self.chat_id
        text = self.cache.transcript(target.message_id) if in_group else None
        if not text:
            if (obj.file_size or 0) > 20 * 1024 * 1024:
                await answer_to.reply_text("Файл больше 20 МБ — бот не может его скачать.")
                return
            status = await answer_to.reply_text(f"🗣 Расшифровываю {label}…")
            tmp = self.cfg.data_dir / "tmp"
            tmp.mkdir(exist_ok=True)
            path = tmp / f"speech-{target.chat_id}-{target.message_id}"
            try:
                f = await obj.get_file()
                await f.download_to_drive(path)
                text, seconds = await self.analyzer.transcribe_file(str(path))
                dur = dur or seconds
            except Exception as exc:  # noqa: BLE001
                log.exception("расшифровка")
                await status.edit_text(f"❌ Не получилось: {esc(exc)}", parse_mode=ParseMode.HTML)
                return
            finally:
                path.unlink(missing_ok=True)
            if in_group and text:
                self.cache.set_transcript(target.message_id, text, dur, "parakeet-tdt-0.6b-v3")
        else:
            status = None
        head = f"🗣 <b>Расшифровка</b> ({label}{', ' + S.fmt_duration(dur) if dur else ''}):\n"
        body = esc(text) if text else "<i>речи не слышно</i>"
        chunks = split_html(head + body)
        if status is not None:
            await status.edit_text(chunks[0], parse_mode=ParseMode.HTML)
        else:
            await answer_to.reply_text(chunks[0], parse_mode=ParseMode.HTML)
        for chunk in chunks[1:]:
            await answer_to.reply_text(chunk, parse_mode=ParseMode.HTML)

    # ------------------------------------------------------------ админ

    async def cmd_status(self, update: Update, ctx):
        if not self.is_admin(update):
            return
        c = self.cache.db
        n = c.execute("select count(*) from messages").fetchone()[0]
        pend = c.execute("select count(*) from messages where media_state = 'pending'").fetchone()[0]
        tr = c.execute("select count(*) from transcripts where text <> ''").fetchone()[0]
        done = c.execute("select count(*) from analyzed where status = 'done'").fetchone()[0]
        last_sync = self.cache.get("last_sync")
        st = self.analyzer.state if self.analyzer else {}
        lines = [
            f"<b>Группа:</b> {esc(self.chat.get('title'))} ({self.chat_id})",
            f"Telethon: {'✅' if self.tg_ok else '❌'}, последняя докачка "
            f"{datetime.fromtimestamp(last_sync, self.cfg.tz):%d.%m %H:%M}" if last_sync else "Telethon: нет докачки",
            f"Сообщений в кэше: {n}, медиа в очереди: {pend}, расшифровок: {tr}",
            f"Разобрано окон: {done}",
            f"Статистика выгружена: {datetime.fromtimestamp(self.last_stats, self.cfg.tz):%H:%M:%S}"
            if self.last_stats else "Статистика ещё не выгружалась",
            *[f"{m.label}: {'работает' if await m.is_up() else 'не запущен'}" for m in self.servers],
            f"Описаний медиа: {c.execute('select count(*) from media_desc where text <> \'\'').fetchone()[0]}",
            (self.progress_text(st) or f"Разбор: {esc(st.get('state', '—'))} {esc(st.get('stage', ''))}"),
            f"Сбор: {'запущен' if self.initiated else 'не запускался (/initiate)'}; очередь реакций "
            f"{c.execute('select count(*) from reaction_todo').fetchone()[0]}; темп ×{self.tg.slow:g}, "
            f"FloodWait: {self.tg.floods}",
            "Задачи: " + (", ".join(t.get_name() for t in self.bg) or "нет"),
        ]
        await self.reply(update, "\n".join(lines))

    async def cmd_analyze(self, update: Update, ctx):
        if not self.is_admin(update):
            return
        if ctx.args and ctx.args[0].lower() in ("stop", "стоп"):
            tasks = [t for t in self.bg if t.get_name() in ("разбор", "ежедневный разбор", "описание медиа")]
            for t in tasks:
                t.cancel()
            self.cache.set("analysis_running", None)          # не продолжать сам после перезапуска
            await self.reply(update, "⏹ Разбор остановлен. Уже разобранное сохранено; продолжить — /analyze."
                             if tasks else "Разбор и так не идёт.")
            return
        if self.analyzer.running:
            await self.reply(update, "Разбор уже идёт — /status.")
            return
        self.cache.set("analysis_started", int(time.time()))
        self.spawn(self.analyzer.run("вручную"), "разбор")
        await self.reply(update, "🧠 Запустил разбор. Прогресс — /status и на карте.")

    async def cmd_describe(self, update: Update, ctx):
        """Nemotron описывает новые голосовые, кружки, видео и GIF сейчас, не дожидаясь ежедневного разбора."""
        if not self.is_admin(update):
            return
        if self.busy("описание медиа") or self.analyzer.running:
            await self.reply(update, "Уже идёт — /status.")
            return
        n = len(self.analyzer._undescribed())
        if not n:
            await self.reply(update, "Новых голосовых, кружков и видео без описания нет.")
            return

        async def run():
            done = await self.analyzer.describe_pending()
            self.analyzer.state = {"state": "idle"}
            await self.notify_admin(f"✅ Описано вложений: {done}")

        self.analyzer.state = {"state": "running", "started": int(time.time())}
        self.spawn(run(), "описание медиа")
        await self.reply(update, f"🎞 Nemotron описывает {n} вложений. Прогресс — /status.")

    # ------------------------------------------------------------ техобслуживание

    @property
    def maintenance(self) -> dict | None:
        return self.cache.get("maintenance") or None

    def guard(self, fn):
        """Во время техобслуживания команды и кнопки работают только у админа."""
        async def wrapped(update: Update, ctx):
            if self.maintenance and not self.is_admin(update):
                m = self.maintenance
                text = "🔧 Идёт техобслуживание" + (f": {m['reason']}" if m.get("reason") else "") + \
                       ". Попробуй позже — сообщения группы по-прежнему учитываются."
                if update.callback_query:
                    await update.callback_query.answer(text[:200], show_alert=True)
                elif update.effective_message:
                    await update.effective_message.reply_text(text)
                return
            return await fn(update, ctx)
        return wrapped

    async def announce(self, text: str):
        """Сообщение участникам — в группу."""
        if self.chat_id:
            try:
                await self.app.bot.send_message(self.chat_id, text)
            except TelegramError as exc:
                log.warning("в группу не отправилось: %s", exc)

    async def cmd_maintenance(self, update: Update, ctx):
        if not self.is_admin(update):
            return
        arg = (ctx.args[0].lower() if ctx.args else "")
        reason = " ".join(ctx.args[1:]).strip()
        m = self.maintenance
        if arg in ("on", "вкл"):
            if m:
                await self.reply(update, "Техобслуживание уже включено.")
                return
            self.cache.set("maintenance", {"since": int(time.time()), "reason": reason})
            stopped = [t.get_name() for t in list(self.bg)]
            for t in list(self.bg):
                t.cancel()
            for mgr in self.servers:
                if mgr.started_by_us:
                    await mgr.stop()
            await self.announce("🔧 Бот уходит на техобслуживание" + (f": {reason}" if reason else "") +
                                ". Команды временно не работают, сообщения группы по-прежнему учитываются.")
            await self.reply(update, "🔧 Техобслуживание включено." +
                             (f" Остановлено: {', '.join(stopped)}." if stopped else " Задач не было.") +
                             " Выключить — /maintenance off")
        elif arg in ("off", "выкл"):
            if not m:
                await self.reply(update, "Техобслуживание и так выключено.")
                return
            self.cache.set("maintenance", None)
            await self.announce("✅ Техобслуживание закончено, бот снова работает.")
            if self.tg_ok and self.initiated and not self.busy("синхронизация"):
                self.spawn(self.startup_sync(), "синхронизация")        # докачать пропущенное
            if self.cache.get("analysis_running") and not self.analyzer.running:
                self.spawn(self.analyzer.run("продолжение после техобслуживания"), "разбор")
            self.dirty = True
            await self.reply(update, "✅ Техобслуживание выключено, докачиваю пропущенное.")
        else:
            if m:
                since = datetime.fromtimestamp(m["since"], self.cfg.tz)
                await self.reply(update, f"🔧 Техобслуживание включено с {since:%d.%m %H:%M}" +
                                 (f": {esc(m['reason'])}" if m.get("reason") else "") + ". Выключить — /maintenance off")
            else:
                await self.reply(update, "Техобслуживание выключено. Включить — /maintenance on [причина]")

    async def cmd_initiate(self, update: Update, ctx):
        if not self.is_admin(update):
            return
        if not self.tg_ok:
            await self.reply(update, "Telethon не подключён — смотри лог: journalctl --user -u levbush")
            return
        if self.initiated:
            await self.reply(update, "Сбор уже запущен. Прогресс — /status.")
            return
        self.cache.set("initiated", int(time.time()))
        self.tg.start_live(on_change=self.mark_dirty)
        self.spawn(self.startup_sync(), "синхронизация")
        d = self.cfg
        await self.reply(update, "📥 Запускаю сбор: участники → вся история (пауза "
                                 f"{d.tg_history_delay:g} с на 100 сообщений) → реакции поимённо ({d.tg_reaction_delay:g} с "
                                 f"на сообщение) → профили → медиа. При FloodWait жду и замедляюсь. Прогресс — /status.")

    async def cmd_sync(self, update: Update, ctx):
        if not self.is_admin(update):
            return
        if not self.tg_ok or not self.initiated:
            await self.reply(update, "Сбор не запущен — /initiate.")
            return
        if self.busy("синхронизация"):
            await self.reply(update, "Уже синхронизируюсь.")
            return

        async def run():
            n = await self.tg.sync_history()
            await self.tg.sync_participants()
            self.dirty = True
            await self.push_stats(force=True)
            await self.notify_admin(f"✅ Докачано сообщений: {n}")

        self.spawn(run(), "синхронизация")
        await self.reply(update, "📥 Докачиваю.")

    # ------------------------------------------------------------ кнопки

    async def on_button(self, update: Update, ctx):
        q = update.callback_query
        if not await self.allowed(update):
            await q.answer("Только для участников группы", show_alert=True)
            return
        data = q.data or ""
        kind, _, rest = data.partition(":")
        try:
            if kind == "st":
                uid, period = rest.split(":")
                p = await self.db.call("api_person", int(uid))
                await q.edit_message_text(self.card(p, period), parse_mode=ParseMode.HTML,
                                          reply_markup=self.card_kb(update, int(uid), period),
                                          disable_web_page_preview=True)
            elif kind == "tp":
                metric, period = rest.split(":")
                await q.edit_message_text(await self.top_text(metric, period), parse_mode=ParseMode.HTML,
                                          reply_markup=self.top_kb(metric, period))
            elif kind == "ds":
                md = await self.dossier_md(int(rest))
                if md is not None:
                    await self.send_rich(q.message, md, await self.dossier_text(int(rest)))
            elif kind == "ln":
                await q.message.reply_text(await self.links_text(int(rest)), parse_mode=ParseMode.HTML)
            elif kind == "an" and self.is_admin(update):
                if rest == "run" and not self.analyzer.running:
                    self.cache.set("analysis_started", int(time.time()))
                    self.spawn(self.analyzer.run("первый прогон"), "разбор")
                    await q.edit_message_text(q.message.text + "\n\n🧠 Разбор запущен. Прогресс — /status.")
                else:
                    await q.edit_message_text(q.message.text + "\n\nОк, запустишь потом командой /analyze.")
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        await q.answer()


def build(config: Config = cfg) -> Application:
    if not config.bot_token:
        raise SystemExit("нет LEVBUSH_BOT_TOKEN в ~/.config/levbush.env")
    lb = Levbush(config)
    app = (Application.builder().token(config.bot_token).rate_limiter(AIORateLimiter(max_retries=3))
           .post_init(lb.post_init).post_stop(lb.post_stop).post_shutdown(lb.post_shutdown).concurrent_updates(True).build())
    app.bot_data["levbush"] = lb
    cmds = {"help": lb.cmd_help, "start": lb.cmd_help, "stats": lb.cmd_stats, "me": lb.cmd_me, "top": lb.cmd_top,
            "pair": lb.cmd_pair, "dossier": lb.cmd_dossier, "links": lb.cmd_links, "map": lb.cmd_map,
            "retell": lb.cmd_retell, "status": lb.cmd_status, "analyze": lb.cmd_analyze, "sync": lb.cmd_sync,
            "initiate": lb.cmd_initiate, "text": lb.cmd_text, "describe": lb.cmd_describe}
    cmds["maintenance"] = lb.cmd_maintenance
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, lb.guard(fn)), group=1)
    # сбор — отдельной группой обработчиков, чтобы команды в группе тоже попадали в кэш
    app.add_handler(MessageHandler(filters.ALL, lb.on_message), group=0)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VOICE | filters.VIDEO_NOTE | filters.AUDIO),
                                   lb.on_private_speech), group=2)
    app.add_handler(MessageReactionHandler(lb.on_reaction, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED), group=0)
    app.add_handler(MessageReactionHandler(lb.on_reaction_count, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_COUNT_UPDATED), group=0)
    app.add_handler(ChatMemberHandler(lb.on_member, ChatMemberHandler.CHAT_MEMBER), group=0)
    app.add_handler(CallbackQueryHandler(lb.guard(lb.on_button)), group=1)
    return app


def main():
    app = build()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, bootstrap_retries=-1)
