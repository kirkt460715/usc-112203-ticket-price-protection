"""挂牌台账：发令（sent）与生效（public_at/effective_minute）是两个事件。

同一条预备挂牌可能处于：
* 仅发令、无回执（seen/public_at 为空）——对外尚未生效，不产生保价义务；
* 已生效——按其含费挂牌价触发保价规则；
* 签准（next_quotes.state=approved）但从未发令——仍只是内部预备事件。

渠道报错“整点售罄”并在签准后更正的场景在这里落地：依据被撤销凭证签准的
挂牌，若尚未对外生效，可识别为“未执行的挂牌”，其原依据失效，不能再按
原依据执行；已经对外产生的价格保护按原合同处理，只在兑现证据中留痕。
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import RecordStore


@dataclass
class PostingState:
    quote_id: str
    state: str                 # next_quotes.state
    sent: int | None
    seen: int | None
    public_at: int | None
    effective_minute: int
    is_effective: bool
    is_sent_only: bool
    never_sent: bool


class PostingLedger:
    def __init__(self, store: RecordStore):
        self.store = store

    def states(self, as_of: int) -> dict[str, PostingState]:
        postings = {p["quote"]: p for p in self.store.visible_postings(as_of)}
        result: dict[str, PostingState] = {}
        for nq in self.store.table("next_quotes"):
            p = postings.get(nq["id"])
            effective = (
                p is not None
                and p["public_at"] is not None
                and p["public_at"] <= as_of
                and nq["effective_minute"] <= as_of
                and nq["state"] == "posted"
            )
            sent_only = (
                p is not None
                and (p["public_at"] is None or p["public_at"] > as_of)
            )
            result[nq["id"]] = PostingState(
                quote_id=nq["id"],
                state=nq["state"],
                sent=p["sent"] if p else None,
                seen=p["seen"] if p else None,
                public_at=p["public_at"] if p else None,
                effective_minute=nq["effective_minute"],
                is_effective=effective,
                is_sent_only=sent_only,
                never_sent=p is None,
            )
        return result

    def unexecuted_quotes(self, as_of: int) -> list[str]:
        """已签准但截至 as_of 未对外生效的挂牌 id。"""
        return [
            qid
            for qid, st in self.states(as_of).items()
            if not st.is_effective
        ]

    def effective_quotes(self, as_of: int) -> list[str]:
        return [
            qid for qid, st in self.states(as_of).items() if st.is_effective
        ]
