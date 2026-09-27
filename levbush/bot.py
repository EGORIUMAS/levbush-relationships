"""Levbush Relationships — бот: статистика, досье, связи, пересказ, карта.

Один процесс: PTB (команды) + Telethon (история и живой сбор: сообщения, правки, удаления, реакции поимённо,
входы/выходы, медиа, профили) + задания (статистика, ежедневный разбор).
Боту права админа не нужны. Если он всё же админ с выключенным privacy mode, его обновления тоже пишутся в кэш
(дубли безвредны) — это страховка на случай, когда Telethon отвалился.
"""
import asyncio
import html
import logging
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
from .gpu import LLMManager
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


def esc(x) -> str:
    return html.escape(str(x), quote=False)


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
        self.mgr = LLMManager(config, notify=self.notify_admin)
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
        if self.app and self.cfg.admin_id:
            try:
                await self.app.bot.send_message(self.cfg.admin_id, text, disable_web_page_preview=True)
            except TelegramError as exc:
                log.warning("админу не отправилось: %s", exc)

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
        self.analyzer = Analyzer(self.cfg, self.cache, self.db, self.mgr, notify=self.notify_admin)
        self.retell = Retell(self.analyzer)
        await app.bot.set_my_commands([
            BotCommand("stats", "статистика: /stats [@ник] или ответом"),
            BotCommand("me", "моя статистика"),
            BotCommand("top", "топ участников: /top [метрика] [d|w|m|a]"),
            BotCommand("pair", "пара: /pair @a [@b] или ответом"),
            BotCommand("dossier", "досье: /dossier [@ник] или ответом"),
            BotCommand("links", "связи человека"),
            BotCommand("retell", "пересказ: /retell 2ч | 14:30 | вчера 20:00 или ответом"),
            BotCommand("map", "карта связей"),
            BotCommand("help", "справка"),
        ], scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands([
            BotCommand("retell", "пересказ: /retell 2ч или ответом на сообщение"),
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
            self.spawn(self.startup_sync(), "синхронизация")
        elif self.tg_ok:
            await self.notify_admin("✅ Бот запущен, Telethon подключён к «" + str(self.chat.get("title")) + "».\n"
                                    "Сбор ещё не запускался — /initiate, когда будешь готов.")

    async def post_shutdown(self, app: Application):
        for t in list(self.bg):
            t.cancel()
        if self.mgr.started_by_us:
            await self.mgr.stop()
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
        try:
            await self.push_stats()
        except Exception:  # noqa: BLE001
            log.exception("статистика")
            self.dirty = True

    def mark_dirty(self):
        self.dirty = True

    async def job_reactions(self, ctx):
        if self.tg_ok and self.initiated and not self.busy("синхронизация"):
            try:
                await self.tg.flush_reactions()
            except Exception:  # noqa: BLE001
                log.exception("реакции")

    async def job_media(self, ctx):
        if self.tg_ok and self.initiated and not self.busy("синхронизация"):
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
        if self.tg_ok and self.initiated and not self.busy("синхронизация"):
            try:
                await self.tg.sync_participants()
                await self.refresh_names()
                self.dirty = True
            except Exception:  # noqa: BLE001
                log.exception("участники")

    async def job_daily(self, ctx):
        if self.initiated and not self.busy("ежедневный разбор"):
            self.spawn(self.daily(), "ежедневный разбор")

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
        await self.analyzer.run("daily")

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
            "или ответом на сообщение — с него\n/map — карта связей")
        if self.is_admin(update):
            text += "\n\nАдмин: /initiate (запуск сбора), /status, /analyze, /sync"
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
        await self.reply(update, await self.dossier_text(uid), InlineKeyboardMarkup([[btn]]) if btn else None)

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
                if not await self.mgr.is_up():
                    await status.edit_text("⏳ Поднимаю Nemotron (~2 мин), потом перескажу…")
                text = await self.retell.run(since_ts)
            except Exception as exc:  # noqa: BLE001
                log.exception("пересказ")
                await status.edit_text(f"❌ Не получилось: {esc(exc)}", parse_mode=ParseMode.HTML)
                return
            chunks = split_html(text)
            try:
                await status.edit_text(chunks[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except BadRequest:
                await status.edit_text(chunks[0][:4000])
            for chunk in chunks[1:]:
                await status.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

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
            f"Nemotron: {'работает' if await self.mgr.is_up() else 'не запущен'}",
            f"Разбор: {esc(st.get('state', '—'))} {esc(st.get('stage', ''))} "
            + (f"{st.get('done')}/{st.get('total')}" if st.get("total") else ""),
            f"Сбор: {'запущен' if self.initiated else 'не запускался (/initiate)'}; очередь реакций "
            f"{c.execute('select count(*) from reaction_todo').fetchone()[0]}; темп ×{self.tg.slow:g}, "
            f"FloodWait: {self.tg.floods}",
            "Задачи: " + (", ".join(t.get_name() for t in self.bg) or "нет"),
        ]
        await self.reply(update, "\n".join(lines))

    async def cmd_analyze(self, update: Update, ctx):
        if not self.is_admin(update):
            return
        if self.analyzer.running:
            await self.reply(update, "Разбор уже идёт — /status.")
            return
        self.spawn(self.analyzer.run("вручную"), "разбор")
        await self.reply(update, "🧠 Запустил разбор. Прогресс — /status и на карте.")

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
                for chunk in split_html(await self.dossier_text(int(rest))):
                    await q.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            elif kind == "ln":
                await q.message.reply_text(await self.links_text(int(rest)), parse_mode=ParseMode.HTML)
            elif kind == "an" and self.is_admin(update):
                if rest == "run" and not self.analyzer.running:
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
           .post_init(lb.post_init).post_shutdown(lb.post_shutdown).concurrent_updates(True).build())
    app.bot_data["levbush"] = lb
    cmds = {"help": lb.cmd_help, "start": lb.cmd_help, "stats": lb.cmd_stats, "me": lb.cmd_me, "top": lb.cmd_top,
            "pair": lb.cmd_pair, "dossier": lb.cmd_dossier, "links": lb.cmd_links, "map": lb.cmd_map,
            "retell": lb.cmd_retell, "status": lb.cmd_status, "analyze": lb.cmd_analyze, "sync": lb.cmd_sync,
            "initiate": lb.cmd_initiate}
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn), group=1)
    # сбор — отдельной группой обработчиков, чтобы команды в группе тоже попадали в кэш
    app.add_handler(MessageHandler(filters.ALL, lb.on_message), group=0)
    app.add_handler(MessageReactionHandler(lb.on_reaction, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED), group=0)
    app.add_handler(MessageReactionHandler(lb.on_reaction_count, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_COUNT_UPDATED), group=0)
    app.add_handler(ChatMemberHandler(lb.on_member, ChatMemberHandler.CHAT_MEMBER), group=0)
    app.add_handler(CallbackQueryHandler(lb.on_button), group=1)
    return app


def main():
    app = build()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, bootstrap_retries=-1)
