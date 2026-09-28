"""Приведение сообщений Telethon (MTProto) и python-telegram-bot (Bot API) к одной строке кэша."""
from telethon import types as tt
from telethon import utils as tu

# типы медиа, которые считаются «медиа» в статистике (кружки и голосовые — отдельно)
MEDIA_KINDS = {"photo", "video", "document", "audio", "gif"}


def u16_slice(text: str, offset: int, length: int) -> str:
    """Смещения сущностей Telegram — в UTF-16 code units."""
    data = (text or "").encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def reaction_key(reaction) -> str | None:
    """Реакция (Telethon или PTB) → строка: эмодзи, custom:<id> или paid."""
    if reaction is None:
        return None
    # Telethon
    if isinstance(reaction, tt.ReactionEmoji):
        return reaction.emoticon
    if isinstance(reaction, tt.ReactionCustomEmoji):
        return f"custom:{reaction.document_id}"
    if isinstance(reaction, tt.ReactionPaid):
        return "paid"
    # PTB
    kind = getattr(reaction, "type", None)
    if kind == "emoji":
        return reaction.emoji
    if kind == "custom_emoji":
        return f"custom:{reaction.custom_emoji_id}"
    if kind == "paid":
        return "paid"
    return None


# ================================================================ Telethon

_TT_ENTITY = {
    tt.MessageEntityMention: "mention", tt.MessageEntityMentionName: "text_mention",
    tt.InputMessageEntityMentionName: "text_mention", tt.MessageEntityUrl: "url",
    tt.MessageEntityTextUrl: "text_link", tt.MessageEntityHashtag: "hashtag", tt.MessageEntityBotCommand: "bot_command",
    tt.MessageEntityEmail: "email", tt.MessageEntityPhone: "phone", tt.MessageEntityBold: "bold",
    tt.MessageEntityItalic: "italic", tt.MessageEntityCode: "code", tt.MessageEntityPre: "pre",
    tt.MessageEntitySpoiler: "spoiler", tt.MessageEntityCustomEmoji: "custom_emoji",
    tt.MessageEntityBlockquote: "blockquote", tt.MessageEntityStrike: "strikethrough",
    tt.MessageEntityUnderline: "underline", tt.MessageEntityCashtag: "cashtag",
}


def _tt_entities(entities):
    out = []
    for e in entities or []:
        item = {"t": _TT_ENTITY.get(type(e), type(e).__name__), "o": e.offset, "l": e.length}
        if isinstance(e, tt.MessageEntityMentionName):
            item["u"] = e.user_id
        elif isinstance(e, tt.InputMessageEntityMentionName):
            item["u"] = getattr(e.user_id, "user_id", None)
        elif isinstance(e, tt.MessageEntityTextUrl):
            item["url"] = e.url
        elif isinstance(e, tt.MessageEntityCustomEmoji):
            item["id"] = e.document_id
        out.append(item)
    return out or None


def _peer(peer):
    return tu.get_peer_id(peer) if peer is not None else None


def _doc_attrs(doc):
    meta = {"mime": doc.mime_type, "size": doc.size}
    for a in doc.attributes or []:
        if isinstance(a, tt.DocumentAttributeFilename):
            meta["file_name"] = a.file_name
        elif isinstance(a, tt.DocumentAttributeVideo):
            meta.update(duration=a.duration, w=a.w, h=a.h)
        elif isinstance(a, tt.DocumentAttributeAudio):
            meta.update(duration=a.duration, title=a.title, performer=a.performer)
        elif isinstance(a, tt.DocumentAttributeSticker):
            meta["emoji"] = a.alt
        elif isinstance(a, tt.DocumentAttributeImageSize):
            meta.update(w=a.w, h=a.h)
    return {k: v for k, v in meta.items() if v not in (None, "")}


def tt_media(msg):
    """(вид, метаданные) медиа сообщения Telethon или (None, None)."""
    media = msg.media
    if media is None:
        return None, None
    if msg.sticker:
        return "sticker", _doc_attrs(msg.sticker)
    if msg.video_note:
        return "video_note", _doc_attrs(msg.video_note)
    if msg.voice:
        return "voice", _doc_attrs(msg.voice)
    if msg.gif:
        return "gif", _doc_attrs(msg.gif)
    if msg.video:
        return "video", _doc_attrs(msg.video)
    if msg.audio:
        return "audio", _doc_attrs(msg.audio)
    if msg.photo:
        sizes = [s for s in (msg.photo.sizes or []) if hasattr(s, "w")]
        big = max(sizes, key=lambda s: s.w * s.h) if sizes else None
        meta = {"w": big.w, "h": big.h} if big else {}
        return "photo", meta
    if msg.document:
        return "document", _doc_attrs(msg.document)
    if isinstance(media, tt.MessageMediaPoll):
        poll = media.poll
        question = getattr(poll.question, "text", poll.question)
        options = [getattr(a.text, "text", a.text) for a in poll.answers]
        return "poll", {"question": question, "options": options, "quiz": bool(poll.quiz)}
    if isinstance(media, (tt.MessageMediaGeo, tt.MessageMediaGeoLive, tt.MessageMediaVenue)):
        geo = media.geo
        meta = {"lat": getattr(geo, "lat", None), "lon": getattr(geo, "long", None)}
        if isinstance(media, tt.MessageMediaVenue):
            meta.update(title=media.title, address=media.address)
        return "location", meta
    if isinstance(media, tt.MessageMediaContact):
        return "contact", {"name": f"{media.first_name} {media.last_name}".strip()}
    if isinstance(media, tt.MessageMediaDice):
        return "dice", {"emoji": media.emoticon, "value": media.value}
    if isinstance(media, tt.MessageMediaWebPage):
        page = media.webpage
        return "webpage", {"url": getattr(page, "url", None), "title": getattr(page, "title", None),
                           "site": getattr(page, "site_name", None),
                           "description": (getattr(page, "description", None) or "")[:500] or None}
    return "other", {"type": type(media).__name__}


def _tt_service(msg):
    action = msg.action
    name = type(action).__name__.removeprefix("MessageAction")
    data = {}
    if isinstance(action, tt.MessageActionChatAddUser):
        data["users"] = list(action.users)
    elif isinstance(action, tt.MessageActionChatDeleteUser):
        data["users"] = [action.user_id]
    elif isinstance(action, tt.MessageActionChatJoinedByLink):
        data["inviter"] = action.inviter_id
    elif isinstance(action, tt.MessageActionPinMessage):
        data["pinned"] = msg.reply_to.reply_to_msg_id if msg.reply_to else None
    elif hasattr(action, "title"):
        data["title"] = action.title
    return name, data


def from_telethon(msg, chat_id: int, channel_id: int | None) -> dict:
    """Сообщение Telethon → строка кэша (без реакций)."""
    row = {"id": msg.id, "date": int(msg.date.timestamp()), "source": "mtproto"}
    sender = _peer(msg.from_id) if msg.from_id else chat_id   # анонимный админ пишет «от группы»
    row["sender_id"] = sender
    if isinstance(msg, tt.MessageService):
        name, data = _tt_service(msg)
        row["service"], row["service_data"] = name, data
        return row
    row["edit_date"] = int(msg.edit_date.timestamp()) if msg.edit_date and not msg.edit_hide else None
    row["text"] = msg.message or None
    row["entities"] = _tt_entities(msg.entities)
    row["grouped_id"] = msg.grouped_id
    row["via_bot"] = msg.via_bot_id
    r = msg.reply_to
    if isinstance(r, tt.MessageReplyHeader):
        if r.forum_topic and r.reply_to_top_id is None:
            row["top_id"] = r.reply_to_msg_id          # просто сообщение в теме форума, не ответ
        else:
            row["reply_to"] = r.reply_to_msg_id
            row["top_id"] = r.reply_to_top_id
            row["reply_peer"] = _peer(r.reply_to_peer_id) if r.reply_to_peer_id else None
            if r.quote and r.quote_text:
                row["quote"] = r.quote_text
    f = msg.fwd_from
    if f is not None:
        row["fwd_from_id"] = _peer(f.from_id) if f.from_id else None
        row["fwd_from_name"] = f.from_name or f.post_author
        row["fwd_date"] = int(f.date.timestamp()) if f.date else None
        if channel_id and sender == channel_id and f.saved_from_peer is not None:
            row["auto_fwd"] = 1
    kind, meta = tt_media(msg)
    if kind:
        row["media"], row["media_meta"] = kind, meta
        row["media_state"] = "pending" if wanted(kind, meta) else None
    return row


# медиа, которые скачиваем (для нейросети и расшифровки)
DOWNLOADABLE = {"photo", "video", "video_note", "voice", "audio", "gif", "document", "sticker"}
TEXT_EXT = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml", ".ini", ".cfg", ".html", ".xml", ".js",
            ".ts", ".c", ".cpp", ".h", ".java", ".go", ".rs", ".sh", ".sql", ".tex", ".srt", ".vtt"}


def wanted(kind, meta) -> bool:
    """Качать ли вложение: только то, что Nemotron примет (или Parakeet расшифрует).
    Не качаются анимированные стикеры .tgs (Lottie) и документы, кроме PDF, картинок/видео/аудио и текстовых."""
    if kind not in DOWNLOADABLE:
        return False
    meta = meta or {}
    mime = meta.get("mime") or ""
    if kind == "sticker":
        return mime != "application/x-tgsticker"
    if kind == "document":
        name = (meta.get("file_name") or "").lower()
        ext = name[name.rfind("."):] if "." in name else ""
        return (mime == "application/pdf" or ext == ".pdf" or mime.startswith(("image/", "video/", "audio/", "text/"))
                or ext in TEXT_EXT)
    return True


def tt_reactions(msg):
    """Счётчики реакций {emoji: n} из самого сообщения Telethon и признак, можно ли получить список."""
    res = msg.reactions
    if not res:
        return {}, False, []
    counts = {}
    for rc in res.results or []:
        key = reaction_key(rc.reaction)
        if key:
            counts[key] = rc.count
    recent = [(_peer(r.peer_id), reaction_key(r.reaction), int(r.date.timestamp()) if r.date else None)
              for r in (res.recent_reactions or [])]
    return counts, bool(res.can_see_list), recent


def tt_user_row(entity) -> dict:
    """Пользователь/канал Telethon → строка users."""
    if isinstance(entity, tt.User):
        row = {"id": entity.id, "kind": "user",
               "first_name": entity.first_name or ("Удалённый аккаунт" if entity.deleted else None),
               "last_name": entity.last_name, "username": entity.username or _first_username(entity),
               "is_bot": int(bool(entity.bot)), "is_premium": int(bool(entity.premium)), "lang": entity.lang_code}
        if entity.contact and not entity.is_self:
            # у контактов аккаунта Telegram отдаёт имя из записной книжки — не берём вовсе
            row.pop("first_name")
            row.pop("last_name")
        # имена и ники проверяет только бот; от Telethon — лишь чтобы заполнить пустое у нового человека
        row["names_weak"] = True
        return row
    if isinstance(entity, (tt.Channel, tt.Chat)):
        return {"id": tu.get_peer_id(entity), "kind": "channel", "first_name": entity.title,
                "username": getattr(entity, "username", None) or _first_username(entity),
                "photo_id": getattr(getattr(entity, "photo", None), "photo_id", None)}
    return {"id": tu.get_peer_id(entity), "kind": "user"}


def _first_username(entity):
    for u in getattr(entity, "usernames", None) or []:
        if u.active:
            return u.username
    return None


# ================================================================ Bot API (PTB)

def _ptb_entities(message):
    out = []
    for e in (message.entities or message.caption_entities or ()):
        item = {"t": str(e.type), "o": e.offset, "l": e.length}
        if e.user:
            item["u"] = e.user.id
        if e.url:
            item["url"] = e.url
        if e.custom_emoji_id:
            item["id"] = e.custom_emoji_id
        out.append(item)
    return out or None


def _file_meta(obj, **extra):
    meta = {"size": getattr(obj, "file_size", None), "mime": getattr(obj, "mime_type", None),
            "file_name": getattr(obj, "file_name", None), "duration": getattr(obj, "duration", None),
            "w": getattr(obj, "width", None), "h": getattr(obj, "height", None), "file_id": getattr(obj, "file_id", None)}
    meta.update(extra)
    return {k: v for k, v in meta.items() if v not in (None, "")}


def ptb_media(m):
    if m.sticker:
        return "sticker", _file_meta(m.sticker, emoji=m.sticker.emoji)
    if m.video_note:
        return "video_note", _file_meta(m.video_note, w=m.video_note.length, h=m.video_note.length)
    if m.voice:
        return "voice", _file_meta(m.voice)
    if m.animation:
        return "gif", _file_meta(m.animation)
    if m.video:
        return "video", _file_meta(m.video)
    if m.audio:
        return "audio", _file_meta(m.audio, title=m.audio.title, performer=m.audio.performer)
    if m.photo:
        return "photo", _file_meta(m.photo[-1])
    if m.document:
        return "document", _file_meta(m.document)
    if m.poll:
        return "poll", {"question": m.poll.question, "options": [o.text for o in m.poll.options],
                        "quiz": m.poll.type == "quiz"}
    if m.venue:
        return "location", {"lat": m.venue.location.latitude, "lon": m.venue.location.longitude,
                            "title": m.venue.title, "address": m.venue.address}
    if m.location:
        return "location", {"lat": m.location.latitude, "lon": m.location.longitude}
    if m.contact:
        return "contact", {"name": f"{m.contact.first_name} {m.contact.last_name or ''}".strip()}
    if m.dice:
        return "dice", {"emoji": m.dice.emoji, "value": m.dice.value}
    if m.story:
        return "other", {"type": "story"}
    return None, None


def _origin_id(origin):
    """MessageOrigin → (id, имя)."""
    if origin is None:
        return None, None
    kind = origin.type
    if kind == "user":
        return origin.sender_user.id, None
    if kind == "hidden_user":
        return None, origin.sender_user_name
    if kind == "chat":
        return origin.sender_chat.id, origin.author_signature
    if kind == "channel":
        return origin.chat.id, origin.author_signature
    return None, None


def from_ptb(m) -> dict | None:
    """Сообщение Bot API → строка кэша. None — служебное, которое не храним."""
    row = {"id": m.message_id, "date": int(m.date.timestamp()), "source": "bot"}
    if m.sender_chat:
        row["sender_id"] = m.sender_chat.id
    elif m.from_user:
        row["sender_id"] = m.from_user.id
    if m.new_chat_members:
        row["service"], row["service_data"] = "ChatAddUser", {"users": [u.id for u in m.new_chat_members]}
        return row
    if m.left_chat_member:
        row["service"], row["service_data"] = "ChatDeleteUser", {"users": [m.left_chat_member.id]}
        return row
    if m.pinned_message:
        row["service"], row["service_data"] = "PinMessage", {"pinned": m.pinned_message.message_id}
        return row
    if m.new_chat_title:
        row["service"], row["service_data"] = "ChatEditTitle", {"title": m.new_chat_title}
        return row
    if m.forum_topic_created or m.forum_topic_edited or m.forum_topic_closed or m.forum_topic_reopened \
            or m.delete_chat_photo or m.new_chat_photo or m.group_chat_created or m.video_chat_started \
            or m.video_chat_ended or m.video_chat_participants_invited or m.message_auto_delete_timer_changed:
        row["service"], row["service_data"] = "Other", {}
        return row
    row["edit_date"] = int(m.edit_date.timestamp()) if m.edit_date else None
    row["text"] = m.text or m.caption or None
    row["entities"] = _ptb_entities(m)
    row["grouped_id"] = int(m.media_group_id) if m.media_group_id and m.media_group_id.isdigit() else None
    row["via_bot"] = m.via_bot.id if m.via_bot else None
    reply = m.reply_to_message
    if reply is not None and not (m.is_topic_message and reply.forum_topic_created):
        row["reply_to"] = reply.message_id
        if m.quote and m.quote.text:
            row["quote"] = m.quote.text
    elif m.is_topic_message and m.message_thread_id:
        row["top_id"] = m.message_thread_id
    if m.external_reply is not None:
        row["reply_peer"] = getattr(m.external_reply.chat, "id", None)
        row["reply_to"] = m.external_reply.message_id
    if m.forward_origin is not None:
        row["fwd_from_id"], row["fwd_from_name"] = _origin_id(m.forward_origin)
        row["fwd_date"] = int(m.forward_origin.date.timestamp())
        if m.is_automatic_forward:
            row["auto_fwd"] = 1
    kind, meta = ptb_media(m)
    if kind:
        row["media"], row["media_meta"] = kind, meta
        row["media_state"] = "pending" if wanted(kind, meta) else None
    return row


def ptb_user_row(user) -> dict:
    """Пользователь из Bot API — публичные имя и ник (без правок из контактов), пишутся как есть."""
    return {"id": user.id, "kind": "user", "first_name": user.first_name, "last_name": user.last_name,
            "username": user.username, "is_bot": int(bool(user.is_bot)),
            "is_premium": int(bool(getattr(user, "is_premium", False))), "lang": user.language_code,
            "names_exact": True, "name_src": "bot"}


def ptb_chat_row(chat) -> dict:
    return {"id": chat.id, "kind": "channel", "first_name": chat.title, "username": chat.username}
