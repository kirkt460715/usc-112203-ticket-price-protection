"""需求证据层：把库存轨迹与订单旅程还原成四类需求证据。

四类证据（对应业务上的不同含义，不能混为一谈）
================================================
``soldout``        可买席位耗尽（available=0），此时段的成交尝试是想买买不到的
                   隐性需求，单纯按卖出张数拟合会把这部分人忽略。
``held``           短暂锁位（available=0 且 held>0）：席位被订单暂时占住，
                   并非永久售罄，需与真售罄分开报告。
``not_released``   渠道在开售窗口内从未放出可买席位（始终 available=0），
                   没有任何需求观测，不能当作零需求。
``no_attempt``     席位可买但没有成交（abandoned/hold_timeout 之外无人下单），
                   代表弱需求证据。

见面会（activity == "meet"）属于外部活动，会把权益吸引力混入票价效应，
对应挂牌在敏感度分析中整体剔除，只保留在原始证据里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import InventoryPoint, RecordStore

# 订单结果语义
PAID = "paid"
SOLDOUT = "soldout"
ABANDONED = "abandoned"
HOLD_TIMEOUT = "hold_timeout"


@dataclass
class OfferEvidence:
    offer_id: str
    round: int
    area: str
    channel: str
    bundle: str
    inclusive_cents: int          # price + fee，单位分
    window: tuple[int, int]       # announced_minute, expires_minute
    meet_confounded: bool         # 窗口内存在见面会活动
    never_released: bool          # 渠道整窗未放票
    soldout_minutes: list[int]    # 可买席位为 0 的观测分钟（0 分钟放票前除外）
    transient_lock_minutes: list[int]  # available=0 且 held>0 的观测分钟
    outcomes: dict[str, int]
    unmet_attempts: int           # 真售罄时段内开始的 soldout 尝试数
    lost_holds: int               # 锁位/售罄时段内的 hold_timeout 数
    weak_attempts: int            # 有可买席位时的 abandoned 数（弱需求）
    trajectory: list[dict] = field(default_factory=list)
    correction: dict | None = None  # 事后更正凭证（seen 晚于窗口即证据撤销）

    @property
    def paid(self) -> int:
        return self.outcomes.get(PAID, 0)

    @property
    def evidence_class(self) -> str:
        """四类需求证据的主分类。"""
        if self.never_released:
            return "not_released"
        if self.soldout_minutes:
            return "soldout"
        if self.transient_lock_minutes:
            return "held"
        return "no_attempt"


def _point_at(points: list[InventoryPoint], minute: int) -> InventoryPoint | None:
    """取 minute 时刻（含）最近的库存点。"""
    candidate = [p for p in points if p.minute <= minute]
    return max(candidate, key=lambda p: p.minute) if candidate else None


def build_evidence(store: RecordStore, as_of: int) -> dict[str, OfferEvidence]:
    """重建 as_of 时点每个挂牌的需求证据。

    迟到的库存更正若 ``seen > as_of`` 不会进入视图——这正是“不能倒填”的
    实现：同一挂牌在不同 as_of 下可能得到不同结论。
    """
    points = store.visible_inventory(as_of)
    by_offer: dict[str, list[InventoryPoint]] = {}
    for p in points:
        by_offer.setdefault(p.offer, []).append(p)
    for plist in by_offer.values():
        plist.sort(key=lambda p: p.minute)

    journeys = store.table("journeys")
    outcomes_by_offer: dict[str, dict[str, int]] = {}
    for row in journeys:
        bucket = outcomes_by_offer.setdefault(
            row["offer"],
            {PAID: 0, SOLDOUT: 0, ABANDONED: 0, HOLD_TIMEOUT: 0},
        )
        bucket[row["outcome"]] += 1

    result: dict[str, OfferEvidence] = {}
    for quote in store.table("quotes"):
        oid = quote["id"]
        plist = by_offer.get(oid, [])
        post_release = [p for p in plist if p.minute > quote["announced_minute"]]
        ever_available = any(p.available > 0 for p in post_release)
        soldout_minutes = [p.minute for p in post_release if p.available == 0]
        lock_minutes = [
            p.minute for p in post_release if p.available == 0 and p.held > 0
        ]
        meet = any(p.activity == "meet" for p in plist)

        unmet = lost = weak = 0
        for row in journeys:
            if row["offer"] != oid:
                continue
            point = _point_at(plist, row["started"])
            if point is None:
                continue
            no_seat = point.available == 0
            if row["outcome"] == SOLDOUT and no_seat:
                unmet += 1
            elif row["outcome"] == HOLD_TIMEOUT and no_seat:
                lost += 1
            elif row["outcome"] == ABANDONED and not no_seat:
                weak += 1

        # 关联本挂牌的库存更正凭证（无论在 as_of 是否可见，都登记其状态）
        correction = None
        offer_original_ids = {p.source_id for p in plist}
        for raw in store.corrections():
            if raw["offer"] != oid:
                continue
            visible = raw["seen"] <= as_of
            if visible:
                # 更正已进入视图：原行已被取代，轨迹里只剩新版本
                orig_row = store.inventory_row(raw["supersedes"])
                correction = {
                    "correcting_id": raw["id"],
                    "original_id": raw["supersedes"],
                    "seen": raw["seen"],
                    "visible": True,
                    "original_available": orig_row["available"],
                    "corrected_available": raw["available"],
                }
            elif raw["supersedes"] in offer_original_ids:
                # 更正尚未可见：原始（错误）行仍在视图里
                correction = {
                    "correcting_id": raw["id"],
                    "original_id": raw["supersedes"],
                    "seen": raw["seen"],
                    "visible": False,
                    "original_available": next(
                        p.available for p in plist if p.source_id == raw["supersedes"]
                    ),
                    "corrected_available": raw["available"],
                }

        result[oid] = OfferEvidence(
            offer_id=oid,
            round=quote["round"],
            area=quote["area"],
            channel=quote["channel"],
            bundle=quote["bundle"],
            inclusive_cents=(quote["price"] + quote["fee"]) * 100,
            window=(quote["announced_minute"], quote["expires_minute"]),
            meet_confounded=meet,
            never_released=not ever_available,
            soldout_minutes=soldout_minutes,
            transient_lock_minutes=lock_minutes,
            outcomes=dict(outcomes_by_offer.get(
                oid, {PAID: 0, SOLDOUT: 0, ABANDONED: 0, HOLD_TIMEOUT: 0}
            )),
            unmet_attempts=unmet,
            lost_holds=lost,
            weak_attempts=weak,
            trajectory=[p.to_dict() for p in plist],
            correction=correction,
        )
    return result
