"""Вложения → части запроса к Nemotron (он омнимодальный: картинки, видео, звук идут как есть).

Отдельно готовятся только то, чего модель не принимает напрямую:
- PDF → страницы PNG; текстовые файлы → их текст (в самой переписке, см. render.py);
- звук в контейнере, который не читает libsndfile (m4a/aac…), → wav;
- у видео со звуком дорожка идёт отдельным звуком сразу после видео: режим use_audio_in_video в vLLM требует,
  чтобы звуков в запросе было ровно столько же, сколько видео, и ломается от любого отдельного голосового;
- видео без звуковой дорожки (GIF, немое) → несколько кадров;
- видео другого разрешения, чем первое в запросе, → кадрами (+ звук): vLLM не принимает в одном запросе видео
  разного размера («Failed to apply NanoNemotronVLProcessor»), по отдельности они проходят.
Речь дополнительно расшифровывает Parakeet (Nemotron понимает только английскую речь) — текст в переписке.
"""
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import llm as L
from .config import Config

SNDFILE_EXT = {".ogg", ".oga", ".opus", ".wav", ".flac"}   # mp3 vLLM не открывает («File does not exist»)
AV_KINDS = ("voice", "audio", "video", "video_note", "gif")


def is_av(m) -> bool:
    """Звук или видео: Qwen их не принимает — описывает Nemotron (вместе с расшифровкой Parakeet), Qwen получает
    описание и кадры. Картинки (фото, PDF, webp-стикеры) Qwen видит сам."""
    kind = m["media"]
    meta = json.loads(m["media_meta"]) if m["media_meta"] else {}
    mime = meta.get("mime") or ""
    ext = Path(m["media_path"] or "").suffix.lower()
    return (kind in AV_KINDS or (kind == "sticker" and ext in (".webm", ".mp4"))
            or (kind == "document" and (mime.startswith("video/") or mime.startswith("audio/"))))


LABEL = {"photo": "фото", "video": "видео", "video_note": "кружок", "voice": "голосовое", "audio": "аудио",
         "gif": "GIF", "document": "файл", "sticker": "стикер"}


@dataclass
class Budget:
    """Сколько медиа ещё можно положить в запрос (совпадает с --limit-mm-per-prompt сервера)."""
    image: int
    video: int
    audio: int
    tokens: int
    audio_in_video: bool = False
    video_size: tuple | None = None    # разрешение видео в запросе: vLLM падает на видео разного размера
    dropped: list = field(default_factory=list)


class Media:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conv = cfg.data_dir / "conv"
        self.conv.mkdir(parents=True, exist_ok=True)
        self._audio_cache: dict[str, bool] = {}
        self._size_cache: dict[str, tuple | None] = {}
        self._tok_cache: dict[str, int] = {}

    def budget(self, tokens: int) -> Budget:
        return Budget(self.cfg.mm_images, self.cfg.mm_videos, self.cfg.mm_audio, tokens)

    # ------------------------------------------------------------ подготовка файлов

    def video_size(self, path: str) -> tuple | None:
        if path not in self._size_cache:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=width,height", "-of", "csv=p=0", path], capture_output=True, text=True).stdout
            try:
                w, h = (int(x) for x in out.strip().split(",")[:2])
                self._size_cache[path] = (w, h)
            except ValueError:
                self._size_cache[path] = None
        return self._size_cache[path]

    def image_tokens(self, path) -> int:
        """Замер на Nemotron: картинка режется на плитки 512×512 (до 12) + уменьшенная копия, 256 токенов на плитку.
        568×186 → 283 токена, 1920×2560 → 3286."""
        key = str(path)
        if key not in self._tok_cache:
            try:
                from PIL import Image
                w, h = Image.open(path).size
                tiles = min(12, math.ceil(w / 512) * math.ceil(h / 512))
                self._tok_cache[key] = 256 * (tiles + (1 if tiles > 1 else 0)) + 30
            except Exception:  # noqa: BLE001
                self._tok_cache[key] = self.cfg.tok_image
        return self._tok_cache[key]

    def has_audio(self, path: str) -> bool:
        if path not in self._audio_cache:
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                "-of", "csv=p=0", path], capture_output=True, text=True)
            self._audio_cache[path] = bool(r.stdout.strip())
        return self._audio_cache[path]

    def _wav(self, src: str, msg_id: int) -> Path | None:
        out = self.conv / f"{msg_id}.wav"
        if not out.exists():
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", str(out)],
                               capture_output=True)
            if r.returncode != 0:
                return None
        return out

    def _frames(self, src: str, msg_id: int, duration: float, n: int = 3) -> list[Path]:
        """n кадров из видео. Видеостикеры Telegram (webm, VP9 с прозрачностью) приходят с неверным цветовым
        пространством и почти нулевой длительностью в метаданных — их сначала перепаковываем (vp9_metadata)."""
        dec, ext = [], "jpg"
        if src.endswith(".webm"):
            fixed = self.conv / f"{msg_id}.fix.webm"
            if not fixed.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c", "copy", "-bsf:v",
                                "vp9_metadata=color_space=bt709", str(fixed)], capture_output=True)
            src = str(fixed) if fixed.exists() else src
            dec, ext = ["-c:v", "libvpx-vp9"], "png"        # libvpx сохраняет альфа-канал
        tag = f"{msg_id}-n{n}f"                                # число кадров в имени: Nemotron — 3, Qwen — свои
        out = sorted(self.conv.glob(f"{tag}*.{ext}"))
        if out:
            return out
        if duration and duration >= 1:
            for i in range(n):
                at = max(0.0, duration * (i + 0.5) / n)
                p = self.conv / f"{tag}{i}.{ext}"
                subprocess.run(["ffmpeg", "-v", "error", "-y", *dec, "-ss", f"{at:.2f}", "-i", src, "-frames:v", "1",
                                str(p)], capture_output=True)
                if p.exists():
                    out.append(p)
        if not out:                                           # длительность неизвестна — кадр в секунду
            subprocess.run(["ffmpeg", "-v", "error", "-y", *dec, "-i", src, "-vf", "fps=1", "-frames:v", str(n),
                            str(self.conv / f"{tag}%d.{ext}")], capture_output=True)
            out = sorted(self.conv.glob(f"{tag}*.{ext}"))
        return out

    def _pdf_pages(self, path: str, msg_id: int) -> list[Path]:
        out_dir = self.conv / f"{msg_id}-pdf"
        pages = sorted(out_dir.glob("p-*.png")) if out_dir.exists() else []
        if not pages:
            out_dir.mkdir(exist_ok=True)
            subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", str(self.cfg.pdf_pages), path,
                            str(out_dir / "p")], capture_output=True, timeout=300)
            pages = sorted(out_dir.glob("p-*.png"))
        return pages

    # ------------------------------------------------------------ части запроса

    def parts(self, m, budget: Budget) -> list:
        """Части запроса для вложения сообщения m (с подписью), либо [] — если не передаётся."""
        kind, path = m["media"], m["media_path"]
        if not kind or m["media_state"] != "ok" or not path or not Path(path).exists():
            return []
        meta = json.loads(m["media_meta"]) if m["media_meta"] else {}
        mime = meta.get("mime") or ""
        ext = Path(path).suffix.lower()
        dur = float(meta.get("duration") or 0)
        if kind == "document" and mime.startswith("video/"):
            kind = "video"
        elif kind == "document" and mime.startswith("audio/"):
            kind = "audio"
        label = f"Вложение к сообщению #{m['id']} ({LABEL.get(kind, kind)}):"
        cfg = self.cfg

        def images(paths):
            paths = paths[:budget.image]
            cost = sum(self.image_tokens(p) for p in paths)
            if not paths or cost > budget.tokens:
                budget.dropped.append(m["id"])
                return []
            budget.image -= len(paths)
            budget.tokens -= cost
            return [L.text(label)] + [L.image(p) for p in paths]

        if kind == "photo" or (kind == "document" and mime.startswith("image/")) or (kind == "sticker" and ext == ".webp"):
            return images([path])
        if kind == "document" and (ext == ".pdf" or mime == "application/pdf"):
            return images(self._pdf_pages(path, m["id"]))
        if kind in ("voice", "audio"):
            if dur > cfg.video_max_sec or budget.audio < 1:
                budget.dropped.append(m["id"])
                return []
            cost = int(dur * cfg.tok_audio_sec) + 50
            if cost > budget.tokens:
                budget.dropped.append(m["id"])
                return []
            src = Path(path) if ext in SNDFILE_EXT else self._wav(path, m["id"])
            if src is None:
                return []
            budget.audio -= 1
            budget.tokens -= cost
            return [L.text(label), L.audio(src)]
        if kind in ("video_note", "video", "gif") or (kind == "sticker" and ext in (".webm", ".mp4")):
            sound = kind != "gif" and kind != "sticker" and self.has_audio(path)
            if not sound or dur > cfg.video_max_sec:
                # немое видео и слишком длинное — кадрами (звук длинного — в расшифровке)
                return images(self._frames(path, m["id"], dur))
            frames = min(cfg.video_max_frames, max(1, int(dur * cfg.video_fps)))
            cost = frames * cfg.tok_video_frame + int(dur * cfg.tok_audio_sec) + 50
            size = self.video_size(path)
            wav = self._wav(path, m["id"]) if budget.audio >= 1 else None
            if (budget.video < 1 or wav is None or cost > budget.tokens or size is None
                    or (budget.video_size is not None and size != budget.video_size)):
                frames = images(self._frames(path, m["id"], dur))
                if frames and wav is not None and budget.audio >= 1:
                    budget.audio -= 1
                    frames += [L.text(f"Звук из #{m['id']}:"), L.audio(wav)]
                return frames
            budget.video_size = size
            budget.video -= 1
            budget.audio -= 1
            budget.tokens -= cost
            return [L.text(label), L.video(path), L.text(f"Звук из #{m['id']}:"), L.audio(wav)]
        return []

    def qwen_parts(self, m, budget: Budget, desc: str | None) -> list:
        """Части запроса для Qwen (картинки видит, звук и видео — нет): картинки как есть; у видео, кружков, GIF и
        видеостикеров — кадры и описание Nemotron; у голосовых — описание (расшифровка и так в строке сообщения)."""
        if not is_av(m):
            return self.parts(m, budget)
        kind, path = m["media"], m["media_path"]
        label = LABEL.get(kind, kind)
        out = []
        visual = kind in ("video", "video_note", "gif", "sticker") or (
            kind == "document" and (json.loads(m["media_meta"] or "{}").get("mime") or "").startswith("video/"))
        if (visual and self.cfg.video_frames and m["media_state"] == "ok" and path and Path(path).exists()):
            dur = float((json.loads(m["media_meta"]) if m["media_meta"] else {}).get("duration") or 0)
            frames = self._frames(path, m["id"], dur, n=self.cfg.video_frames)[:max(0, budget.image)]
            cost = sum(self.image_tokens(f) for f in frames)
            if frames and cost <= budget.tokens:
                budget.image -= len(frames)
                budget.tokens -= cost
                out += [L.text(f"Вложение к сообщению #{m['id']} ({label}), кадры:")] + [L.image(f) for f in frames]
            elif frames:
                budget.dropped.append(m["id"])
        if desc:
            t = (f"[Описание вложения #{m['id']} ({label}) от Nemotron — он смотрел и слушал его целиком; речь бери "
                 f"из расшифровки Parakeet, детали Nemotron может выдумать: {desc.strip()}]")
            budget.tokens -= int(len(t) / 2.8)
            out.append(L.text(t))
        return out

    def cost(self, m) -> int:
        """Оценка токенов вложения без учёта лимитов запроса (для планирования шагов)."""
        b = Budget(99, 99, 99, 10 ** 9)
        self.parts(m, b)
        return 10 ** 9 - b.tokens
