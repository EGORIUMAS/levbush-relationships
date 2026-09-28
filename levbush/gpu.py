"""Nemotron 3 Nano Omni на vLLM: поднять по требованию, погасить по простою, не мешать H3 и Qwen.

- Пока MiniMax H3 считает запрос (progress-файл), модель не запускается — ждём.
- Если VRAM не хватает и vLLM-Qwen бодрствует — усыпляем его (/sleep?level=2), после работы будим.
- Сервер — отдельный user-юнит `levbush-nemotron` (systemd-run) с потолком RAM.
  --enable-sleep-mode с Nemotron не работает (CUDA OOM при загрузке), поэтому по простою юнит останавливается.
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger("levbush.gpu")

UNIT = "levbush-nemotron"
CUDA_HOME = os.environ.get("CUDA_HOME") or ("/opt/cuda" if Path("/opt/cuda/bin/nvcc").exists() else "/usr/local/cuda")
SERVED_NAME = "nemotron3-nano-omni"


def gpu_free_gib() -> float:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        return int(out.stdout.split()[0]) / 1024
    except Exception:  # noqa: BLE001
        return float("nan")


def h3_busy() -> str | None:
    """Описание активного запроса MiniMax H3 или None (как в vidup)."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    for path in sorted(runtime.glob("minimax-h3-progress-*.json")):
        try:
            d = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        stage = d.get("stage")
        if stage in (None, "done", "error"):
            continue
        if time.time() - float(d.get("updated", 0)) > 300:
            continue
        return f"H3: этап «{stage}»"
    return None


class LLMManager:
    def __init__(self, cfg: Config, notify=None):
        self.cfg = cfg
        self.notify = notify           # async def notify(text) — сообщения админу
        self.users = 0
        self.started_by_us = False
        self.slept_qwen = False
        self.last_used = 0.0
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self.model: str | None = None
        self.state = "unknown"

    async def _say(self, text):
        log.info(text)
        if self.notify:
            try:
                await self.notify(text)
            except Exception:  # noqa: BLE001
                log.exception("notify")

    # ------------------------------------------------------------ проверки

    async def is_up(self, base: str | None = None) -> bool:
        base = base or self.cfg.llm_url
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{base}/v1/models")
                if r.status_code == 200:
                    data = r.json().get("data") or []
                    if data and base == self.cfg.llm_url:
                        self.model = self.cfg.llm_model or data[0]["id"]
                    return bool(data)
        except httpx.HTTPError:
            pass
        return False

    def unit_active(self) -> bool:
        r = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True)
        return r.stdout.strip() in ("active", "activating")

    async def _qwen(self, path: str, method="GET"):
        url = f"http://127.0.0.1:{self.cfg.qwen_port}{path}"
        try:
            async with httpx.AsyncClient(timeout=300) as c:
                r = await c.request(method, url)
                return r
        except httpx.HTTPError:
            return None

    async def _qwen_awake(self) -> bool:
        r = await self._qwen("/is_sleeping")
        if r is None or r.status_code != 200:
            return False
        return "true" not in r.text.lower()

    # ------------------------------------------------------------ запуск/остановка

    def _command(self) -> list[str]:
        vllm = shutil.which("vllm") or str(Path.home() / ".local/bin/vllm")
        port = self.cfg.llm_url.rsplit(":", 1)[-1].split("/")[0]
        return [
            "systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet",
            "-p", "MemoryMax=36G", "-p", "MemorySwapMax=0", "-p", "KillSignal=SIGINT", "-p", "TimeoutStopSec=60",
            "--setenv=VLLM_SERVER_DEV_MODE=1", "--setenv=MAX_JOBS=2", "--setenv=NVCC_THREADS=1",
            # у systemd --user PATH урезан (/usr/local/bin:/usr/bin): без nvcc FlashInfer не соберёт JIT-ядра
            "--setenv=FLASHINFER_NVCC_THREADS=1", f"--setenv=CUDA_HOME={CUDA_HOME}",
            f"--setenv=PATH={CUDA_HOME}/bin:{Path.home()}/.local/bin:{os.environ.get('PATH', '/usr/local/bin:/usr/bin')}",
            f"--setenv=HOME={Path.home()}",
            vllm, "serve", self.cfg.llm_model_path,
            "--served-model-name", SERVED_NAME, "--host", "127.0.0.1", "--port", port,
            "--trust-remote-code", "--max-model-len", str(self.cfg.llm_ctx),
            "--max-num-seqs", str(self.cfg.llm_seqs), "--gpu-memory-utilization", "0.85",
            "--kv-cache-dtype", "turboquant_k8v4", "--attention-config.flash_attn_version=2",
            "--video-pruning-rate", "0.5", "--allowed-local-media-path", "/",
            "--media-io-kwargs", json.dumps({"video": {"fps": self.cfg.video_fps,
                                                       "num_frames": self.cfg.video_max_frames}}),
            "--limit-mm-per-prompt", json.dumps({"image": self.cfg.mm_images, "video": self.cfg.mm_videos,
                                                 "audio": self.cfg.mm_audio}),
            # без этого штраф за повторы гонит модель в пробелы между токенами JSON
            "--structured-outputs-config", json.dumps({"backend": "xgrammar", "disable_any_whitespace": True}),
            "--enable-prefix-caching", "--reasoning-parser", "nemotron_v3",
            "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
        ]

    async def _start(self):
        waited = 0
        while True:
            busy = h3_busy()
            if not busy:
                break
            if waited % 600 == 0:
                await self._say(f"⏳ Жду GPU: {busy}")
            await asyncio.sleep(30)
            waited += 30
        free = gpu_free_gib()
        if free == free and free < self.cfg.llm_need_gib and await self._qwen_awake():
            await self._say(f"Свободно {free:.1f} ГиБ VRAM — усыпляю vLLM-Qwen на время разбора")
            r = await self._qwen("/sleep?level=2", "POST")
            if r is not None and r.status_code == 200:
                self.slept_qwen = True
                await asyncio.sleep(3)
                free = gpu_free_gib()
        if free == free and free < self.cfg.llm_need_gib:
            raise RuntimeError(f"не хватает VRAM для Nemotron: свободно {free:.1f} из ~{self.cfg.llm_need_gib:.0f} ГиБ")
        subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True)
        await self._say("🚀 Запускаю Nemotron 3 Nano Omni (~2 мин)")
        r = subprocess.run(self._command(), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"systemd-run: {r.stderr.strip()[-500:]}")
        self.started_by_us = True
        deadline = time.monotonic() + 40 * 60          # первый старт собирает JIT-ядра до ~25 мин
        while time.monotonic() < deadline:
            if await self.is_up():
                self.state = "up"
                await self._say("✅ Nemotron запущен")
                return
            if not self.unit_active():
                logs = subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "30", "--no-pager"],
                                      capture_output=True, text=True).stdout
                raise RuntimeError("vLLM с Nemotron упал при запуске:\n" + logs[-1500:])
            await asyncio.sleep(5)
        raise RuntimeError("Nemotron не поднялся за 40 мин")

    async def stop(self):
        if self.unit_active():
            subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
            await self._say("💤 Nemotron остановлен")
        self.started_by_us = False
        self.state = "down"
        if self.slept_qwen:
            r = await self._qwen("/wake_up", "POST")
            if r is not None and r.status_code == 200:
                log.info("vLLM-Qwen разбужен")
            self.slept_qwen = False

    async def _idle_watch(self):
        while True:
            await asyncio.sleep(30)
            if self.users == 0 and time.monotonic() - self.last_used > self.cfg.llm_idle_stop_min * 60:
                async with self._lock:
                    if self.users == 0 and self.started_by_us:
                        await self.stop()
                return

    @asynccontextmanager
    async def use(self, autostart: bool | None = None):
        """Контекст: внутри Nemotron гарантированно поднят (или исключение)."""
        autostart = self.cfg.llm_autostart if autostart is None else autostart
        async with self._lock:
            if not await self.is_up():
                if not autostart:
                    raise RuntimeError("Nemotron не запущен, а автозапуск выключен")
                await self._start()
            self.users += 1
        try:
            yield self
        finally:
            self.users -= 1
            self.last_used = time.monotonic()
            if self.started_by_us and (self._idle_task is None or self._idle_task.done()):
                self._idle_task = asyncio.create_task(self._idle_watch())
