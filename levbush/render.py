"""Сообщения кэша → текст для нейросети (разбор эпизодов, пересказ) и ссылки на сообщения."""
import json
import re
from datetime import datetime
from pathlib import Path

from .cache import Cache
from .config import Config

MEDIA_RU = {"photo": "фото", "video": "видео", "video_note": "кружок", "voice": "голосовое", "audio": "аудио",
            "gif": "GIF", "document": "файл", "sticker": "стикер", "poll": "опрос", "location": "геопозиция",
            "contact": "контакт", "dice": "кубик", "webpage": "ссылка", "other": "вложение"}


def msg_link(chat: dict, msg_id: int) -> str:
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{msg_id}"
    cid = str(chat.get("id") or "")
    internal = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{internal}/{msg_id}"


def link_msgs(markdown: str, chat: dict) -> str:
    """[текст](msg:123) и голые #123 в ответах нейросети → ссылки на сообщения."""
    if not markdown:
        return markdown
    markdown = re.sub(r"\]\(msg:(\d+)\)", lambda m: f"]({msg_link(chat, int(m.group(1)))})", markdown)
    return markdown


TEXT_EXT = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml", ".ini", ".cfg", ".html", ".xml", ".js",
            ".ts", ".c", ".cpp", ".h", ".java", ".go", ".rs", ".sh", ".sql", ".tex", ".srt", ".vtt"}


def file_text(path: str, meta: dict, limit: int = 6000) -> str | None:
    """Текст выделяется только из текстовых файлов; остальное Nemotron получает как медиа."""
    ext = Path(path).suffix.lower()
    if ext not in TEXT_EXT and not (meta.get("mime") or "").startswith("text/"):
        return None
    try:
        return Path(path).read_bytes()[:limit * 4].decode("utf-8", errors="replace")[:limit]
    except OSError:
        return None


def fmt_dur(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


class Renderer:
    def __init__(self, cache: Cache, cfg: Config):
        self.cache, self.cfg = cache, cfg
        self.chat = cache.get("chat", {}) or {}
        self._names = {}
        self.alias = cache.aliases(cfg.aliases)

    def sender(self, m):
        """Автор с учётом псевдонимов: сообщение от имени группы/канала → конкретный человек."""
        s = m["sender_id"]
        return self.alias.get(s, s) if not m["auto_fwd"] else s

    def name(self, uid) -> str:
        if uid is None:
            return "неизвестный"
        if uid in self._names:
            return self._names[uid]
        u = self.cache.user(uid)
        if u is None:
            label = f"id{uid}"
        else:
            label = " ".join(x for x in (u["first_name"], u["last_name"]) if x) or (u["username"] or f"id{uid}")
            if u["username"]:
                label += f" (@{u['username']})"
        if uid == self.chat.get("channel_id"):
            label += " [канал]"
        elif uid == self.chat.get("id"):
            label += " [от имени группы]"
        self._names[uid] = label
        return label

    def short_text(self, msg_id: int, limit: int = 90) -> str:
        m = self.cache.message(msg_id)
        if m is None:
            return f"#{msg_id}"
        body = (m["text"] or "").replace("\n", " ")
        if not body and m["media"]:
            body = f"[{MEDIA_RU.get(m['media'], m['media'])}]"
        if len(body) > limit:
            body = body[:limit] + "…"
        return f"#{msg_id} {self.name(self.sender(m)).split(' (@')[0]}: «{body}»"

    def reactions(self, msg_id: int) -> str:
        rows = self.cache.db.execute("select user_id, emoji from reactions where msg_id = ?", (msg_id,)).fetchall()
        if rows:
            by = {}
            for uid, emoji in rows:
                label = self.cache.reaction_label(emoji)
                if emoji.startswith("custom:"):
                    label += " (премиум)"
                by.setdefault(label, []).append(
                    self.name(uid).split(" (@")[0])
            return "; ".join(f"{e} {', '.join(n)}" for e, n in by.items())
        counts = self.cache.db.execute("select emoji, count from reaction_counts where msg_id = ?", (msg_id,)).fetchall()
        return "; ".join(f"{e} ×{n}" for e, n in counts)

    def media_line(self, m) -> str | None:
        kind = m["media"]
        if not kind:
            return None
        meta = json.loads(m["media_meta"]) if m["media_meta"] else {}
        label = MEDIA_RU.get(kind, kind)
        parts = []
        if kind in ("voice", "video_note", "video", "audio") and meta.get("duration"):
            label += f" {fmt_dur(meta['duration'])}"
        if kind == "document" and meta.get("file_name"):
            label += f" «{meta['file_name']}»"
        if kind == "audio" and (meta.get("title") or meta.get("performer")):
            label += f" «{meta.get('performer') or ''} — {meta.get('title') or ''}»"
        if kind == "sticker" and meta.get("emoji"):
            label += f" {meta['emoji']}"
        if kind == "poll":
            label += f": «{meta.get('question')}» — варианты: " + " / ".join(meta.get("options") or [])
        if kind == "location" and meta.get("title"):
            label += f": {meta['title']}, {meta.get('address') or ''}"
        if kind == "webpage" and (meta.get("title") or meta.get("site")):
            label += f": {meta.get('site') or ''} «{meta.get('title') or ''}»"
            if meta.get("description"):
                parts.append(f"превью: {meta['description'][:200]}")
        tr = self.cache.transcript(m["id"])
        if tr:
            parts.append(f"расшифровка: «{tr}»")
        if kind == "document":
            body = file_text(m["media_path"], meta) if m["media_state"] == "ok" and m["media_path"] else None
            if body:
                parts.append(f"текст файла:\n{body}")
        if m["media_state"] == "skip":
            parts.append("файл слишком большой, не скачан")
        return f"[{label}" + (" — " + "; ".join(parts) if parts else "") + "]"

    def line(self, m, with_ids: bool = True) -> str:
        dt = datetime.fromtimestamp(m["date"], self.cfg.tz).strftime("%Y-%m-%d %H:%M")
        uid = self.sender(m)
        who = self.name(uid)
        if uid != m["sender_id"]:
            who += " [пишет от имени " + ("канала]" if m["sender_id"] == self.chat.get("channel_id") else "группы]")
        head = f"[#{m['id']} {dt}] {who}" + (f" (id {uid})" if with_ids and uid else "")
        if m["auto_fwd"]:
            head += " — пост канала"
        extra = []
        if m["reply_to"] and not m["reply_peer"]:
            extra.append("↩ ответ на " + self.short_text(m["reply_to"]))
        elif m["reply_to"]:
            extra.append("↩ ответ на сообщение из другого чата")
        if m["quote"]:
            extra.append(f"цитирует: «{m['quote'][:300]}»")
        if (m["fwd_from_id"] or m["fwd_from_name"]) and not m["auto_fwd"]:
            src = self.name(m["fwd_from_id"]) if m["fwd_from_id"] else m["fwd_from_name"]
            extra.append(f"переслано от {src}")
        if m["edit_date"]:
            extra.append("изменено")
        body = []
        if m["text"]:
            body.append(m["text"])
        media = self.media_line(m)
        if media:
            body.append(media)
        reacts = self.reactions(m["id"])
        if reacts:
            body.append(f"(реакции: {reacts})")
        return head + (" | " + "; ".join(extra) if extra else "") + ":\n" + "\n".join(body)
