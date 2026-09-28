"""OpenAI-совместимый клиент к vLLM: текст + картинки/видео/звук (file://), ответ по JSON-схеме."""
import asyncio
import json
import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger("levbush.llm")


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def image(path) -> dict:
    return {"type": "image_url", "image_url": {"url": Path(path).resolve().as_uri()}}


def video(path) -> dict:
    return {"type": "video_url", "video_url": {"url": Path(path).resolve().as_uri()}}


def audio(path) -> dict:
    return {"type": "audio_url", "audio_url": {"url": Path(path).resolve().as_uri()}}


class LLMError(RuntimeError):
    pass


def parse_json(raw: str):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except ValueError:
            pass
    raise LLMError(f"ответ не JSON: {raw[:300]}")


class LLM:
    def __init__(self, base: str, model: str, timeout: float = 1800):
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._schema_ok = True

    async def chat(self, messages, schema: dict | None = None, *, max_tokens: int = 4096, temperature: float = 0.3,
                   think: bool = False, think_budget: int = 0, audio_in_video: bool = False, retries: int = 2):
        """think_budget — сколько токенов модель может рассуждать; max_tokens — на сам ответ (бюджет добавляется)."""
        body = {"model": self.model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens + (think_budget if think else 0),
                "chat_template_kwargs": {"enable_thinking": think},
                "repetition_penalty": 1.05}   # против зацикливания; пробелы в JSON запрещены на сервере
        if think and think_budget:
            # мягко — подсказкой в шаблоне модели, жёстко — ограничением vLLM (потом рассуждение закрывается)
            body["chat_template_kwargs"]["reasoning_budget"] = think_budget
            body["thinking_token_budget"] = think_budget
        if audio_in_video:
            body["mm_processor_kwargs"] = {"use_audio_in_video": True}
        if schema is not None and self._schema_ok:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "result", "schema": schema, "strict": True}}
        last = None
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.post(f"{self.base}/v1/chat/completions", json=body)
                if r.status_code == 400 and "response_format" in body and "response_format" in r.text:
                    log.warning("сервер не принял response_format — прошу JSON текстом")
                    self._schema_ok = False
                    body.pop("response_format")
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
                choice = r.json()["choices"][0]
                content = choice["message"].get("content") or ""
                if choice.get("finish_reason") == "length" and schema is not None:
                    # почти всегда это зацикливание: повторяем холоднее, лимит не раздуваем
                    body["temperature"] = max(0.0, body["temperature"] - 0.2)
                    raise LLMError("ответ обрезан по max_tokens (зацикливание?)")
                return parse_json(content) if schema is not None else content.strip()
            except (httpx.HTTPError, LLMError, KeyError) as exc:
                last = exc
                log.warning("LLM, попытка %d: %s", attempt + 1, exc)
                if isinstance(exc, LLMError) and "HTTP 400" in str(exc):
                    break
                await asyncio.sleep(3 * (attempt + 1))
        raise LLMError(str(last))
