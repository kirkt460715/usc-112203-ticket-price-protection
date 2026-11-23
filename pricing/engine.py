"""轮次决策引擎：把证据、敏感度、保价、发布台账与准备额约束合并成可发布方案。

输出回答运营的四个问题
======================
1. 哪些候选价档有可信依据（准入判定 + 依据/反转因素）；
2. 同时可发布的一组价档（不能把单渠道各自可行的价档全部放出）；
3. 每组对原订单的新增保价承诺、仍需保留的准备额与财务上限对比；
4. 每个价档可追溯到的人群、净收入区间与应补款范围。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .evidence import build_evidence
from .posting import PostingLedger
from .protection import ProtectionEngine
from .sensitivity import (
    ADMIT_CONFOUNDED,
    ADMIT_NO_EQUIVALENCE,
    ADMIT_SUPPORTED,
    ADMIT_UNOBSERVED,
    build_groups,
    evaluate_candidate,
)
from .store import RecordStore

# 准入状态到是否允许进入发布组合的映射
BLOCKED_ADMISSIONS = {
    ADMIT_UNOBSERVED,
    ADMIT_NO_EQUIVALENCE,
    ADMIT_CONFOUNDED,
}


@dataclass
class BundlePlan:
    quote_ids: tuple[str, ...]
    incremental_cents: int          # 相对当前已生效义务的新增保价承诺
    total_union_cents: int          # 组合下全部保价义务（去重后）
    within_provision: bool
    headroom_after_cents: int
    per_quote_increment: dict[str, int]
    liabilities_added: int
    notes: list[str] = field(default_factory=list)


@dataclass
class RoundPlan:
    as_of: int
    candidate_verdicts: dict
    current_effective: list[str]
    current_obligation_cents: int
    provision_limit_cents: int
    refunded_excluded: list[str]
    bundles: list[BundlePlan]
    blocked: dict[str, list[str]]
    quote_revenue_bands: dict[str, tuple[int, int]]
    quote_trace_counts: dict[str, int]


def plan_round(store: RecordStore, as_of: int, market: str = "east") -> RoundPlan:
    evidence = build_evidence(store, as_of)
    groups = build_groups(evidence)
    ledger = PostingLedger(store)
    protection = ProtectionEngine(store, market=market)

    current_states = ledger.states(as_of)
    current_effective = [q for q, s in current_states.items() if s.is_effective]
    base_report = protection.evaluate(as_of)

    verdicts = {}
    blocked: dict[str, list[str]] = {}
    revenue_bands: dict[str, tuple[int, int]] = {}
    for nq in store.table("next_quotes"):
        v = evaluate_candidate(nq, groups, evidence, store, as_of)
        verdicts[nq["id"]] = v
        if v.admission in BLOCKED_ADMISSIONS:
            blocked[nq["id"]] = v.reasons
        if v.revenue_band_cents is not None:
            revenue_bands[nq["id"]] = v.revenue_band_cents

    # 候选池：准入通过、且当前尚未生效（已生效的是既成义务，不重复发布）
    publishable = [
        nq["id"]
        for nq in store.table("next_quotes")
        if nq["id"] not in blocked and nq["id"] not in current_effective
    ]

    # 枚举同时发布组合；财务约束按“同一次发布”整体考核
    bundles: list[BundlePlan] = []
    for size in range(0, len(publishable) + 1):
        for combo in combinations(publishable, size):
            report = protection.evaluate(
                as_of, extra_effective=set(combo)
            )
            incremental = report.union_total_cents - base_report.union_total_cents
            per_quote: dict[str, int] = {}
            for qid in combo:
                solo = protection.evaluate(
                    as_of, extra_effective=set(combo) - {qid}
                )
                per_quote[qid] = report.union_total_cents - solo.union_total_cents
            notes: list[str] = []
            for qid in combo:
                v = verdicts[qid]
                if v.correction_warning is not None:
                    cw = v.correction_warning
                    notes.append(
                        f"{qid} 依赖的同渠道历史售罄凭证已被更正（seen={cw['seen']}，"
                        f"visible={cw['visible']}）：该未执行挂牌不得再按原依据执行"
                    )
                else:
                    notes.append(
                        f"{qid} 不依赖被撤销的售罄凭证，但仍受准入状态与准备额约束"
                    )
            bundles.append(
                BundlePlan(
                    quote_ids=tuple(sorted(combo)),
                    incremental_cents=incremental,
                    total_union_cents=report.union_total_cents,
                    within_provision=(
                        report.headroom_cents is None
                        or report.headroom_cents >= 0
                    ),
                    headroom_after_cents=(
                        report.headroom_cents
                        if report.headroom_cents is not None else 0
                    ),
                    per_quote_increment=per_quote,
                    liabilities_added=len(report.liabilities)
                    - len(base_report.liabilities),
                    notes=notes,
                )
            )

    # 可发布组合优先：在准备额内、且增量最小的在前，便于运营直接挑选
    bundles.sort(
        key=lambda b: (
            not b.within_provision,
            b.incremental_cents,
            len(b.quote_ids),
            b.quote_ids,
        )
    )

    trace_counts = {}
    full_report = protection.evaluate(
        as_of, extra_effective=set(publishable)
    )
    for nq in store.table("next_quotes"):
        trace_counts[nq["id"]] = len(full_report.trace_for_quote(nq["id"]))

    return RoundPlan(
        as_of=as_of,
        candidate_verdicts=verdicts,
        current_effective=current_effective,
        current_obligation_cents=base_report.union_total_cents,
        provision_limit_cents=(
            base_report.headroom_limit_cents
            if base_report.headroom_limit_cents is not None else 0
        ),
        refunded_excluded=base_report.excluded_refunded,
        bundles=bundles,
        blocked=blocked,
        quote_revenue_bands=revenue_bands,
        quote_trace_counts=trace_counts,
    )
