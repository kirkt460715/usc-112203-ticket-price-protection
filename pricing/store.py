"""不可变记录仓：原始凭证只读，按 as_of 重建任意时点的可见事实。

关键约定
========
* 所有记录按业务时钟（挂牌轮次内的分钟）排列，``seen`` 是票务台收到凭证的时间。
* 带 ``supersedes`` 的补录库存不修改原记录，而是产生一个新版本；取数时以
  最新版本为准。更正凭证只有在 ``seen <= as_of`` 时才对当时的决策可见。
* 退款、补放等迟到凭证不能倒填：``as_of`` 视图严格按 ``seen`` 过滤。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

TABLES = (
    "quotes",
    "inventory",
    "journeys",
    "equivalence",
    "protection",
    "next_quotes",
    "refunds",
    "postings",
    "equivalence_notices",
    "provision_limits",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


@dataclass(frozen=True)
class InventoryPoint:
    """某个挂牌在某一分钟的库存事实（已按版本取最新）。"""

    offer: str
    minute: int
    available: int
    held: int
    channel_limit: int
    activity: str
    seen: int
    source_id: str
    supersedes: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer": self.offer,
            "minute": self.minute,
            "available": self.available,
            "held": self.held,
            "channel_limit": self.channel_limit,
            "activity": self.activity,
            "seen": self.seen,
            "source_id": self.source_id,
            "supersedes": self.supersedes,
        }


class RecordStore:
    """只读记录仓。初始化即校验 catalog 行数与哈希。"""

    def __init__(self, records_dir: str | Path):
        self.root = Path(records_dir)
        catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
        self._tables: dict[str, list[dict[str, Any]]] = {}
        for name, meta in catalog.items():
            raw = (self.root / f"{name}.jsonl").read_bytes()
            if sha256(raw).hexdigest() != meta["sha256"]:
                raise ValueError(f"{name}.jsonl 校验值与 catalog 不一致")
            rows = _read_jsonl(self.root / f"{name}.jsonl")
            if len(rows) != meta["rows"]:
                raise ValueError(f"{name}.jsonl 行数与 catalog 不一致")
            ids = [row["id"] for row in rows]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name}.jsonl 存在重复 id")
            self._tables[name] = rows
        missing = set(TABLES) - set(catalog)
        if missing:
            raise ValueError(f"catalog 缺少数据表: {sorted(missing)}")

    # ---- 基础取数 -------------------------------------------------------
    def table(self, name: str) -> list[dict[str, Any]]:
        """返回整张表（只读语义：调用方不应修改）。"""
        return list(self._tables[name])

    def quote(self, quote_id: str) -> dict[str, Any]:
        for row in self._tables["quotes"]:
            if row["id"] == quote_id:
                return row
        raise KeyError(quote_id)

    def next_quote(self, quote_id: str) -> dict[str, Any]:
        for row in self._tables["next_quotes"]:
            if row["id"] == quote_id:
                return row
        raise KeyError(quote_id)

    # ---- as_of 视图 -----------------------------------------------------
    def visible_inventory(self, as_of: int) -> list[InventoryPoint]:
        """as_of 时点可见的库存点。

        同一 (offer, minute) 若存在补录版本，仅当补录凭证 ``seen <= as_of``
        时覆盖原行；原行始终保留在表中，不被物理修改。
        """
        chosen: dict[tuple[str, int], dict[str, Any]] = {}
        for row in self._tables["inventory"]:
            if row["seen"] > as_of:
                continue
            key = (row["offer"], row["minute"])
            old = chosen.get(key)
            if old is None:
                chosen[key] = row
                continue
            # 版本裁决：带 supersedes 且指向当前版本的补录优先
            if row.get("supersedes") == old["id"]:
                chosen[key] = row
            elif old.get("supersedes") == row["id"]:
                continue
            else:
                raise ValueError(
                    f"库存点 {key} 存在无法裁决的多版本: {old['id']} / {row['id']}"
                )
        return [
            InventoryPoint(
                offer=row["offer"],
                minute=row["minute"],
                available=row["available"],
                held=row["held"],
                channel_limit=row["channel_limit"],
                activity=row["activity"],
                seen=row["seen"],
                source_id=row["id"],
                supersedes=row.get("supersedes"),
            )
            for row in chosen.values()
        ]

    def visible_refunds(self, as_of: int) -> list[dict[str, Any]]:
        """as_of 时点已收到的退款回执（迟到的退款不能倒填）。"""
        return [row for row in self._tables["refunds"] if row["seen"] <= as_of]

    def visible_equivalence_notices(self, as_of: int) -> list[dict[str, Any]]:
        """已公布的等效公告；受理中（pending）不产生等效效力。"""
        return [
            row
            for row in self._tables["equivalence_notices"]
            if row["state"] == "public"
            and row["announced"] is not None
            and row["announced"] <= as_of
        ]

    def visible_postings(self, as_of: int) -> list[dict[str, Any]]:
        return [row for row in self._tables["postings"] if row["sent"] <= as_of]

    def corrections(self) -> Iterable[dict[str, Any]]:
        """全部补录/更正凭证（不区分 as_of，供证据撤销分析）。"""
        return (row for row in self._tables["inventory"] if row.get("supersedes"))

    def inventory_row(self, row_id: str) -> dict[str, Any]:
        """按 id 取原始库存行（含已被补录取代的旧版本）。"""
        for row in self._tables["inventory"]:
            if row["id"] == row_id:
                return row
        raise KeyError(row_id)
