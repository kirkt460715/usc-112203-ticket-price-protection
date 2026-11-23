"""票价能力的业务规则测试。

运行：python3 -m unittest discover -s tests -v
"""

import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pricing import (
    PostingLedger,
    ProtectionEngine,
    RecordStore,
    build_evidence,
    build_groups,
    evaluate_candidate,
    plan_round,
)
from pricing.sensitivity import (
    ADMIT_NO_EQUIVALENCE,
    ADMIT_SUPPORTED,
    ADMIT_UNOBSERVED,
)

RECORDS = ROOT / "records"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore(RECORDS)

    def test_catalog_integrity_enforced(self):
        # 构造即完成哈希/行数/唯一 id 校验，能初始化说明资料完整
        self.assertEqual(len(self.store.table("quotes")), 48)

    def test_late_correction_not_backfilled(self):
        # 更正凭证 seen=140：as_of=130 的视图里原行仍是 available=0
        early = {p.source_id: p for p in self.store.visible_inventory(130)}
        self.assertEqual(early["C8-north-web-25"].available, 0)
        late = {p.source_id: p for p in self.store.visible_inventory(160)}
        # 140 之后原行被新版本取代，原始记录本身未被修改
        self.assertNotIn("C8-north-web-25", late)
        self.assertEqual(late["C8-north-web-25-R"].available, 27)
        self.assertEqual(
            self.store.inventory_row("C8-north-web-25")["available"], 0
        )

    def test_late_refunds_not_backfilled(self):
        self.assertEqual(self.store.visible_refunds(130), [])
        self.assertEqual(len(self.store.visible_refunds(160)), 3)

    def test_pending_equivalence_not_effective(self):
        # E2 在 as_of=160 仍为 pending，任何时点都不产生等效效力
        ids = {n["rule"] for n in self.store.visible_equivalence_notices(160)}
        self.assertEqual(ids, {"E1"})


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore(RECORDS)

    def test_soldout_vs_transient_lock_distinguished(self):
        ev = build_evidence(self.store, 160)
        # north 区在 100 分钟售罄：售罄证据
        self.assertIn(100, ev["O8-north-web"].soldout_minutes)
        # 更正后 25 分钟不再是售罄点
        self.assertNotIn(25, ev["O8-north-web"].soldout_minutes)
        # south web：75 分钟短暂锁位后 100 分钟仍为 0（窗口结束前未补放），
        # 75 分钟同时登记为锁位（held>0），与“可买但没人买”的弱需求区分开
        south = ev["O8-south-web"]
        self.assertIn(75, south.transient_lock_minutes)
        self.assertEqual(south.evidence_class, "soldout")
        # 对照组：O2-south-web 在 25 分钟锁位后 75 分钟补放恢复可买，
        # 100 分钟有可买席位，不属于终局售罄
        recovered = ev["O2-south-web"]
        self.assertIn(25, recovered.transient_lock_minutes)
        self.assertNotIn(100, recovered.soldout_minutes)

    def test_correction_flagged_and_view_dependent(self):
        early = build_evidence(self.store, 130)["O8-north-web"]
        late = build_evidence(self.store, 160)["O8-north-web"]
        self.assertFalse(early.correction["visible"])
        self.assertTrue(late.correction["visible"])
        # 结论反转：130 时点 25 分钟被当作售罄，140 更正后只剩 100 分钟
        self.assertIn(25, early.soldout_minutes)
        self.assertNotIn(25, late.soldout_minutes)

    def test_meet_activity_confounds_offer(self):
        ev = build_evidence(self.store, 160)
        self.assertTrue(ev["O8-deck-web"].meet_confounded)
        self.assertFalse(ev["O8-north-web"].meet_confounded)

    def test_unmet_demand_counted_separately_from_paid(self):
        ev = build_evidence(self.store, 160)["O8-north-web"]
        self.assertEqual(ev.paid, 16)
        self.assertGreater(ev.unmet_attempts, 0)

    def test_no_never_released_offer_in_dataset(self):
        ev = build_evidence(self.store, 160)
        self.assertFalse(any(e.never_released for e in ev.values()))

    def test_evidence_classification_boundaries(self):
        from pricing.evidence import OfferEvidence

        base = dict(
            round=1, area="north", channel="web", bundle="seat",
            inclusive_cents=10000, window=(0, 120), meet_confounded=False,
            outcomes={}, unmet_attempts=0, lost_holds=0, weak_attempts=0,
            trajectory=[], correction=None,
        )
        never = OfferEvidence(
            offer_id="X1", never_released=True, soldout_minutes=[],
            transient_lock_minutes=[], **base,
        )
        soldout = OfferEvidence(
            offer_id="X2", never_released=False, soldout_minutes=[100],
            transient_lock_minutes=[100], **base,
        )
        held = OfferEvidence(
            offer_id="X3", never_released=False, soldout_minutes=[],
            transient_lock_minutes=[25], **base,
        )
        quiet = OfferEvidence(
            offer_id="X4", never_released=False, soldout_minutes=[],
            transient_lock_minutes=[], **base,
        )
        self.assertEqual(never.evidence_class, "not_released")
        self.assertEqual(soldout.evidence_class, "soldout")
        self.assertEqual(held.evidence_class, "held")
        self.assertEqual(quiet.evidence_class, "no_attempt")
        self.assertEqual(quiet.paid, 0)


class SensitivityTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore(RECORDS)
        self.ev = build_evidence(self.store, 160)
        self.groups = build_groups(self.ev)

    def test_meet_offers_excluded_from_comparison(self):
        g = self.groups[("north", "web", "seat-and-meet")]
        self.assertEqual(g.observations, [])
        self.assertIn("O5-north-web", g.excluded_meet)

    def test_observed_band_uses_clean_observations(self):
        g = self.groups[("north", "web", "seat")]
        self.assertEqual((g.observed_min_cents, g.observed_max_cents),
                         (9300, 13800))

    def test_candidate_below_band_goes_to_verification_zone(self):
        n1 = next(q for q in self.store.table("next_quotes") if q["id"] == "N1")
        v = evaluate_candidate(n1, self.groups, self.ev, self.store, 160)
        self.assertEqual(v.admission, ADMIT_UNOBSERVED)
        self.assertEqual(v.observed_band_cents, (9300, 18000))

    def test_candidate_within_band_supported(self):
        n2 = next(q for q in self.store.table("next_quotes") if q["id"] == "N2")
        v = evaluate_candidate(n2, self.groups, self.ev, self.store, 160)
        self.assertEqual(v.admission, ADMIT_SUPPORTED)

    def test_unknown_bundle_without_published_equivalence_blocked(self):
        n4 = next(q for q in self.store.table("next_quotes") if q["id"] == "N4")
        v = evaluate_candidate(n4, self.groups, self.ev, self.store, 160)
        self.assertEqual(v.admission, ADMIT_NO_EQUIVALENCE)
        self.assertEqual(v.equivalent_groups, [])

    def test_meet_only_history_is_confounded_not_zero_demand(self):
        # 同口径历史挂牌全部在见面会窗口：独立的污染准入，而非“无需求”
        synth = {
            "id": "SYN-1", "round": 9, "area": "deck", "channel": "web",
            "bundle": "seat-and-meet", "price": 160, "fee": 3,
            "state": "approved", "effective_minute": 160, "as_of": 130,
        }
        v = evaluate_candidate(synth, self.groups, self.ev, self.store, 160)
        self.assertEqual(v.admission, "confounded")
        self.assertTrue(any("见面会" in r for r in v.reasons))

    def test_equivalence_invisible_before_notice(self):
        # as_of=120 之前 E1 公告（announced=120）尚不可见
        ev0 = build_evidence(self.store, 119)
        g0 = build_groups(ev0)
        n2 = next(q for q in self.store.table("next_quotes") if q["id"] == "N2")
        v = evaluate_candidate(n2, g0, ev0, self.store, 119)
        self.assertEqual(v.equivalent_groups, [])


class PostingTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore(RECORDS)
        self.ledger = PostingLedger(self.store)

    def test_sent_and_effective_are_separate_events(self):
        s = self.ledger.states(160)
        self.assertTrue(s["N2"].is_effective)
        self.assertTrue(s["N1"].is_sent_only and not s["N1"].is_effective)
        self.assertTrue(s["N4"].never_sent)

    def test_effective_timing(self):
        self.assertEqual(self.ledger.effective_quotes(127), [])
        self.assertEqual(self.ledger.effective_quotes(128), ["N2"])

    def test_unexecuted_quotes_identified(self):
        self.assertEqual(
            sorted(self.ledger.unexecuted_quotes(160)), ["N1", "N3", "N4"]
        )


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore(RECORDS)
        self.engine = ProtectionEngine(self.store)

    def test_obligation_starts_only_when_listing_effective(self):
        self.assertEqual(self.engine.evaluate(127).union_total_cents, 0)
        self.assertEqual(self.engine.evaluate(128).union_total_cents, 394700)

    def test_full_refunds_excluded_after_seen(self):
        before = self.engine.evaluate(138)
        after = self.engine.evaluate(139)
        self.assertEqual(before.excluded_refunded, [])
        self.assertEqual(
            sorted(after.excluded_refunded),
            ["J8-north-web-2", "J8-north-web-4", "J8-north-web-5"],
        )
        # 三张 9300 分全退订单在 N2(10300) 下本无补差、本不在义务清单中；
        # 退款回执到达后必须仍被显式登记为剔除对象，未来更低挂牌生效时
        # 不会被重新纳入补差人群
        self.assertEqual(before.union_total_cents, after.union_total_cents)
        all_paid = {
            x["id"]
            for x in self.store.table("journeys")
            if x["outcome"] == "paid"
        }
        refunded = set(after.excluded_refunded)
        after_ids = {l.ticket_id for l in after.liabilities}
        self.assertTrue(refunded <= all_paid)
        self.assertTrue(after_ids.isdisjoint(refunded))

    def test_same_difference_paid_once_across_rules(self):
        r = self.engine.evaluate(160, extra_effective={"N1", "N3"})
        # P1 与 P2 对 r8 web 订单同时命中，但并表额取最大值而非相加
        self.assertLess(r.union_total_cents, sum(r.rule_totals_cents.values()))
        for liab in r.liabilities:
            self.assertEqual(
                liab.union_cents, max(liab.rule_amounts.values())
            )

    def test_original_channel_only_rule(self):
        # P2 只覆盖 web：追加 partner 挂牌 N3 不改变 P2 总额
        only_n2 = self.engine.evaluate(160)
        with_n3 = self.engine.evaluate(160, extra_effective={"N3"})
        self.assertEqual(only_n2.rule_totals_cents["P2"], 0)
        self.assertEqual(with_n3.rule_totals_cents["P2"], 0)
        with_n1 = self.engine.evaluate(160, extra_effective={"N1"})
        self.assertEqual(with_n1.rule_totals_cents["P2"], 74500)

    def test_provision_headroom_and_incremental_scenarios(self):
        base = self.engine.evaluate(160)
        self.assertEqual(base.headroom_limit_cents, 598000)
        n1 = self.engine.evaluate(160, extra_effective={"N1"})
        n3 = self.engine.evaluate(160, extra_effective={"N3"})
        both = self.engine.evaluate(160, extra_effective={"N1", "N3"})
        # N3 的 8700 是 E1 全域最低，N1 叠加不再增加 P1 义务
        self.assertEqual(both.union_total_cents, n3.union_total_cents)
        # 所有场景均在准备额内但余量极薄
        self.assertGreaterEqual(n3.headroom_cents, 0)
        self.assertEqual(n1.union_total_cents - base.union_total_cents,
                         561100 - 394700)

    def test_trace_quote_to_population(self):
        r = self.engine.evaluate(160, extra_effective={"N1", "N3"})
        traced = {l.ticket_id for l in r.trace_for_quote("N3")}
        # N3 经 E1 影响所有 r7/r8 north/south 的未退款订单
        self.assertIn("J7-north-web-0", traced)
        self.assertNotIn("J8-north-web-2", traced)  # 已全退
        # deck 订单不在 E1 等效范围内
        self.assertFalse(any(t.startswith("J8-deck") for t in traced))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore(RECORDS)

    def test_plan_blocks_unsupported_and_keeps_n2(self):
        plan = plan_round(self.store, 160)
        self.assertEqual(set(plan.blocked), {"N1", "N3", "N4"})
        self.assertEqual(plan.current_effective, ["N2"])

    def test_plan_bundles_respect_provision_and_dedup(self):
        plan = plan_round(self.store, 160)
        # 没有可新增发布的价档时，唯一组合是空组，且在准备额内
        self.assertEqual([b.quote_ids for b in plan.bundles], [()])
        self.assertTrue(plan.bundles[0].within_provision)

    def test_hypothetical_bundles_when_admission_ignored(self):
        # 直接用保护引擎验证：即便强行同时发布 N1+N3，同一次发布的合并
        # 义务也不超过财务上限（598000），且差额只兑现一次
        eng = ProtectionEngine(self.store)
        r = eng.evaluate(160, extra_effective={"N1", "N3"})
        self.assertLessEqual(r.union_total_cents, 598000)
        self.assertEqual(r.union_total_cents, 573500)

    def test_reversal_scenario_before_correction(self):
        # 130 时点：更正不可见，N1/N3 的“售罄依据”看似成立但价位仍越界
        plan = plan_round(self.store, 130)
        self.assertIn("N1", plan.blocked)
        verdicts = plan.candidate_verdicts
        self.assertFalse(
            verdicts["N1"].correction_warning["visible"]
        )


class ReportTests(unittest.TestCase):
    def test_markdown_report_renders_both_views(self):
        from pricing.report import render_markdown

        store = RecordStore(RECORDS)
        late = render_markdown(store, 160)
        early = render_markdown(store, 130)
        self.assertIn("原售罄依据失效", late)
        self.assertIn("结论可能反转", early)
        # 保价去重、准备额、求证区间均出现在报告中
        self.assertIn("5980.00 元", late)
        self.assertIn("保留在求证区间", late)
        self.assertIn("394700", late)

    def test_json_export_roundtrip(self):
        import json
        import tempfile
        from pathlib import Path
        from pricing.report import main

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            rc = main([
                "--records", str(RECORDS), "--as-of", "160",
                "--json", str(out),
            ])
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["current_effective"], ["N2"])
            self.assertEqual(set(data["blocked"]), {"N1", "N3", "N4"})


if __name__ == "__main__":
    unittest.main()
