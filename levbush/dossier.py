"""Досье и связи как структурированные записи + правки операциями (без переписывания целиком и без версий).

Досье: {"summary": str, "next": int, "names": ["Лёва", "Льва", "Левбуш"], "entries": [
    {"id": "e7", "section": "facts", "text": "…", "since": "2025-03-01", "changed": "2025-06-02" | null,
     "prev": "старый текст" | null, "msgs": [123], "certain": true, "removed": "2025-07-01" | null, "why": "…"}]}
Когда что появилось, изменилось или устарело — видно по датам прямо в досье.

Связь: {"how": str, "bond": str, "dynamics": str, "notes_changed": {section: date},
        "events": [{"date": "2025-03-01", "text": "…", "msgs": [123]}]}
"""
import re

from .render import link_msgs

SECTIONS = {"who": "Кто это", "facts": "Биография", "interests": "Интересы", "character": "Характер и манера общения",
            "quirks": "Мелочи и привычки", "role": "Роль в группе", "timeline": "Хронология"}
# на чём основана запись: сам сказал / косвенно (вывод из деталей) / со слов других
BASIS_MARK = {"косвенно": "косвенно", "со слов других": "со слов других"}
REL_NOTES = {"how": "Как общаются", "bond": "Что их связывает", "dynamics": "Динамика"}


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uff60]+")


def clean(text: str) -> str:
    """Qwen иногда вставляет китайские слова («…届时 будет 16») — вырезаем иероглифы, кану, хангыль."""
    return re.sub(r"\s{2,}", " ", _CJK.sub("", text or "")).strip()


def _basis(op: dict) -> str:
    if op.get("basis") in ("сам", "косвенно", "со слов других"):
        return op["basis"]
    return "сам" if op.get("certain", True) else "косвенно"


def _mark(e: dict, fmt: str) -> str:
    basis = e.get("basis") or ("сам" if e.get("certain", True) else "косвенно")
    return fmt.format(BASIS_MARK[basis]) if basis in BASIS_MARK else ""


def empty_dossier() -> dict:
    return {"summary": "", "next": 1, "names": [], "entries": []}


def norm_name(s: str) -> str:
    return (s or "").strip().lower().replace("ё", "е")


def empty_relation() -> dict:
    return {"how": "", "bond": "", "dynamics": "", "notes_changed": {}, "events": []}


# ------------------------------------------------------------ правки

def apply_person(data: dict, ops: dict, uid: int, day_of) -> int:
    """Применяет к досье uid правки из ответа шага. day_of(msgs) → дата YYYY-MM-DD. Возвращает число правок."""
    n = 0
    entries = {e["id"]: e for e in data["entries"]}
    for op in ops.get("add", []):
        if op.get("person") != uid or not op.get("text") or op.get("section") not in SECTIONS:
            continue
        eid = f"e{data['next']}"
        data["next"] += 1
        data["entries"].append({"id": eid, "section": op["section"], "text": clean(op["text"]),
                                "since": day_of(op.get("msgs")), "changed": None, "prev": None,
                                "msgs": op.get("msgs", [])[:8], "basis": _basis(op),
                                "certain": _basis(op) == "сам",
                                "removed": None, "why": None})
        n += 1
    for op in ops.get("update", []):
        e = entries.get(op.get("entry"))
        if op.get("person") != uid or e is None or e["removed"] or not op.get("text"):
            continue
        if clean(op["text"]) == e["text"]:
            continue
        e["prev"], e["text"] = e["text"], clean(op["text"])
        e["changed"] = day_of(op.get("msgs"))
        e["msgs"] = (op.get("msgs") or [])[:8] or e["msgs"]
        e["why"] = op.get("why") or None
        if op.get("basis"):
            e["basis"] = _basis(op)
            e["certain"] = e["basis"] == "сам"
        n += 1
    for op in ops.get("remove", []):
        e = entries.get(op.get("entry"))
        if op.get("person") != uid or e is None or e["removed"]:
            continue
        e["removed"] = day_of(op.get("msgs"))
        e["why"] = op.get("why") or None
        n += 1
    for op in ops.get("summaries", []):
        if op.get("person") == uid and op.get("text"):
            data["summary"] = clean(op["text"])[:300]
            n += 1
    names = data.setdefault("names", [])
    for op in ops.get("names", []):
        name = (op.get("name") or "").strip()
        if op.get("person") == uid and 2 <= len(name) <= 40 and norm_name(name) not in map(norm_name, names):
            names.append(name)
            n += 1
    return n


BASES = ("сам", "косвенно", "со слов других")


def compact_person(data: dict, out: dict, today: str) -> int:
    """Сжатие досье: новые записи из ответа заменяют старые, из которых собраны (from); dropped — выброшены,
    не упомянутые нигде — остаются как были. Старые записи уходят в data["archive"]. Возвращает, сколько стало
    записей меньше (0 — ответ негодный, досье не тронуто)."""
    live = {e["id"]: e for e in data["entries"] if not e["removed"]}
    used, new = set(), []
    for item in out.get("entries", []):
        src = [live[i] for i in dict.fromkeys(item.get("from") or []) if i in live and i not in used]
        if not src or not item.get("text") or item.get("section") not in SECTIONS:
            continue                                      # запись ни из чего — выдумка
        used.update(e["id"] for e in src)
        basis = item["basis"] if item.get("basis") in BASES else _basis(src[0])
        new.append({"id": "", "section": item["section"], "text": clean(item["text"]),
                    "since": min(e["since"] for e in src), "changed": None, "prev": None,
                    "msgs": list(dict.fromkeys(m for e in src for m in e["msgs"]))[:8], "basis": basis,
                    "certain": basis == "сам", "removed": None, "why": None})
    dropped = {i for i in out.get("dropped", []) if i in live} - used
    kept = [e for i, e in live.items() if i not in used and i not in dropped]
    if len(new) + len(kept) > len(live) * 0.9:           # почти не сжалось — не трогаем
        return 0
    for e in new:
        e["id"] = f"e{data['next']}"
        data["next"] += 1
    data.setdefault("archive", []).append({"at": today, "entries": [live[i] for i in live if i in used | dropped]})
    data["entries"] = [e for e in data["entries"] if e["removed"]] + kept + new
    return len(live) - len(new) - len(kept)


def apply_relation(data: dict, ops: dict, a: int, b: int, day_of) -> int:
    n = 0
    pair = {a, b}
    for op in ops.get("relation_events", []):
        if {op.get("a"), op.get("b")} == pair and op.get("text"):
            data["events"].append({"date": day_of(op.get("msgs")), "text": clean(op["text"]),
                                   "msgs": op.get("msgs", [])[:6]})
            n += 1
    for op in ops.get("relation_notes", []):
        if {op.get("a"), op.get("b")} == pair and op.get("section") in REL_NOTES and op.get("text"):
            data[op["section"]] = clean(op["text"])
            data.setdefault("notes_changed", {})[op["section"]] = day_of(op.get("msgs"))
            n += 1
    data["events"].sort(key=lambda e: e["date"])
    return n


# ------------------------------------------------------------ для нейросети (с id записей)

def prompt_person(data: dict | None, ids: bool = True) -> str:
    """ids=False — без номеров записей (для разговора: править досье там нельзя)."""
    if not data or not (data["entries"] or data.get("names")):
        return "Досье пока нет."
    out = []
    if data.get("names"):
        out.append("Как называют: " + ", ".join(data["names"]))
    if data.get("summary"):
        out.append(f"Кратко: {data['summary']}")
    for key, title in SECTIONS.items():
        items = [e for e in data["entries"] if e["section"] == key and not e["removed"]]
        if not items:
            continue
        out.append(f"{title}:")
        for e in items:
            mark = _mark(e, " ({})")
            when = f"с {e['since']}" + (f", изм. {e['changed']}" if e["changed"] else "")
            num = f"[{e['id']}] " if ids else ""
            out.append(f"  {num}{e['text']}{mark} ({when})")
    return "\n".join(out)


def prompt_relation(row, data: dict | None, max_events: int = 25) -> str:
    out = [f"тип: {row['kind'] or '—'}, тон: {row['tone'] or '—'}, близость {round((row['llm_score'] or 0) * 10)}/10"
           + (f"; кратко: {row['summary']}" if row["summary"] else "")]
    if data:
        for key, title in REL_NOTES.items():
            if data.get(key):
                out.append(f"{title}: {data[key]}")
        ev = data.get("events", [])
        if len(ev) > max_events:
            out.append(f"(ещё {len(ev) - max_events} более ранних событий)")
        for e in ev[-max_events:]:
            out.append(f"- {e['date']}: {e['text']}")
    return "\n".join(out)


# ------------------------------------------------------------ для людей (Markdown, сайт и бот)

def _refs(msgs) -> str:
    return " " + " ".join(f"[#{m}](msg:{m})" for m in msgs[:3]) if msgs else ""


def render_person(data: dict, chat: dict) -> str:
    out = [f"**Как называют:** {', '.join(data['names'])}", ""] if data.get("names") else []
    for key, title in SECTIONS.items():
        items = [e for e in data["entries"] if e["section"] == key]
        if not items:
            continue
        out.append(f"## {title}")
        items.sort(key=lambda e: (e["removed"] is not None, e["since"] if key == "timeline" else ""))
        for e in items:
            guess = _mark(e, " *({})*")
            # прежний текст (e["prev"]) хранится в данных как история, но в досье не выводится
            upd = f", обновлено {e['changed']}" if e["changed"] else ""
            if key == "timeline":
                line = f"- **{e['since']}** — {e['text']}{guess}" + (f" *(обновлено {e['changed']})*" if upd else "")
            else:
                line = f"- {e['text']}{guess} — *с {e['since']}{upd}*"
            if e["removed"]:
                line = f"- ~~{e['text']}~~ — *устарело {e['removed']}" + (f": {e['why']}" if e["why"] else "") + "*"
            out.append(line + _refs(e["msgs"]))
        out.append("")
    return link_msgs("\n".join(out).strip(), chat)


def render_relation(data: dict, chat: dict) -> str:
    out = []
    changed = data.get("notes_changed", {})
    for key in ("how", "bond"):
        if data.get(key):
            out += [f"## {REL_NOTES[key]}", data[key] + (f" *(на {changed[key]})*" if changed.get(key) else ""), ""]
    if data.get("events"):
        out.append("## История")
        out += [f"- **{e['date']}** — {e['text']}{_refs(e['msgs'])}" for e in data["events"]]
        out.append("")
    if data.get("dynamics"):
        out += [f"## {REL_NOTES['dynamics']}", data["dynamics"] + (f" *(на {changed['dynamics']})*"
                                                                  if changed.get("dynamics") else "")]
    return link_msgs("\n".join(out).strip(), chat)
