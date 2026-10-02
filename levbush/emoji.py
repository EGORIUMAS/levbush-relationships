"""Обычные эмодзи → официальные анимированные custom emoji Telegram (наборы «Animated Emoji»).

Соответствие берётся из наборов Telegram с типом custom_emoji (getStickerSet): RestrictedEmoji (~1000 эмодзи),
недостающие — из AnimatedEmoji (200). Кэшируется в kv на неделю. Отправлять custom emoji бот может, если у
владельца бота есть Telegram Premium.
"""
import logging
import re
import time

from telegram.error import TelegramError

log = logging.getLogger("levbush.emoji")

SETS = ("RestrictedEmoji", "AnimatedEmoji")      # первый — основной, второй дополняет
REFRESH = 7 * 86400
MAX_CUSTOM = 100                                 # больше custom emoji в одном сообщении Telegram не показывает
VS16 = "️"
# одно эмодзи без текста: пиктограммы, флаги, модификаторы, ZWJ, клавиши (#️⃣)
_EMOJI_ONLY = re.compile(r"[\U0001F000-\U0001FAFF←-⇿⌀-➿⬀-⯿〰〽㊗㊙"
                         r"©®‼⁉™ℹ‍️⃣#*0-9\U000E0020-\U000E007F]+")


class EmojiMap:
    def __init__(self, cache):
        self.cache = cache
        self.ids: dict[str, str] = {}
        self._re: re.Pattern | None = None
        self._set((cache.get("emoji_map") or {}).get("ids") or {})

    def _set(self, ids: dict[str, str]):
        full = dict(ids)
        for e, cid in ids.items():                  # «❤» и «❤️» — одно и то же
            full.setdefault(e.replace(VS16, ""), cid)
            if len(e) == 1:
                full.setdefault(e + VS16, cid)
        self.ids = full
        keys = sorted(full, key=len, reverse=True)  # сначала длинные: 👨‍👩‍👧 раньше 👨, 👍🏽 раньше 👍
        self._re = re.compile("|".join(map(re.escape, keys))) if keys else None

    async def load(self, bot):
        """Соответствие из наборов Telegram; раз в неделю обновляется."""
        saved = self.cache.get("emoji_map") or {}
        if saved.get("ids") and time.time() - saved.get("at", 0) < REFRESH:
            return
        ids: dict[str, str] = {}
        for name in SETS:
            try:
                st = await bot.get_sticker_set(name)
            except TelegramError as exc:
                log.warning("набор эмодзи %s: %s", name, exc)
                continue
            for s in st.stickers:
                if s.emoji and s.custom_emoji_id:
                    ids.setdefault(s.emoji, s.custom_emoji_id)
        if ids:
            self.cache.set("emoji_map", {"at": int(time.time()), "ids": ids})
            self._set(ids)
            log.info("анимированных эмодзи: %d", len(ids))

    def single(self, text: str) -> str | None:
        """Ответ — одно эмодзи (или несколько подряд без текста)? Тогда само эмодзи, иначе None."""
        t = (text or "").strip()
        return t if t and len(t) <= 16 and _EMOJI_ONLY.fullmatch(t) and not t.isdigit() else None

    def _sub(self, text: str, fmt) -> str:
        if not self._re:
            return text
        n = 0

        def one(m):
            nonlocal n
            n += 1
            return fmt(m.group(0), self.ids[m.group(0)]) if n <= MAX_CUSTOM else m.group(0)

        # в `коде` и ```блоках``` не трогаем
        parts = re.split(r"(```.*?```|`[^`\n]*`)", text, flags=re.S)
        return "".join(p if i % 2 else self._re.sub(one, p) for i, p in enumerate(parts))

    def markdown(self, text: str) -> str:
        """Rich Markdown: ![😀](tg://emoji?id=…)."""
        return self._sub(text, lambda e, cid: f"![{e}](tg://emoji?id={cid})")

    def html(self, text: str) -> str:
        """HTML Telegram: <tg-emoji emoji-id="…">😀</tg-emoji> (эмодзи не встречаются внутри тегов и ссылок)."""
        return self._sub(text, lambda e, cid: f'<tg-emoji emoji-id="{cid}">{e}</tg-emoji>')
