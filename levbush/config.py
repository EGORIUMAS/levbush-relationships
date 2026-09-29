"""Настройки из ~/.config/levbush.env (или переменных окружения LEVBUSH_*)."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ENV_FILE = Path(os.environ.get("LEVBUSH_ENV", Path.home() / ".config/levbush.env"))
ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


_FILE = _load_env_file(ENV_FILE)


def env(name: str, default=None):
    key = "LEVBUSH_" + name
    return os.environ.get(key, _FILE.get(key, default))


def env_int(name: str, default: int) -> int:
    value = env(name)
    return int(value) if value not in (None, "") else default


def env_float(name: str, default: float) -> float:
    value = env(name)
    return float(value) if value not in (None, "") else default


def env_bool(name: str, default: bool) -> bool:
    value = env(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "yes", "true", "on", "да")


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value))


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: env("BOT_TOKEN", ""), repr=False)
    admin_id: int = field(default_factory=lambda: env_int("ADMIN_ID", 0))
    group: str = field(default_factory=lambda: env("GROUP", ""))          # @username / id / ссылка; канал → его группа
    api_id: int = field(default_factory=lambda: env_int("API_ID", 0))
    api_hash: str = field(default_factory=lambda: env("API_HASH", ""), repr=False)
    database_url: str = field(default_factory=lambda: env("DATABASE_URL", ""), repr=False)
    webapp_url: str = field(default_factory=lambda: env("WEBAPP_URL", ""))
    # чьи сообщения от имени группы/канала: "-100…=@ник,-100…=@ник" (аноним-админ, пост от канала)
    aliases: str = field(default_factory=lambda: env("ALIASES", ""))
    webapp_name: str = field(default_factory=lambda: env("WEBAPP_NAME", ""))       # короткое имя Mini App (/newapp)
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(env("TZ", "Europe/Moscow")))

    data_dir: Path = field(default_factory=lambda: _path(env("DATA_DIR", "~/.local/share/levbush")))
    media_dir: Path = field(default_factory=lambda: _path(env("MEDIA_DIR", "/mnt/shared/levbush/media")))
    session_file: Path = field(default_factory=lambda: _path(env("SESSION", "~/.config/levbush/telethon")))
    # паузы между запросами Telethon, с (аккуратно с лимитами: аккаунт живой)
    tg_history_delay: float = field(default_factory=lambda: env_float("TG_HISTORY_DELAY", 1.0))    # на 100 сообщений
    tg_reaction_delay: float = field(default_factory=lambda: env_float("TG_REACTION_DELAY", 1.5))
    tg_media_delay: float = field(default_factory=lambda: env_float("TG_MEDIA_DELAY", 0.7))
    tg_profile_delay: float = field(default_factory=lambda: env_float("TG_PROFILE_DELAY", 2.0))
    media_max_mb: int = field(default_factory=lambda: env_int("MEDIA_MAX_MB", 300))

    # хранение периодов
    keep_days: int = field(default_factory=lambda: env_int("KEEP_DAYS", 30))
    keep_weeks: int = field(default_factory=lambda: env_int("KEEP_WEEKS", 12))
    keep_months: int = field(default_factory=lambda: env_int("KEEP_MONTHS", 12))

    # эпизоды и сессии
    gap_min: int = field(default_factory=lambda: env_int("GAP_MIN", 30))            # перерыв, делящий беседы
    cast_window: int = field(default_factory=lambda: env_int("CAST_WINDOW", 15))    # окно смены «действующих лиц»

    # нейросеть
    llm_url: str = field(default_factory=lambda: env("LLM_URL", "http://127.0.0.1:18090").rstrip("/"))
    llm_model: str = field(default_factory=lambda: env("LLM_MODEL", ""))           # пусто — первая из /v1/models
    llm_autostart: bool = field(default_factory=lambda: env_bool("LLM_AUTOSTART", True))
    llm_model_path: str = field(default_factory=lambda: env(
        "LLM_MODEL_PATH", "/mnt/shared/Models/nemotron3-nano-omni-30b-a3b-nvfp4"))
    llm_ctx: int = field(default_factory=lambda: env_int("LLM_CTX", 262144))   # предел Nemotron 3 Nano Omni — 256k
    llm_seqs: int = field(default_factory=lambda: env_int("LLM_SEQS", 4))
    llm_parallel: int = field(default_factory=lambda: env_int("LLM_PARALLEL", 4))  # одновременных запросов на медиа
    llm_idle_stop_min: int = field(default_factory=lambda: env_int("LLM_IDLE_STOP_MIN", 15))
    llm_need_gib: float = field(default_factory=lambda: env_float("LLM_NEED_GIB", 28.0))
    llm_think: bool = field(default_factory=lambda: env_bool("LLM_THINK", True))             # рассуждение Nemotron
    llm_think_budget: int = field(default_factory=lambda: env_int("LLM_THINK_BUDGET", 8192))  # токенов на рассуждение
    # общий сервер пользователя (run-qwen.sh: прокси автовыгрузки :8080 → vllm :18081) — пересказ сначала идёт туда
    fallback_llm_url: str = field(default_factory=lambda: env("FALLBACK_LLM_URL", "http://127.0.0.1:8080").rstrip("/"))
    qwen_port: int = field(default_factory=lambda: env_int("QWEN_PORT", 18081))    # его vllm — усыпить ради VRAM
    # Qwen для разбора и пересказа (свой юнит levbush-qwen): Nemotron слабее в досье, он описывает звук и видео
    qwen_url: str = field(default_factory=lambda: env("QWEN_URL", "http://127.0.0.1:18091").rstrip("/"))
    qwen_model_path: str = field(default_factory=lambda: env("QWEN_MODEL_PATH", "/mnt/shared/Models/qwen38-27b-nvfp4"))
    qwen_name: str = field(default_factory=lambda: env("QWEN_NAME", "qwen3.8-27b"))
    qwen_ctx: int = field(default_factory=lambda: env_int("QWEN_CTX", 131072))
    qwen_util: float = field(default_factory=lambda: env_float("QWEN_UTIL", 0.92))
    # 1 слот: при двух запросах с картинками и MTP vLLM 0.27.1 падает (cudaErrorIllegalAddress); пересказ во время
    # разбора встаёт в очередь за текущим шагом
    qwen_seqs: int = field(default_factory=lambda: env_int("QWEN_SEQS", 1))
    qwen_mtp: int = field(default_factory=lambda: env_int("QWEN_MTP", 3))          # 0 — без MTP; с MTP только fp8 KV
    qwen_need_gib: float = field(default_factory=lambda: env_float("QWEN_NEED_GIB", 29.0))   # 0,92 × 31,4
    video_frames: int = field(default_factory=lambda: env_int("VIDEO_FRAMES", 4))  # кадров видео/GIF для Qwen
    qwen_images: int = field(default_factory=lambda: env_int("QWEN_IMAGES", 40))   # картинок в одном запросе к Qwen
    # шаг разбора: несколько последовательных окон + контекст + текущие досье и связи
    # новые окна: текст + медиа; с досье, связями и контекстом за 48 ч весь промпт ~100–105k (Qwen: контекст 131k)
    step_tokens: int = field(default_factory=lambda: env_int("STEP_TOKENS", 80000))
    step_context_chars: int = field(default_factory=lambda: env_int("STEP_CONTEXT_CHARS", 30000))  # уже разобранные
    step_relations_chars: int = field(default_factory=lambda: env_int("STEP_RELATIONS_CHARS", 30000))
    step_max_windows: int = field(default_factory=lambda: env_int("STEP_MAX_WINDOWS", 8))
    step_max_discussed: int = field(default_factory=lambda: env_int("STEP_MAX_DISCUSSED", 6))  # обсуждаемые заочно — с досье
    step_max_people: int = field(default_factory=lambda: env_int("STEP_MAX_PEOPLE", 10))
    # досье разрослось (записей или знаков больше порога) — Qwen сжимает его в биографию перед шагом;
    # после сжатия обычно 40–55 записей и 7–13 тыс. знаков — порог с запасом, чтобы не пережимать каждые пару шагов
    dossier_max_entries: int = field(default_factory=lambda: env_int("DOSSIER_MAX_ENTRIES", 90))
    dossier_max_chars: int = field(default_factory=lambda: env_int("DOSSIER_MAX_CHARS", 20000))
    # медиа в запросе: лимиты (= --limit-mm-per-prompt сервера) и оценка токенов для планирования
    mm_images: int = field(default_factory=lambda: env_int("MM_IMAGES", 24))
    mm_videos: int = field(default_factory=lambda: env_int("MM_VIDEOS", 6))
    mm_audio: int = field(default_factory=lambda: env_int("MM_AUDIO", 12))
    tok_image: int = field(default_factory=lambda: env_int("TOK_IMAGE", 1100))   # если размер картинки не прочитать
    tok_audio_sec: float = field(default_factory=lambda: env_float("TOK_AUDIO_SEC", 13))
    tok_video_frame: int = field(default_factory=lambda: env_int("TOK_VIDEO_FRAME", 90))    # замер: 76–93
    video_fps: float = field(default_factory=lambda: env_float("VIDEO_FPS", 2))
    video_max_frames: int = field(default_factory=lambda: env_int("VIDEO_MAX_FRAMES", 128))
    context_hours: int = field(default_factory=lambda: env_int("CONTEXT_HOURS", 48))   # «два последних дня»
    pdf_pages: int = field(default_factory=lambda: env_int("PDF_PAGES", 8))           # сколько страниц PDF показывать
    video_max_sec: int = field(default_factory=lambda: env_int("VIDEO_MAX_SEC", 600))

    asr_python: str = field(default_factory=lambda: env("ASR_PYTHON", "/usr/bin/python3"))

    # смысловой поиск /find: индекс в search.db рядом с cache.db (переписка — только локально)
    search: bool = field(default_factory=lambda: env_bool("SEARCH", True))
    embed_model: str = field(default_factory=lambda: env(
        "EMBED_MODEL", "/mnt/shared/Models/jina-v5-omni-small-retrieval"))
    embed_python: str = field(default_factory=lambda: env("EMBED_PYTHON", env("ASR_PYTHON", "/usr/bin/python3")))
    embed_dim: int = field(default_factory=lambda: env_int("EMBED_DIM", 1024))        # Matryoshka: 256/512/768/1024
    # индексация: auto — GPU, если очередь большая и видеокарта свободна (нет модели бота, генерации H3, хватает
    # VRAM), иначе CPU; cpu / cuda — принудительно (но и cuda при модели бота, H3 или нехватке VRAM идёт на CPU)
    embed_device: str = field(default_factory=lambda: env("EMBED_DEVICE", "auto"))
    embed_need_gib: float = field(default_factory=lambda: env_float("EMBED_NEED_GIB", 6.0))
    embed_gpu_min: int = field(default_factory=lambda: env_int("EMBED_GPU_MIN", 2000))  # меньше — на CPU
    embed_threads: int = field(default_factory=lambda: env_int("EMBED_THREADS", 8))     # потоков torch на CPU
    embed_max_pixels: int = field(default_factory=lambda: env_int("EMBED_MAX_PIXELS", 1310720))
    embed_idle_min: int = field(default_factory=lambda: env_int("EMBED_IDLE_MIN", 15))  # эмбеддер запросов
    search_interval: int = field(default_factory=lambda: env_int("SEARCH_INTERVAL", 300))
    # фоновая дозаливка векторов — только пока очередь не больше этого; первичную (вся история) — /index run
    search_auto_max: int = field(default_factory=lambda: env_int("SEARCH_AUTO_MAX", 3000))
    find_per_page: int = field(default_factory=lambda: env_int("FIND_PER_PAGE", 7))

    daily_at: str = field(default_factory=lambda: env("DAILY_AT", "04:30"))
    stats_interval: int = field(default_factory=lambda: env_int("STATS_INTERVAL", 120))
    retell_tokens: int = field(default_factory=lambda: env_int("RETELL_TOKENS", 240000))  # один запрос пересказа (ctx 256k)
    retell_think_budget: int = field(default_factory=lambda: env_int("RETELL_THINK_BUDGET", 1024))  # 0 — без рассуждения
    retell_max_hours: int = field(default_factory=lambda: env_int("RETELL_MAX_HOURS", 48))

    @property
    def cache_db(self) -> Path:
        return self.data_dir / "cache.db"

    @property
    def search_db(self) -> Path:
        return self.data_dir / "search.db"

    def ensure_dirs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.session_file.parent, 0o700)


cfg = Config()
