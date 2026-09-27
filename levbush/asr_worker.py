#!/usr/bin/env python3
"""Пакетная расшифровка Parakeet-TDT 0.6B v3 (запускается системным python3, где стоят transformers/torch).

stdin — строки JSON {"id": …, "path": "…"}; stdout — строки JSON {"id": …, "text": "…", "seconds": …}
или {"id": …, "error": "…"}. Модель грузится один раз; код распознавания — из ~/scripts/asr.py.
"""
import importlib.util
import json
import sys
from pathlib import Path

ASR_PY = Path.home() / "scripts/asr.py"


def load_asr():
    spec = importlib.util.spec_from_file_location("asr", ASR_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    asr = load_asr()

    def die(msg):          # в asr.py die() делает sys.exit — для пачки это слишком
        raise RuntimeError(msg)

    asr.die = die
    rec = asr.Recognizer("auto")
    print(json.dumps({"ready": True, "device": rec.device}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        job = json.loads(line)
        try:
            text, seconds = asr.transcribe(rec, Path(job["path"]))
            out = {"id": job["id"], "text": text, "seconds": round(seconds, 1)}
        except Exception as exc:  # noqa: BLE001 — один плохой файл не останавливает пачку
            out = {"id": job["id"], "error": str(exc)[:500]}
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
