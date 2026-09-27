"""Нарезка переписки на эпизоды (беседы).

Граница эпизода:
  1. перерыв ≥ GAP_MIN минут между соседними сообщениями;
  2. полная смена действующих лиц: авторы последних CAST_WINDOW сообщений не пересекаются
     с авторами следующих CAST_WINDOW (обе части не короче окна).
Автопересылки постов канала в состав участников не входят.
"""
from dataclasses import dataclass, field


@dataclass
class Episode:
    msgs: list = field(default_factory=list)
    alias: dict = field(default_factory=dict)     # от имени группы/канала → человек

    @property
    def id(self) -> int:
        return self.msgs[0]["id"]

    @property
    def last_id(self) -> int:
        return self.msgs[-1]["id"]

    @property
    def start(self) -> int:
        return self.msgs[0]["date"]

    @property
    def end(self) -> int:
        return self.msgs[-1]["date"]

    @property
    def participants(self) -> list[int]:
        seen = {}
        for m in self.msgs:
            a = _author(m, self.alias)
            if a is not None:
                seen.setdefault(a, None)
        return list(seen)


def _author(m, alias=None):
    if m["auto_fwd"]:
        return None
    s = m["sender_id"]
    return alias.get(s, s) if alias else s


def _cast_splits(msgs, window: int, alias=None) -> list[int]:
    """Индексы внутри куска без больших перерывов, где полностью сменился состав."""
    authors = [_author(m, alias) for m in msgs]
    n = len(msgs)
    splits = []
    last = 0
    i = window
    while i <= n - window:
        if i - last >= window:
            before = {a for a in authors[i - window:i] if a is not None}
            after = {a for a in authors[i:i + window] if a is not None}
            if before and after and not (before & after):
                splits.append(i)
                last = i
                i += window
                continue
        i += 1
    return splits


def segment(msgs, gap_min: int = 30, window: int = 15, alias: dict | None = None) -> list[Episode]:
    """msgs — строки сообщений без служебных, по возрастанию даты."""
    if not msgs:
        return []
    gap = gap_min * 60
    runs, cur = [], [msgs[0]]
    for prev, m in zip(msgs, msgs[1:]):
        if m["date"] - prev["date"] >= gap:
            runs.append(cur)
            cur = []
        cur.append(m)
    runs.append(cur)
    out = []
    for run in runs:
        cuts = [0] + _cast_splits(run, window, alias) + [len(run)]
        for a, b in zip(cuts, cuts[1:]):
            out.append(Episode(run[a:b], alias or {}))
    return out


def is_closed(ep: Episode, now: int, gap_min: int = 30) -> bool:
    return now - ep.end >= gap_min * 60
