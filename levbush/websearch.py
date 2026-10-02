"""Интернет для разговора с ботом: поиск через свой SearXNG (docker, 127.0.0.1:8890) и чтение страниц.

web_fetch открывает ссылки, которые модели могут подсунуть участники чата, поэтому ходит только на публичные адреса:
каждый адрес хоста (и после каждого редиректа) проверяется — локальные, частные, служебные запрещены (иначе можно было
бы прочитать vLLM на :8080, Jupyter, роутер). Тело — не больше WEB_FETCH_MAX_MB; HTML → текст (trafilatura),
PDF → pdftotext, текст и JSON — как есть.
"""
import asyncio
import ipaddress
import logging
import socket
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("levbush.websearch")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 "
      "levbush-bot")


class WebError(RuntimeError):
    pass


async def _check_host(url: str):
    """Только http(s) и только публичные адреса."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebError("нужна ссылка http(s)://…")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443,
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebError(f"нет такого сайта: {parts.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise WebError("локальные и служебные адреса открывать нельзя")


class Web:
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.web_search_url)

    async def search(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "Пустой запрос."
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{self.cfg.web_search_url}/search",
                                params={"q": query, "format": "json", "language": "auto", "safesearch": 0})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("поиск «%s»: %s", query, exc)
            return f"Поиск не работает: {exc}"
        out = []
        for a in data.get("answers") or []:
            text = a.get("answer") if isinstance(a, dict) else a
            if text:
                out.append(f"Быстрый ответ: {text}")
        for box in (data.get("infoboxes") or [])[:1]:
            if box.get("content"):
                out.append(f"Справка «{box.get('infobox')}»: {box['content'][:800]}")
        seen = set()
        for r in data.get("results") or []:
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            date = f" ({r['publishedDate'][:10]})" if r.get("publishedDate") else ""
            snippet = " ".join((r.get("content") or "").split())[:400]
            out.append(f"{len(seen)}. {r.get('title') or url}{date}\n   {url}\n   {snippet}")
            if len(seen) >= self.cfg.web_results:
                break
        return "\n".join(out) if out else "Ничего не нашлось."

    async def fetch(self, url: str) -> str:
        url = (url or "").strip()
        try:
            text, final, kind = await self._get(url)
        except WebError as exc:
            return f"Не открылось: {exc}"
        except httpx.HTTPError as exc:
            return f"Не открылось: {type(exc).__name__}: {exc}"
        limit = self.cfg.web_fetch_chars
        text = text.strip()
        more = f"\n…(обрезано: показано {limit} из {len(text)} знаков)" if len(text) > limit else ""
        return f"Страница {final} ({kind}):\n\n" + (text[:limit] + more if text else "(текста нет)")

    async def _get(self, url: str) -> tuple[str, str, str]:
        max_bytes = int(self.cfg.web_fetch_max_mb * 1024 * 1024)
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={"User-Agent": UA}) as c:
            for _ in range(6):                     # редиректы — вручную: адрес каждого проверяется
                await _check_host(url)
                async with c.stream("GET", url) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise WebError(f"HTTP {r.status_code}")
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            break
                    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                    return await asyncio.to_thread(self._text, bytes(body), ctype, r.encoding), str(r.url), ctype
        raise WebError("слишком много перенаправлений")

    @staticmethod
    def _text(body: bytes, ctype: str, encoding: str | None) -> str:
        if ctype == "application/pdf" or body[:5] == b"%PDF-":
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                f.write(body)
                f.flush()
                r = subprocess.run(["pdftotext", "-l", "20", "-layout", f.name, "-"], capture_output=True, timeout=60)
            return r.stdout.decode("utf-8", "replace")
        raw = body.decode(encoding or "utf-8", "replace")
        if "html" in ctype or raw.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
            import trafilatura
            text = trafilatura.extract(raw, favor_recall=True, include_tables=True, include_links=False,
                                       output_format="markdown")
            if text:
                title = trafilatura.extract_metadata(raw)
                return (f"# {title.title}\n\n" if title and title.title else "") + text
            return ""
        if ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
            return raw
        raise WebError(f"не текст ({ctype or 'неизвестный тип'})")
