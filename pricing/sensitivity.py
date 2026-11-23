"""价格敏感度与可辨识区间。

方法
====
1. 先按相同开售阶段（所有挂牌均为轮次内 0–120 分钟同窗口）与相同权益口径
   （area + channel + bundle 完全一致）建立可比组；窗口内出现见面会活动的
   挂牌作为权益版本变化整体剔除，不进入价格-需求比较。
2. 跨渠道/跨座区只有在等效规则对该地区、套餐版本、轮次均生效，且公告已在
   as_of 前公布时，才允许并入比较。
3. 每个可比组给出已观测含费价区间；落在区间外的候选价没有足够支撑，
   一律进入求证区间，只给区间、不给点估计。
4. 需求同时报告成交量与“想买买不到”的未满足尝试，避免直接对挂牌价拟合
   卖出张数而忽略配给效应。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import OfferEvidence

ADMIT_SUPPORTED = "supported"          # 落在已辨识区间，可进入发布评估
ADMIT_UNOBSERVED = "unobserved_price"  # 超出历史观测价域，保留在求证区间
ADMIT_NO_EQUIVALENCE = "no_equivalence"  # 等效未公布/版本不符，无法比较
ADMIT_CONFOUNDED = "confounded"        # 唯一可比证据被见面会等外部活动污染


@dataclass
class Observation:
    round: int
    inclusive_cents: int
    paid: int
    unmet_attempts: int
    weak_attempts: int
    evidence_class: str
    offer_id: str


@dataclass
class ComparableGroup:
    area: str
    channel: str
    bundle: str
    observations: list[Observation] = field(default_factory=list)
    excluded_meet: list[str] = field(default_factory=list)

    @property
    def observed_min_cents(self) -> int:
        return min(o.inclusive_cents for o in self.observations)

    @property
    def observed_max_cents(self) -> int:
        return max(o.inclusive_cents for o in self.observations)

    def demand_at(self, inclusive_cents: int) -> list[Observation]:
        return [o for o in self.observations if o.inclusive_cents == inclusive_cents]

    def paid_band(self) -> tuple[int, int]:
        return (
            min(o.paid for o in self.observations),
            max(o.paid for o in self.observations),
        )

    def unmet_band(self) -> tuple[int, int]:
        return (
            min(o.unmet_attempts for o in self.observations),
            max(o.unmet_attempts for o in self.observations),
        )


def build_groups(
    evidence: dict[str, OfferEvidence],
) -> dict[tuple[str, str, str], ComparableGroup]:
    """建立严格同口径可比组；见面会污染的挂牌单列剔除。"""
    groups: dict[tuple[str, str, str], ComparableGroup] = {}
    for ev in evidence.values():
        key = (ev.area, ev.channel, ev.bundle)
        group = groups.setdefault(key, ComparableGroup(*key))
        if ev.meet_confounded:
            group.excluded_meet.append(ev.offer_id)
            continue
        group.observations.append(
            Observation(
                round=ev.round,
                inclusive_cents=ev.inclusive_cents,
                paid=ev.paid,
                unmet_attempts=ev.unmet_attempts,
                weak_attempts=ev.weak_attempts,
                evidence_class=ev.evidence_class,
                offer_id=ev.offer_id,
            )
        )
    for group in groups.values():
        group.observations.sort(key=lambda o: (o.inclusive_cents, o.round))
    return groups


@dataclass
class CandidateVerdict:
    quote_id: str
    inclusive_cents: int
    group_key: tuple[str, str, str]
    admission: str
    reasons: list[str] = field(default_factory=list)
    observed_band_cents: tuple[int, int] | None = None
    equivalent_groups: list[tuple[str, str, str]] = field(default_factory=list)
    revenue_band_cents: tuple[int, int] | None = None
    correction_warning: dict | None = None


def _equivalence_open(
    store, area: str, bundle: str, round_no: int, as_of: int
) -> tuple[bool, list[str]]:
    """等效规则是否已公布并覆盖该座区/套餐/轮次；返回 (可用, 命中规则id)。

    渠道覆盖在具体比较时按规则的 channels 与保价承诺的渠道口径判断。
    """
    hits: list[str] = []
    notice_rules = {n["rule"] for n in store.visible_equivalence_notices(as_of)}
    for rule in store.table("equivalence"):
        if rule["id"] not in notice_rules:
            continue  # 受理中的变更不能抢先产生等效效力
        if (
            area in rule["areas"]
            and bundle in rule["bundles"]
            and round_no in rule["valid_rounds"]
        ):
            hits.append(rule["id"])
    return (bool(hits), hits)


def _channel_covered(store, rule_id: str, channel: str) -> bool:
    for rule in store.table("equivalence"):
        if rule["id"] == rule_id:
            return channel in rule["channels"]
    return False


def evaluate_candidate(
    quote: dict,
    groups: dict[tuple[str, str, str], ComparableGroup],
    evidence: dict[str, OfferEvidence],
    store,
    as_of: int,
) -> CandidateVerdict:
    """评估一个预备挂牌的需求支撑与可辨识范围。"""
    inclusive = (quote["price"] + quote["fee"]) * 100
    key = (quote["area"], quote["channel"], quote["bundle"])
    verdict = CandidateVerdict(
        quote_id=quote["id"],
        inclusive_cents=inclusive,
        group_key=key,
        admission=ADMIT_SUPPORTED,
    )

    group = groups.get(key)
    direct_obs = group.observations if group else []
    if not direct_obs:
        if group is not None and group.excluded_meet:
            verdict.admission = ADMIT_CONFOUNDED
            verdict.reasons.append(
                f"同口径 {key} 的历史挂牌 {group.excluded_meet} 全部处于见面会窗口，"
                "权益吸引力与票价效应无法分离，不能作为调价依据"
            )
        else:
            verdict.admission = ADMIT_NO_EQUIVALENCE
            verdict.reasons.append(
                f"同口径 {key} 没有无污染的历史挂牌（套餐版本 "
                f"{quote['bundle']} 从未在该座区/渠道挂牌）"
            )

    # 等效规则可以把其它座区/渠道的同权益观测并入比较
    ok, rules = _equivalence_open(
        store, quote["area"], quote["bundle"], quote["round"], as_of
    )
    equivalent: list[tuple[str, str, str]] = []
    if ok:
        for gkey, g in groups.items():
            if gkey == key or gkey[2] != quote["bundle"]:
                continue
            covered, hit_rules = _equivalence_open(
                store, gkey[0], quote["bundle"], quote["round"], as_of
            )
            if not covered or not g.observations:
                continue
            # 候选渠道与观测渠道都必须被同一条已公布规则覆盖
            if not any(
                _channel_covered(store, rid, quote["channel"])
                and _channel_covered(store, rid, gkey[1])
                for rid in hit_rules
            ):
                continue
            equivalent.append(gkey)
        verdict.equivalent_groups = equivalent
        verdict.reasons.append(f"等效规则 {rules} 已公布，可并入 {equivalent} 同权益观测")
    else:
        verdict.reasons.append(
            "无已公布且覆盖本候选的等效规则，跨座区/跨渠道价档不可比较"
        )

    all_obs = list(direct_obs)
    for gkey in equivalent:
        all_obs.extend(groups[gkey].observations)
    if all_obs:
        band = (min(o.inclusive_cents for o in all_obs),
                max(o.inclusive_cents for o in all_obs))
        verdict.observed_band_cents = band
        if inclusive < band[0] or inclusive > band[1]:
            verdict.admission = ADMIT_UNOBSERVED
            verdict.reasons.append(
                f"候选含费价 {inclusive} 分超出已辨识区间 {band[0]}–{band[1]} 分，"
                "该价位没有足够支撑，保留在求证区间"
            )
    # 净收入区间只用同口径历史成交量带（跨渠道/座区的未满足尝试不能当作
    # 本渠道的可兑现销量）；未满足需求作为独立证据单列，不并入收入上沿
    if direct_obs:
        low_q = min(o.paid for o in direct_obs)
        high_q = max(o.paid for o in direct_obs)
        verdict.revenue_band_cents = (low_q * inclusive, high_q * inclusive)
        verdict.reasons.append(
            f"同口径历史成交量带 {low_q}–{high_q} 张；"
            f"另有未满足尝试 {min(o.unmet_attempts for o in direct_obs)}–"
            f"{max(o.unmet_attempts for o in direct_obs)} 次，"
            "属配给证据而非可兑现销量，不计入收入区间"
        )
    elif verdict.admission == ADMIT_SUPPORTED:
        verdict.admission = ADMIT_NO_EQUIVALENCE

    # 证据撤销提示：候选依据的历史挂牌若存在迟到更正，标记结论可能反转
    for ev in evidence.values():
        if (
            ev.correction is not None
            and ev.area == quote["area"]
            and ev.channel == quote["channel"]
        ):
            verdict.correction_warning = ev.correction
            verdict.reasons.append(
                f"历史挂牌 {ev.offer_id} 的售罄凭证在第 {ev.correction['seen']} 分钟"
                f"被更正（available {ev.correction['original_available']} -> "
                f"{ev.correction['corrected_available']}）；"
                + ("更正已可见，原依据失效" if ev.correction["visible"]
                   else "更正尚未可见，as_of 结论可能反转")
            )
    return verdict
