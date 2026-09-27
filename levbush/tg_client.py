"""Telethon (аккаунт пользователя): история группы, реакции поимённо, участники, входы/выходы, аватары, медиа.

Сессией владеет один процесс (файловая блокировка): обычно это `levbush run`; CLI-команды `sync` и т.п.
работают, только когда бот остановлен.
"""
import asyncio
import base64
import fcntl
import io
import logging
import os
import time
from pathlib import Path

from telethon import TelegramClient, errors, functions
from telethon import types as tt
from telethon import utils as tu

from .cache import Cache
from .config import Config
from .normalize import from_telethon, reaction_key, tt_reactions, tt_user_row

log = logging.getLogger("levbush.tg")

MEDIA_EXT = {"photo": ".jpg", "voice": ".ogg", "video_note": ".mp4", "gif": ".mp4", "video": ".mp4"}


class SessionBusy(RuntimeError):
    pass


def avatar_data_uri(raw: bytes, size: int = 96) -> str | None:
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:  # noqa: BLE001 — битая картинка не должна ронять синхронизацию
        return None
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


class TG:
    def __init__(self, cfg: Config, cache: Cache):
        self.cfg, self.cache = cfg, cache
        self.client: TelegramClient | None = None
        self.chat = None
        self.chat_id: int | None = None
        self.channel_id: int | None = None
        self._lock_fd = None
        self.io_lock = asyncio.Lock()   # одна тяжёлая операция с API за раз

    # ------------------------------------------------------------ подключение

    def _take_lock(self):
        path = self.cfg.data_dir / "telethon.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SessionBusy("сессией Telethon уже пользуется другой процесс (бот запущен?)")
        self._lock_fd = fd

    async def connect(self, interactive: bool = False):
        if not self.cfg.api_id or not self.cfg.api_hash:
            raise RuntimeError("нет LEVBUSH_API_ID / LEVBUSH_API_HASH (my.telegram.org → API development tools)")
        self.cfg.ensure_dirs()
        self._take_lock()
        self.client = TelegramClient(str(self.cfg.session_file), self.cfg.api_id, self.cfg.api_hash,
                                     flood_sleep_threshold=120, device_model="Levbush Relationships",
                                     system_version="Linux", app_version="1.0")
        if interactive:
            await self.client.start()
        else:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError("Telethon не авторизован: запусти `levbush login`")
        for suffix in ("", ".session"):
            p = Path(str(self.cfg.session_file) + suffix)
            if p.exists():
                os.chmod(p, 0o600)
        await self.resolve_group()

    async def close(self):
        if self.client:
            await self.client.disconnect()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def _find_group(self, target: str):
        """@username / ссылка / id в любом виде: -100…, -…, голый id (разные клиенты показывают по-разному)."""
        if not target.lstrip("-").isdigit():
            return await self.client.get_entity(target)
        raw = target.lstrip("-")
        bare = int(raw[3:]) if target.startswith("-100") and len(raw) > 10 else int(raw)
        async for d in self.client.iter_dialogs():
            if getattr(d.entity, "id", None) == bare and not isinstance(d.entity, tt.User):
                return d.entity
        for guess in (int(f"-100{bare}"), -bare):
            try:
                return await self.client.get_entity(guess)
            except (ValueError, errors.RPCError):
                continue
        raise RuntimeError(f"группа {target} не найдена среди чатов аккаунта — проверь id или укажи @username/ссылку")

    async def resolve_group(self):
        if not self.cfg.group:
            raise RuntimeError("не задана группа: LEVBUSH_GROUP (@username, id или ссылка)")
        entity = await self._find_group(self.cfg.group.strip())
        channel = None
        if isinstance(entity, tt.Channel):
            full = await self.client(functions.channels.GetFullChannelRequest(entity))
            linked = full.full_chat.linked_chat_id
            if entity.broadcast:
                if not linked:
                    raise RuntimeError(f"у канала «{entity.title}» нет группы обсуждения")
                channel = entity
                entity = await self.client.get_entity(tt.PeerChannel(linked))
            elif linked:
                channel = await self.client.get_entity(tt.PeerChannel(linked))
        self.chat = entity
        self.chat_id = tu.get_peer_id(entity)
        self.channel_id = tu.get_peer_id(channel) if channel else None
        info = {"id": self.chat_id, "title": getattr(entity, "title", None), "username": getattr(entity, "username", None),
                "channel_id": self.channel_id, "channel_title": channel.title if channel else None}
        self.cache.set("chat", info)
        for ent in (entity, channel):
            if ent is not None:
                self.cache.upsert_user(tt_user_row(ent))
        return info

    # ------------------------------------------------------------ живой сбор (без прав админа у бота)

    def start_live(self, on_change=None):
        """Новые сообщения, правки, удаления, входы/выходы и реакции — прямо через аккаунт пользователя.
        on_change() вызывается после каждой записи в кэш (бот помечает статистику устаревшей)."""
        from telethon import events
        c = self.client
        self._reaction_queue: set[int] = set()

        def changed():
            if on_change:
                on_change()

        @c.on(events.NewMessage(chats=self.chat))
        @c.on(events.MessageEdited(chats=self.chat))
        async def _msg(ev):
            need = self._store_batch([ev.message])
            self._reaction_queue.update(need)
            changed()

        @c.on(events.ChatAction(chats=self.chat))
        async def _action(ev):
            if ev.action_message is not None:
                self._store_batch([ev.action_message])
            else:
                now = int(time.time())
                for uid in ev.user_ids or []:
                    if ev.user_joined or ev.user_added:
                        self.cache.add_membership(uid, now, "join", "live")
                        self.cache.upsert_user({"id": uid, "is_member": 1})
                    elif ev.user_left or ev.user_kicked:
                        self.cache.add_membership(uid, now, "leave", "live")
                        self.cache.upsert_user({"id": uid, "is_member": 0})
            changed()

        @c.on(events.MessageDeleted(chats=self.chat))
        async def _deleted(ev):
            self.cache.db.executemany("update messages set deleted = 1 where id = ?", [(i,) for i in ev.deleted_ids])
            changed()

        @c.on(events.Raw(tt.UpdateMessageReactions))
        async def _reactions(update):
            if tu.get_peer_id(update.peer) != self.chat_id or self.cache.message(update.msg_id) is None:
                return
            res = update.reactions
            counts = {k: rc.count for rc in res.results or [] if (k := reaction_key(rc.reaction))}
            recent = [(tu.get_peer_id(r.peer_id), reaction_key(r.reaction), int(r.date.timestamp()) if r.date else None)
                      for r in res.recent_reactions or []]
            with self.cache.tx() as db:
                self.cache.set_reaction_counts(update.msg_id, counts, db)
                if len(recent) >= sum(counts.values()) or not res.can_see_list:
                    self.cache.set_reactions(update.msg_id, [r for r in recent if r[0] and r[1]], db)
                else:
                    self._reaction_queue.add(update.msg_id)
            changed()

    async def flush_reactions(self):
        """Догружает поимённые списки реакций, накопленные живым сбором (вызывается раз в минуту)."""
        ids = sorted(getattr(self, "_reaction_queue", ()))
        if not ids:
            return 0
        self._reaction_queue.clear()
        async with self.io_lock:
            await self._fetch_reaction_lists(ids)
        return len(ids)

    # ------------------------------------------------------------ история

    async def sync_history(self, progress=None, full_reactions: bool = True) -> int:
        """Докачивает всё после курсора sync_upto (первый раз — всю историю). Возвращает число сообщений."""
        async with self.io_lock:
            upto = self.cache.get("sync_upto", 0)
            n = 0
            batch = []
            reaction_queue = []
            last_report = time.monotonic()
            total = None
            if progress:
                try:
                    total = (await self.client.get_messages(self.chat, limit=0)).total
                except Exception:  # noqa: BLE001
                    total = None
            async for msg in self.client.iter_messages(self.chat, reverse=True, min_id=upto, wait_time=0):
                batch.append(msg)
                if len(batch) >= 200:
                    reaction_queue += self._store_batch(batch)
                    upto = batch[-1].id
                    self.cache.set("sync_upto", upto)
                    n += len(batch)
                    batch = []
                    if progress and time.monotonic() - last_report > 5:
                        last_report = time.monotonic()
                        await progress(n, total)
            if batch:
                reaction_queue += self._store_batch(batch)
                upto = batch[-1].id
                self.cache.set("sync_upto", upto)
                n += len(batch)
            if full_reactions and reaction_queue:
                await self._fetch_reaction_lists(reaction_queue, progress)
            self.cache.set("history_synced", True)
            self.cache.set("last_sync", int(time.time()))
            return n

    def _store_batch(self, batch) -> list[int]:
        """Пишет пачку сообщений в кэш; возвращает id, у которых список реакций надо догрузить."""
        need = []
        with self.cache.tx() as db:
            for msg in batch:
                row = from_telethon(msg, self.chat_id, self.channel_id)
                self.cache.upsert_message(row, db)
                sender = getattr(msg, "sender", None)
                if sender is not None:
                    self.cache.upsert_user(tt_user_row(sender), db)
                if isinstance(msg, tt.MessageService):
                    self._membership_from_service(msg, row, db)
                    continue
                if getattr(msg, "fwd_from", None) is not None and getattr(msg, "forward", None) is not None:
                    fwd_sender = getattr(msg.forward, "sender", None) or getattr(msg.forward, "chat", None)
                    if fwd_sender is not None:
                        self.cache.upsert_user(tt_user_row(fwd_sender), db)
                counts, can_list, recent = tt_reactions(msg)
                total = sum(counts.values())
                if total:
                    self.cache.set_reaction_counts(msg.id, counts, db)
                    if len(recent) >= total:
                        self.cache.set_reactions(msg.id, [r for r in recent if r[0] and r[1]], db)
                    elif can_list:
                        need.append(msg.id)
                    else:
                        self.cache.set_reactions(msg.id, [r for r in recent if r[0] and r[1]], db)
                else:
                    db.execute("delete from reaction_counts where msg_id = ?", (msg.id,))
                    db.execute("delete from reactions where msg_id = ?", (msg.id,))
        return need

    def _membership_from_service(self, msg, row, db):
        name = row.get("service")
        data = row.get("service_data") or {}
        date = row["date"]
        if name == "ChatAddUser":
            for uid in data.get("users", []):
                self.cache.add_membership(uid, date, "join", "service", db)
        elif name in ("ChatJoinedByLink", "ChatJoinedByRequest"):
            self.cache.add_membership(row["sender_id"], date, "join", "service", db)
        elif name == "ChatDeleteUser":
            for uid in data.get("users", []):
                self.cache.add_membership(uid, date, "leave", "service", db)

    async def _fetch_reaction_lists(self, ids, progress=None):
        """Поимённый список реакций (messages.getMessageReactionsList) для сообщений, где он не весь в recent."""
        done = 0
        for msg_id in ids:
            pairs = []
            offset = None
            try:
                while True:
                    res = await self.client(functions.messages.GetMessageReactionsListRequest(
                        peer=self.chat, id=msg_id, limit=100, offset=offset))
                    for r in res.reactions:
                        key = reaction_key(r.reaction)
                        if key:
                            pairs.append((tu.get_peer_id(r.peer_id), key, int(r.date.timestamp()) if r.date else None))
                    for u in res.users:
                        self.cache.upsert_user(tt_user_row(u))
                    offset = res.next_offset
                    if not offset:
                        break
            except errors.FloodWaitError as exc:
                log.warning("FloodWait %s с на списке реакций", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                continue
            except errors.RPCError as exc:
                log.info("реакции #%s недоступны: %s", msg_id, exc)
                continue
            with self.cache.tx() as db:
                self.cache.set_reactions(msg_id, pairs, db)
            done += 1
            if progress and done % 200 == 0:
                await progress(done, len(ids), "реакции")

    async def refresh_reactions(self, since_ts: int):
        """Перечитывает реакции на сообщения за последние дни (бот ловит новые, но не всё и не всегда)."""
        async with self.io_lock:
            ids = [r[0] for r in self.cache.db.execute(
                "select id from messages where date >= ? and service is null", (since_ts,))]
            for i in range(0, len(ids), 100):
                msgs = await self.client.get_messages(self.chat, ids=ids[i:i + 100])
                need = self._store_batch([m for m in msgs if m is not None])
                if need:
                    await self._fetch_reaction_lists(need)

    # ------------------------------------------------------------ участники

    async def sync_participants(self):
        async with self.io_lock:
            now = int(time.time())
            present = set()
            users = [u async for u in self.client.iter_participants(self.chat)]   # до транзакции: сеть не держит блокировку
            with self.cache.tx() as db:
                for user in users:
                    row = tt_user_row(user)
                    part = user.participant
                    since = getattr(part, "date", None)
                    row["is_member"] = 1
                    if since:
                        row["member_since"] = int(since.timestamp())
                        self.cache.add_membership(user.id, row["member_since"], "join", "participant", db)
                    self.cache.upsert_user(row, db)
                    present.add(user.id)
                for (uid,) in db.execute("select id from users where is_member = 1 and kind = 'user'").fetchall():
                    if uid not in present:
                        db.execute("update users set is_member = 0 where id = ?", (uid,))
                        last = db.execute("select event from membership where user_id = ? order by date desc limit 1",
                                          (uid,)).fetchone()
                        if not last or last[0] != "leave":
                            self.cache.add_membership(uid, now, "leave", "participants_diff", db)
                db.execute("update users set is_member = 0 where is_member is null and kind = 'user'")
            try:
                async for ev in self.client.iter_admin_log(self.chat, join=True, leave=True, invite=True):
                    date = int(ev.date.timestamp())
                    act = ev.action
                    if isinstance(act, (tt.ChannelAdminLogEventActionParticipantJoin,
                                        tt.ChannelAdminLogEventActionParticipantJoinByInvite,
                                        tt.ChannelAdminLogEventActionParticipantJoinByRequest)):
                        self.cache.add_membership(ev.user_id, date, "join", "admin_log")
                    elif isinstance(act, tt.ChannelAdminLogEventActionParticipantLeave):
                        self.cache.add_membership(ev.user_id, date, "leave", "admin_log")
                    elif isinstance(act, tt.ChannelAdminLogEventActionParticipantInvite):
                        uid = getattr(act.participant, "user_id", None)
                        if uid:
                            self.cache.add_membership(uid, date, "join", "admin_log")
            except errors.RPCError as exc:
                log.info("журнал действий недоступен: %s", exc)
            self.cache.set("participants_synced", now)
            return len(present)

    async def sync_profiles(self, max_age_days: int = 7):
        """Био и аватарки: участники и все, кто писал. Раз в max_age_days на человека."""
        async with self.io_lock:
            limit = int(time.time()) - max_age_days * 86400
            rows = self.cache.db.execute(
                "select u.id, u.photo_id, u.kind from users u where (u.is_member = 1 or exists "
                "(select 1 from messages m where m.sender_id = u.id)) and coalesce(cast(json_extract("
                "(select value from kv where key = 'profile:' || u.id), '$') as integer), 0) < ?", (limit,)).fetchall()
            for uid, photo_id, kind in rows:
                try:
                    entity = await self.client.get_entity(uid)
                    row = tt_user_row(entity)
                    if kind == "user" and isinstance(entity, tt.User):
                        full = await self.client(functions.users.GetFullUserRequest(entity))
                        row["bio"] = full.full_user.about
                    if entity.photo and not isinstance(entity.photo, (tt.UserProfilePhotoEmpty, tt.ChatPhotoEmpty)):
                        raw = await self.client.download_profile_photo(entity, file=bytes, download_big=False)
                        if raw:
                            row["avatar"] = avatar_data_uri(raw)
                    self.cache.upsert_user(row)
                except errors.FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds + 1)
                except (errors.RPCError, ValueError) as exc:
                    log.info("профиль %s: %s", uid, exc)
                self.cache.set(f"profile:{uid}", int(time.time()))
                await asyncio.sleep(0.3)

    # ------------------------------------------------------------ медиа

    def media_path(self, msg_id: int, date: int, kind: str, meta: dict) -> Path:
        month = time.strftime("%Y-%m", time.gmtime(date))
        ext = MEDIA_EXT.get(kind)
        if ext is None:
            name = (meta or {}).get("file_name") or ""
            ext = Path(name).suffix.lower()[:10] if "." in name else ""
            if not ext:
                mime = (meta or {}).get("mime") or ""
                ext = {"image/webp": ".webp", "application/x-tgsticker": ".tgs", "video/webm": ".webm",
                       "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "application/pdf": ".pdf"}.get(mime, ".bin")
        return self.cfg.media_dir / month / f"{msg_id}{ext}"

    async def download_pending(self, limit: int = 500, progress=None) -> int:
        """Скачивает медиа со state=pending (всё, что больше MEDIA_MAX_MB, помечается skip)."""
        import json
        rows = self.cache.db.execute(
            "select id, date, media, media_meta from messages where media_state = 'pending' order by id desc limit ?",
            (limit,)).fetchall()
        if not rows:
            return 0
        max_bytes = self.cfg.media_max_mb * 1024 * 1024
        done = 0
        async with self.io_lock:
            for i in range(0, len(rows), 50):
                chunk = rows[i:i + 50]
                msgs = await self.client.get_messages(self.chat, ids=[r["id"] for r in chunk])
                for r, msg in zip(chunk, msgs):
                    meta = json.loads(r["media_meta"]) if r["media_meta"] else {}
                    if msg is None or msg.media is None:
                        self.cache.set_media(r["id"], "error")
                        continue
                    size = getattr(msg.file, "size", None) or meta.get("size") or 0
                    if size > max_bytes:
                        self.cache.set_media(r["id"], "skip")
                        continue
                    path = self.media_path(r["id"], r["date"], r["media"], meta)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        await self.client.download_media(msg, file=str(path))
                        self.cache.set_media(r["id"], "ok", str(path))
                        done += 1
                    except errors.FloodWaitError as exc:
                        await asyncio.sleep(exc.seconds + 1)
                    except Exception as exc:  # noqa: BLE001 — один битый файл не останавливает загрузку
                        log.warning("медиа #%s: %s", r["id"], exc)
                        self.cache.set_media(r["id"], "error")
                if progress:
                    await progress(done, len(rows), "медиа")
        return done
