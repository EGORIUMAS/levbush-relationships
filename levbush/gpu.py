"""Свои vLLM-серверы бота: поднять по требованию, погасить по простою, не мешать H3 и общему Qwen пользователя.

Два сервера, на GPU — только один за раз:
- Nemotron 3 Nano Omni (`levbush-nemotron`) — описывает звук и видео (голосовые, кружки, видео, GIF);
- Qwen 3.8 27B (`levbush-qwen`) — разбор (досье и связи) и пересказ; MTP + fp8 KV.

- Пока MiniMax H3 считает запрос (progress-файл), модель не запускается — ждём.
- Перед запуском гасим второй свой сервер (дождавшись, пока он освободится).
- Если VRAM не хватает и vLLM-Qwen пользователя (:8080 → :18081) бодрствует — усыпляем его, после работы будим.
- Сервер — отдельный user-юнит (systemd-run) с потолком RAM. --enable-sleep-mode с Nemotron не работает (CUDA OOM
  при загрузке), поэтому по простою юнит просто останавливается.
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


async def served_models(base: str) -> list[dict]:
    """Модели сервера (/v1/models не будит спящий vLLM за прокси)."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{base}/v1/models")
            if r.status_code == 200:
                return r.json().get("data") or []
    except (httpx.HTTPError, ValueError):
        pass
    return []


class LLMManager:
    _start_lock: asyncio.Lock | None = None       # общий на все свои серверы: запускаем по одному
    gpu_guests: list = []      # async-функции «освободи GPU» (эмбеддер поиска) — перед запуском модели

    def __init__(self, cfg: Config, notify=None, kind: str = "nemotron"):
        self.cfg = cfg
        self.kind = kind
        self.notify = notify           # async def notify(text) — сообщения админу
        self.peers: list[LLMManager] = []
        self.users = 0
        self.started_by_us = False
        self.slept_qwen = False
        self.last_used = 0.0
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self.model: str | None = None
        self.state = "unknown"
        if kind == "qwen":
            self.unit, self.url, self.label = "levbush-qwen", cfg.qwen_url, "Qwen 3.8 27B"
            self.need_gib, self.fixed_model = cfg.qwen_need_gib, cfg.qwen_name
        else:
            self.unit, self.url, self.label = "levbush-nemotron", cfg.llm_url, "Nemotron 3 Nano Omni"
            self.need_gib, self.fixed_model = cfg.llm_need_gib, cfg.llm_model

    @property
    def start_lock(self) -> asyncio.Lock:
        if LLMManager._start_lock is None:
            LLMManager._start_lock = asyncio.Lock()
        return LLMManager._start_lock

    async def _say(self, text):
        log.info(text)
        if self.notify:
            try:
                await self.notify(text)
            except Exception:  # noqa: BLE001
                log.exception("notify")

    # ------------------------------------------------------------ проверки

    async def is_up(self) -> bool:
        data = await served_models(self.url)
        if data:
            self.model = self.fixed_model or data[0]["id"]
        return bool(data)

    def unit_active(self) -> bool:
        r = subprocess.run(["systemctl", "--user", "is-active", self.unit], capture_output=True, text=True)
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
        port = self.url.rsplit(":", 1)[-1].split("/")[0]
        cfg = self.cfg
        head = [
            "systemd-run", "--user", f"--unit={self.unit}", "--collect", "--quiet",
            "-p", "MemoryMax=36G", "-p", "MemorySwapMax=0", "-p", "KillSignal=SIGINT", "-p", "TimeoutStopSec=60",
            "--setenv=VLLM_SERVER_DEV_MODE=1", "--setenv=MAX_JOBS=2", "--setenv=NVCC_THREADS=1",
            # у systemd --user PATH урезан (/usr/local/bin:/usr/bin): без nvcc FlashInfer не соберёт JIT-ядра
            "--setenv=FLASHINFER_NVCC_THREADS=1", f"--setenv=CUDA_HOME={CUDA_HOME}",
            f"--setenv=PATH={CUDA_HOME}/bin:{Path.home()}/.local/bin:{os.environ.get('PATH', '/usr/local/bin:/usr/bin')}",
            f"--setenv=HOME={Path.home()}",
        ]
        common = ["--host", "127.0.0.1", "--port", port, "--allowed-local-media-path", "/",
                  "--attention-config.flash_attn_version=2",
                  # без этого штраф за повторы гонит модель в пробелы между токенами JSON
                  "--structured-outputs-config", json.dumps({"backend": "xgrammar", "disable_any_whitespace": True})]
        if self.kind == "qwen":
            cmd = [vllm, "serve", cfg.qwen_model_path, "--served-model-name", cfg.qwen_name,
                   "--max-model-len", str(cfg.qwen_ctx), "--max-num-seqs", str(cfg.qwen_seqs),
                   "--gpu-memory-utilization", str(cfg.qwen_util),
                   "--limit-mm-per-prompt", json.dumps({"image": cfg.qwen_images, "video": 0}),
                   "--mm-processor-kwargs", json.dumps({"max_pixels": 1048576}),
                   # без кэша префиксов: с ним MTP на запросах с картинками в vLLM 0.27.1 зависает (генерация 0 ток/с)
                   "--no-enable-prefix-caching", "--reasoning-parser", "qwen3", *common]
            if cfg.qwen_mtp:
                # MTP только с fp8 KV: с TurboQuant vLLM 0.27.1 молча портит вывод (vllm#53180)
                cmd += ["--kv-cache-dtype", "fp8", "--speculative-config",
                        json.dumps({"method": "mtp", "num_speculative_tokens": cfg.qwen_mtp})]
            else:
                cmd += ["--kv-cache-dtype", "turboquant_k8v4"]
            return head + cmd
        return head + [
            vllm, "serve", cfg.llm_model_path, "--served-model-name", SERVED_NAME, "--trust-remote-code",
            "--max-model-len", str(cfg.llm_ctx), "--max-num-seqs", str(cfg.llm_seqs),
            "--gpu-memory-utilization", "0.85", "--kv-cache-dtype", "turboquant_k8v4",
            "--video-pruning-rate", "0.5",
            "--media-io-kwargs", json.dumps({"video": {"fps": cfg.video_fps, "num_frames": cfg.video_max_frames}}),
            "--limit-mm-per-prompt", json.dumps({"image": cfg.mm_images, "video": cfg.mm_videos, "audio": cfg.mm_audio}),
            "--enable-prefix-caching", "--reasoning-parser", "nemotron_v3", "--enable-auto-tool-choice",
            "--tool-call-parser", "qwen3_coder",
            *common,
        ]

    async def _free_peers(self):
        """Второй свой сервер — погасить, дождавшись, пока им перестанут пользоваться."""
        for p in self.peers:
            if not p.unit_active():
                continue
            waited = 0
            while p.users > 0:
                if waited % 300 == 0:
                    log.info("%s ждёт, пока освободится %s", self.label, p.label)
                await asyncio.sleep(5)
                waited += 5
            async with p._lock:
                if p.users == 0:
                    await p.stop(wake_shared=False)

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
        await self._free_peers()
        for release in LLMManager.gpu_guests:
            await release()
        free = gpu_free_gib()
        if free == free and free < self.need_gib and await self._qwen_awake():
            await self._say(f"Свободно {free:.1f} ГиБ VRAM — усыпляю vLLM-Qwen на время работы")
            r = await self._qwen("/sleep?level=2", "POST")
            if r is not None and r.status_code == 200:
                self.slept_qwen = True
                await asyncio.sleep(3)
                free = gpu_free_gib()
        for p in self.peers:                     # усыплённый общий Qwen будит тот, кто гасится последним
            if p.slept_qwen:
                self.slept_qwen, p.slept_qwen = True, False
        if free == free and free < self.need_gib:
            raise RuntimeError(f"не хватает VRAM для {self.label}: свободно {free:.1f} из ~{self.need_gib:.0f} ГиБ")
        subprocess.run(["systemctl", "--user", "reset-failed", self.unit], capture_output=True)
        await self._say(f"🚀 Запускаю {self.label} (~2 мин)")
        r = subprocess.run(self._command(), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"systemd-run: {r.stderr.strip()[-500:]}")
        self.started_by_us = True
        deadline = time.monotonic() + 40 * 60          # первый старт собирает JIT-ядра до ~25 мин
        while time.monotonic() < deadline:
            if await self.is_up():
                self.state = "up"
                await self._say(f"✅ {self.label} запущен")
                return
            if not self.unit_active():
                logs = subprocess.run(["journalctl", "--user", "-u", self.unit, "-n", "30", "--no-pager"],
                                      capture_output=True, text=True).stdout
                raise RuntimeError(f"vLLM с {self.label} упал при запуске:\n" + logs[-1500:])
            await asyncio.sleep(5)
        raise RuntimeError(f"{self.label} не поднялся за 40 мин")

    async def stop(self, wake_shared: bool = True):
        if self.unit_active():
            subprocess.run(["systemctl", "--user", "stop", self.unit], capture_output=True)
            await self._say(f"💤 {self.label} остановлен")
        self.started_by_us = False
        self.state = "down"
        if self.slept_qwen and wake_shared:
            r = await self._qwen("/wake_up", "POST")
            if r is not None and r.status_code == 200:
                log.info("vLLM-Qwen разбужен")
            self.slept_qwen = False

    async def restart(self):
        """Перезапуск зависшего сервера, не отпуская пользователей (они внутри use())."""
        async with self._lock:
            subprocess.run(["systemctl", "--user", "stop", self.unit], capture_output=True)
            await self._start()

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
        """Контекст: внутри сервер гарантированно поднят (или исключение)."""
        autostart = self.cfg.llm_autostart if autostart is None else autostart
        async with self.start_lock, self._lock:
            if not await self.is_up():
                if not autostart:
                    raise RuntimeError(f"{self.label} не запущен, а автозапуск выключен")
                await self._start()
            self.users += 1
        try:
            yield self
        finally:
            self.users -= 1
            self.last_used = time.monotonic()
            if self.started_by_us and (self._idle_task is None or self._idle_task.done()):
                self._idle_task = asyncio.create_task(self._idle_watch())
