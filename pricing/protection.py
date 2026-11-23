"""已售票价格保护的逐单核算。

规则要点（全部来自业务记录，不做表外假设）
==========================================
* 保价规则只对 ``purchased_rounds`` 内、规则覆盖渠道上的旧订单生效，且
  当前轮次不晚于 ``effective_through``。
* ``include_fees=True`` 时比较的是含费实付（price+fee），实收以分计。
* ``original_channel_only=True`` 时只看原渠道挂牌；否则在已公布等效规则
  覆盖的所有渠道、座区、套餐版本内取最低含费实付价。
* 受理中（pending）的等效变更不产生效力，不能作为释放保价占用的理由。
* 已全额退款的订单不能再重复补差；同一订单被多个承诺指向时，同一差额
  只兑现一次（取各承诺应补额的最大值）。
* 挂牌只有“已生效”（postings 有 public_at 且候选挂牌 effective_minute
  已到）才触发保价；仅发令未生效的不产生不可撤销义务。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import RecordStore


@dataclass
class OrderLiability:
    ticket_id: str
    offer_id: str
    round: int
    area: str
    channel: str
    bundle: str
    paid_cents: int
    rule_amounts: dict[str, int] = field(default_factory=dict)
    matched_quotes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def union_cents(self) -> int:
        return max(self.rule_amounts.values(), default=0)


@dataclass
class ProtectionReport:
    liabilities: list[OrderLiability]
    rule_totals_cents: dict[str, int]
    union_total_cents: int
    excluded_refunded: list[str]
    headroom_cents: int | None
    headroom_limit_cents: int | None

    def trace_for_quote(self, quote_id: str) -> list[OrderLiability]:
        """某个挂牌价直接参与补差的人群（可从价档追到的订单）。"""
        return [
            liab
            for liab in self.liabilities
            if quote_id in {q for qs in liab.matched_quotes.values() for q in qs}
        ]


class ProtectionEngine:
    def __init__(self, store: RecordStore, market: str = "east"):
        self.store = store
        self.market = market

    # ---- 公告与挂牌生效判定 ---------------------------------------------
    def _public_equivalence(self, as_of: int) -> dict[str, dict]:
        notices = self.store.visible_equivalence_notices(as_of)
        rules = {r["id"]: r for r in self.store.table("equivalence")}
        return {
            n["rule"]: rules[n["rule"]]
            for n in notices
            if n["market"] == self.market and n["effective"] <= as_of
        }

    def effective_next_quotes(self, as_of: int) -> dict[str, dict]:
        """截至 as_of 已对外生效的预备挂牌。"""
        postings = {
            p["quote"]: p
            for p in self.store.visible_postings(as_of)
            if p["public_at"] is not None and p["public_at"] <= as_of
        }
        result = {}
        for nq in self.store.table("next_quotes"):
            p = postings.get(nq["id"])
            if p is None:
                continue
            if nq["effective_minute"] <= as_of and nq["state"] == "posted":
                result[nq["id"]] = nq
        return result

    # ---- 等效匹配 -------------------------------------------------------
    def _equivalent_quotes(
        self,
        order_quote: dict,
        effective: dict[str, dict],
        public_rules: dict[str, dict],
        cross_channel: bool,
    ) -> list[dict]:
        """找出与旧订单同等权益、可触发本保价规则的已生效挂牌。"""
        matches = []
        for nq in effective.values():
            if nq["bundle"] != order_quote["bundle"]:
                # 套餐版本不同：只有已公布等效规则声明的范围内才算同等
                if not any(
                    order_quote["area"] in r["areas"]
                    and nq["area"] in r["areas"]
                    and order_quote["bundle"] in r["bundles"]
                    and nq["bundle"] in r["bundles"]
                    and nq["round"] in r["valid_rounds"]
                    for r in public_rules.values()
                ):
                    continue
            if not cross_channel and nq["channel"] != order_quote["channel"]:
                continue
            if cross_channel:
                # 跨渠道/跨座区必须落在某条已公布等效规则内
                covered = any(
                    order_quote["area"] in r["areas"]
                    and nq["area"] in r["areas"]
                    and order_quote["channel"] in r["channels"]
                    and nq["channel"] in r["channels"]
                    and nq["bundle"] in r["bundles"]
                    and nq["round"] in r["valid_rounds"]
                    for r in public_rules.values()
                )
                if not covered:
                    continue
            matches.append(nq)
        return matches

    # ---- 逐单核算 -------------------------------------------------------
    def evaluate(
        self,
        as_of: int,
        extra_effective: set[str] | None = None,
        round_no: int = 9,
    ) -> ProtectionReport:
        """核算 as_of 时点的保价义务。

        extra_effective: 假设追加生效的预备挂牌 id（用于发布前压力测试），
        不修改原始记录。
        """
        effective = dict(self.effective_next_quotes(as_of))
        for qid in extra_effective or set():
            effective[qid] = self.store.next_quote(qid)
        public_rules = self._public_equivalence(as_of)

        full_refunds = {
            r["ticket"]
            for r in self.store.visible_refunds(as_of)
            if r["mode"] == "full"
        }
        quote_by_id = {q["id"]: q for q in self.store.table("quotes")}
        paid_orders = [
            row for row in self.store.table("journeys") if row["outcome"] == "paid"
        ]

        liabilities: list[OrderLiability] = []
        excluded = sorted(t for t in full_refunds)
        rule_totals: dict[str, int] = {
            r["id"]: 0 for r in self.store.table("protection")
        }

        for order in paid_orders:
            if order["id"] in full_refunds:
                continue  # 已退全款，不能重复补
            oq = quote_by_id[order["offer"]]
            liab = OrderLiability(
                ticket_id=order["id"],
                offer_id=order["offer"],
                round=oq["round"],
                area=oq["area"],
                channel=oq["channel"],
                bundle=oq["bundle"],
                paid_cents=order["paid_cents"],
            )
            for rule in self.store.table("protection"):
                if oq["round"] not in rule["purchased_rounds"]:
                    continue
                if round_no > rule["effective_through"]:
                    continue
                if oq["channel"] not in rule["channels"]:
                    continue
                cross = not rule["original_channel_only"]
                if not cross:
                    # 原渠道保价：订单渠道必须被规则覆盖，且只与同渠道挂牌比较
                    if oq["channel"] not in rule["channels"]:
                        continue
                matches = self._equivalent_quotes(oq, effective, public_rules, cross)
                if not matches:
                    continue
                min_paid = min((n["price"] + n["fee"]) * 100 for n in matches)
                amount = max(0, order["paid_cents"] - min_paid)
                if amount > 0:
                    liab.rule_amounts[rule["id"]] = amount
                    liab.matched_quotes[rule["id"]] = sorted(n["id"] for n in matches)
                    rule_totals[rule["id"]] += amount
            if liab.rule_amounts:
                liabilities.append(liab)

        union_total = sum(l.union_cents for l in liabilities)

        headroom = limit = None
        limits = [
            p
            for p in self.store.table("provision_limits")
            if p["round"] == round_no and p["market"] == self.market
            and p["issued_at"] <= as_of
        ]
        if limits:
            p = max(limits, key=lambda x: x["issued_at"])
            limit = p["max_cents"] - p["unrelated_committed_cents"]
            headroom = limit - union_total
        return ProtectionReport(
            liabilities=liabilities,
            rule_totals_cents=rule_totals,
            union_total_cents=union_total,
            excluded_refunded=excluded,
            headroom_cents=headroom,
            headroom_limit_cents=limit,
        )
