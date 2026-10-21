"""Merge the per-kind recommendation lists into one table, in batches of 10 with a 6:3:1 video:audio:mantra mix.

Pure logic: no Streamlit, Valkey or database access.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

BATCH_SIZE = 10
KIND_ORDER = ("videos", "audios", "mantras")
KIND_LABEL = {"videos": "Video", "audios": "Audio", "mantras": "Mantra"}
PARENT_COLUMN = {"videos": "Series", "audios": "Audio Research"}
COLUMNS = ["Position", "Batch", "Kind", "Rank", "Type", "Title", "Parent", "Characters", "Source", "source_code"]


@dataclass(frozen=True)
class Placement:
    position: int  # 1-based, across the whole table
    batch: int  # 1-based
    kind: str  # "videos" | "audios" | "mantras"
    index: int  # 0-based into that kind's list


def _mark_batch(remaining: dict[str, int], rng: random.Random) -> list[str | None]:
    """Decide which kind each of the batch's slots gets; blank slots are None."""
    slots: list[str | None] = [None] * BATCH_SIZE
    left = dict(remaining)

    def take(kind: str, positions: list[int]) -> None:
        for p in positions:
            slots[p] = kind
        left[kind] -= len(positions)

    def blanks(count: int) -> list[int]:
        return [p for p, kind in enumerate(slots) if kind is None][:count]

    take("videos", list(range(min(4, left["videos"]))))
    take("videos", sorted(rng.sample(range(4, BATCH_SIZE), min(2, left["videos"]))))
    take("audios", blanks(min(3, left["audios"])))
    take("mantras", blanks(min(1, left["mantras"])))
    for p in blanks(BATCH_SIZE):
        kind = next((k for k in KIND_ORDER if left[k] > 0), None)
        if kind is None:
            break
        take(kind, [p])
    return slots


def order(
    n_videos: int,
    n_audios: int,
    n_mantras: int,
    seed: str,
    rng_factory: Callable[[int], random.Random] | None = None,
) -> list[Placement]:
    """Placement of every item of the three lists, in table order.

    Each batch b draws its random video positions from rng_factory(b), by default
    random.Random(f"{seed}:{b}"), which is stable across processes (unlike hash()).
    """
    if rng_factory is None:
        rng_factory = lambda b: random.Random(f"{seed}:{b}")  # noqa: E731
    remaining = {"videos": n_videos, "audios": n_audios, "mantras": n_mantras}
    next_index = dict.fromkeys(KIND_ORDER, 0)
    placements: list[Placement] = []
    batch = 0
    while any(remaining.values()):
        batch += 1
        # Kinds are assigned to slots first and items to slots second, so a backfilled
        # item can never land above a lower-ranked item of its own kind.
        for kind in _mark_batch(remaining, rng_factory(batch)):
            if kind is None:
                continue
            placements.append(Placement(len(placements) + 1, batch, kind, next_index[kind]))
            next_index[kind] += 1
            remaining[kind] -= 1
    return placements


def build_table(frames: dict[str, pd.DataFrame], seed: str) -> pd.DataFrame:
    """One table from the per-kind fetch_top frames (empty or column-less frames allowed)."""
    frames = {kind: frames.get(kind, pd.DataFrame()) for kind in KIND_ORDER}
    placements = order(*(len(frames[kind]) for kind in KIND_ORDER), seed=seed)
    rows = []
    for p in placements:
        item = frames[p.kind].iloc[p.index]
        rows.append(
            {
                "Position": p.position,
                "Batch": p.batch,
                "Kind": KIND_LABEL[p.kind],
                "Rank": item["Rank"],
                "Type": item["Type"],
                "Title": item["Title"],
                "Parent": item[PARENT_COLUMN[p.kind]] if p.kind in PARENT_COLUMN else "",
                "Characters": item["Characters"],
                "Source": item["Source"],
                "source_code": item["source_code"],
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)
