#!/usr/bin/env python3
"""Эмбеддинги jina-embeddings-v5-omni-small (запускается системным python3, где стоят transformers/torch).

  embed_worker.py --model DIR --device cpu|cuda --modality text|vision [--dim 1024] [--threads 8]

stdin — строка JSON на пачку: {"items": [{"text": "…", "role": "query"|"document"} | {"image": "путь"}, …]};
stdout — первой строкой {"ready": true, …}, дальше на каждую пачку {"vecs": [base64 float32 | null, …],
"errors": {индекс: "текст"}}. Вектора L2-нормированы, обрезаны до --dim (Matryoshka). Видео (путь .mp4 и т.п.
в "image") — --frames кадров равномерно по ролику, каждый как картинка; векторы кадров идут подряд одной строкой.

Текст и картинки — в одном пространстве («locked aligned towers»: текстовая башня = jina-embeddings-v5-text-small),
поэтому текстовый запрос ищет и по сообщениям, и по фото. Текст — как в custom_st.py модели: «Query: …» /
«Document: …» без шаблона чата; картинка — «Document: <картинка>» в шаблоне чата. Пулинг — последний токен.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--device", default="cpu")
    p.add_argument("--modality", default="text", choices=["text", "vision"])
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--max-tokens", type=int, default=1024)        # длиннее — обрезается (сообщения короткие)
    p.add_argument("--max-pixels", type=int, default=1310720)     # как у модели: 256–1280 токенов на картинку
    p.add_argument("--batch-tokens", type=int, default=16384)     # токенов (с паддингом) в одном прогоне текста
    p.add_argument("--frames", type=int, default=6)                # кадров на видео
    a = p.parse_args()

    # протокол — в исходный stdout; всё, что печатают библиотеки (прогресс загрузки, предупреждения), — в stderr
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    if a.device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""                   # на CPU GPU не трогаем вовсе (даже контекст CUDA)
    sys.modules["vllm"] = None      # modeling_*.py модели при импорте регистрирует себя в vLLM — он тут не нужен
    import torch
    from PIL import Image
    from transformers import AutoModel, AutoProcessor, AutoTokenizer

    torch.set_num_threads(a.threads)
    t0 = time.time()
    dtype = torch.bfloat16
    model = AutoModel.from_pretrained(a.model, trust_remote_code=True, modality=a.modality, dtype=dtype).eval()
    model.to(a.device)
    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True)
    tok.padding_side = "right"                                    # пулинг берёт attention_mask.sum() - 1
    proc = None
    if a.modality == "vision":
        proc = AutoProcessor.from_pretrained(a.model, trust_remote_code=True, min_pixels=262144,
                                             max_pixels=a.max_pixels)
    print(json.dumps({"ready": True, "device": a.device, "load_sec": round(time.time() - t0, 1)}), file=proto, flush=True)

    def pool(hidden, mask):
        idx = mask.sum(dim=1) - 1
        v = hidden[torch.arange(hidden.shape[0], device=hidden.device), idx][:, :a.dim]
        return torch.nn.functional.normalize(v.float(), dim=-1)

    def positions(mask):
        pos = mask.long().cumsum(-1) - 1
        return pos.masked_fill(mask == 0, 0).unsqueeze(0).expand(3, -1, -1).contiguous()

    def enc(v) -> str:
        return base64.b64encode(v.cpu().numpy().astype("<f4").tobytes()).decode()

    @torch.inference_mode()
    def texts(batch: list[tuple[int, str]], out: list):
        """Пачка текстов: по длине, чтобы паддинг был поменьше, и не больше batch_tokens за прогон."""
        ids = tok([t for _, t in batch], truncation=True, max_length=a.max_tokens)["input_ids"]
        order = sorted(range(len(batch)), key=lambda i: len(ids[i]))
        k = 0
        while k < len(order):
            n = 1
            while k + n < len(order) and len(ids[order[k + n]]) * (n + 1) <= a.batch_tokens:
                n += 1
            chunk = order[k:k + n]
            enc_in = tok.pad({"input_ids": [ids[i] for i in chunk]}, return_tensors="pt")
            mask = enc_in["attention_mask"].to(a.device)
            hidden = model(input_ids=enc_in["input_ids"].to(a.device), attention_mask=mask,
                           position_ids=positions(mask)).last_hidden_state
            for i, v in zip(chunk, pool(hidden, mask)):
                out[batch[i][0]] = enc(v)
            k += n

    def frames(path: str, side: int = 512) -> list:
        """Кадры равномерно по ролику (одним вызовом ffmpeg), вписанные в квадрат side×side."""
        info = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
                              capture_output=True, text=True, timeout=60).stdout
        dur = float((json.loads(info or "{}").get("format") or {}).get("duration") or 1)
        vf = (f"fps={a.frames / max(dur, 0.1):.4f},scale={side}:{side}:force_original_aspect_ratio=decrease,"
              f"pad={side}:{side}:(ow-iw)/2:(oh-ih)/2")
        raw = subprocess.run(["ffmpeg", "-v", "quiet", "-i", path, "-an", "-vf", vf, "-frames:v", str(a.frames),
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=300).stdout
        n = len(raw) // (side * side * 3)
        if not n:
            raise ValueError("ffmpeg не отдал ни одного кадра")
        return [Image.frombuffer("RGB", (side, side), raw[i * side * side * 3:(i + 1) * side * side * 3])
                for i in range(n)]

    @torch.inference_mode()
    def image(path: str):
        if path.lower().endswith(VIDEO_EXT):
            vecs = [picture(f) for f in frames(path)]
            return base64.b64encode(b"".join(base64.b64decode(v) for v in vecs)).decode()
        img = Image.open(path)
        img.load()
        return picture(img)

    @torch.inference_mode()
    def picture(img):
        if img.mode != "RGB":
            img = img.convert("RGB")
        prompt = proc.apply_chat_template(
            [{"role": "user", "content": "Document: <|vision_start|><|image_pad|><|vision_end|>"}],
            tokenize=False, add_generation_prompt=False)
        inputs = proc(images=img, text=prompt, return_tensors="pt", truncation=False)
        inputs = {k: v.to(a.device) for k, v in inputs.items() if torch.is_tensor(v)}
        inputs["position_ids"] = positions(inputs["attention_mask"])
        hidden = model(**inputs).last_hidden_state
        return enc(pool(hidden, inputs["attention_mask"])[0])

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        items = json.loads(line)["items"]
        out, errors = [None] * len(items), {}
        batch = []
        for i, it in enumerate(items):
            if "text" in it:
                prefix = "Query: " if it.get("role") == "query" else "Document: "
                batch.append((i, prefix + it["text"]))
            elif "image" in it:
                if proc is None:
                    errors[i] = "картинки — только с --modality vision"
                    continue
                try:
                    out[i] = image(it["image"])
                except Exception as exc:  # noqa: BLE001 — одна битая картинка не останавливает пачку
                    errors[i] = f"{type(exc).__name__}: {exc}"[:300]
        if batch:
            try:
                texts(batch, out)
            except Exception as exc:  # noqa: BLE001
                for i, _ in batch:
                    errors[i] = f"{type(exc).__name__}: {exc}"[:300]
        print(json.dumps({"vecs": out, "errors": errors}), file=proto, flush=True)


if __name__ == "__main__":
    main()
