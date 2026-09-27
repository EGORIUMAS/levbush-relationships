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
            if not m["auto_fwd"] and m["sender_id"] is not None:
                seen.setdefault(m["sender_id"], None)
        return list(seen)


def _author(m):
    return None if m["auto_fwd"] else m["sender_id"]


def _cast_splits(msgs, window: int) -> list[int]:
    """Индексы внутри куска без больших перерывов, где полностью сменился состав."""
    authors = [_author(m) for m in msgs]
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


def segment(msgs, gap_min: int = 30, window: int = 15) -> list[Episode]:
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
        cuts = [0] + _cast_splits(run, window) + [len(run)]
        for a, b in zip(cuts, cuts[1:]):
            out.append(Episode(run[a:b]))
    return out


def is_closed(ep: Episode, now: int, gap_min: int = 30) -> bool:
    return now - ep.end >= gap_min * 60
