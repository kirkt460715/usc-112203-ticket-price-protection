"""命令行入口：输出一轮挂牌决策的完整复核报告。

用法
====
    python3 -m pricing.report --records records --as-of 160
    python3 -m pricing.report --records records --as-of 130 --json out.json

报告包含：as_of 证据视图、四类需求证据、可比组与可辨识区间、候选准入、
挂牌台账、逐单保价与去重、准备额约束、可同时发布组合、净收入/应补款区间。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .engine import plan_round
from .evidence import build_evidence
from .posting import PostingLedger
from .protection import ProtectionEngine
from .sensitivity import build_groups
from .store import RecordStore

ADMIT_TEXT = {
    "supported": "可进入发布评估（落在已辨识区间）",
    "unobserved_price": "保留在求证区间（超出历史观测价域）",
    "no_equivalence": "保留在求证区间（无已公布等效、无法比较）",
    "confounded": "保留在求证区间（历史证据被见面会等外部活动污染）",
}


def _yuan(cents: int) -> str:
    return f"{cents / 100:.2f}"


def render_markdown(store: RecordStore, as_of: int, market: str = "east") -> str:
    evidence = build_evidence(store, as_of)
    groups = build_groups(evidence)
    ledger = PostingLedger(store)
    engine = ProtectionEngine(store, market)
    plan = plan_round(store, as_of, market)
    states = ledger.states(as_of)

    lines: list[str] = []
    add = lines.append
    add(f"# 第 9 轮挂牌复核报告（as_of={as_of} 分钟，市场={market}）\n")

    # 1. 证据视图
    add("## 1. as_of 证据视图与迟到凭证\n")
    for ev in evidence.values():
        if ev.correction is not None:
            c = ev.correction
            add(
                f"- {ev.offer_id}：库存更正 `{c['correcting_id']}` 在第 "
                f"{c['seen']} 分钟到达，{('已可见，原售罄依据失效' if c['visible'] else '尚未可见，当前结论可能反转')}；"
                f"available {c['original_available']} → {c['corrected_available']}。"
            )
    refunds = store.visible_refunds(as_of)
    add(
        f"- 已收到退款回执 {len(refunds)} 笔："
        + (", ".join(f"{r['ticket']}（{r['mode']}）" for r in refunds) or "无")
    )
    notices = store.visible_equivalence_notices(as_of)
    add("- 已公布等效公告：" + ", ".join(n["rule"] for n in notices) +
        "；受理中（pending）的变更不产生效力。\n")

    # 2. 需求证据分类
    add("## 2. 四类需求证据（按挂牌）\n")
    add("| 挂牌 | 含费(分) | 主分类 | 售罄分钟 | 锁位分钟 | 成交 | 未满足尝试 | 弱需求 | 见面会污染 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for ev in sorted(evidence.values(), key=lambda e: e.offer_id):
        add(
            f"| {ev.offer_id} | {ev.inclusive_cents} | {ev.evidence_class} | "
            f"{ev.soldout_minutes or '-'} | {ev.transient_lock_minutes or '-'} | "
            f"{ev.paid} | {ev.unmet_attempts} | {ev.weak_attempts} | "
            f"{'是' if ev.meet_confounded else '否'} |"
        )
    add("")

    # 3. 可比组与可辨识区间
    add("## 3. 可比组与价格可辨识区间\n")
    add("仅同座区/同渠道/同套餐版本、且无见面会污染的观测进入比较；"
        "等效并入以已公布规则为限。\n")
    for key, g in sorted(groups.items()):
        if not g.observations:
            add(f"- `{key}`：无干净观测，剔除挂牌 {g.excluded_meet}")
            continue
        prices = sorted({o.inclusive_cents for o in g.observations})
        add(
            f"- `{key}`：可辨识含费价域 {_yuan(g.observed_min_cents)}–"
            f"{_yuan(g.observed_max_cents)} 元，观测价 {[ _yuan(p) for p in prices ]}，"
            f"成交量带 {g.paid_band()}，未满足尝试带 {g.unmet_band()}"
            + (f"；见面会剔除 {g.excluded_meet}" if g.excluded_meet else "")
        )
    add("")

    # 4. 候选价档准入
    add("## 4. 候选价档准入与净收入区间\n")
    add("| 候选 | 含费(分) | 准入 | 可辨识区间(分) | 净收入区间(分) | 追溯人群 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for qid, v in plan.candidate_verdicts.items():
        add(
            f"| {qid} | {v.inclusive_cents} | {ADMIT_TEXT.get(v.admission, v.admission)} | "
            f"{v.observed_band_cents or '-'} | {v.revenue_band_cents or '-'} | "
            f"{plan.quote_trace_counts[qid]} 单 |"
        )
        for reason in v.reasons:
            add(f"  - {reason}")
    add("")
    add("> 净收入区间 = 同口径历史成交量带 × 候选含费价；未满足尝试属配给证据，"
        "不计入可兑现销量。超出可辨识价域的候选不做点估计。\n")

    # 5. 挂牌台账
    add("## 5. 挂牌台账：发令与生效分离\n")
    add("| 候选 | 签准状态 | 发令 | 回执 | 对外生效 | 结论 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for qid, s in sorted(states.items()):
        cw = plan.candidate_verdicts[qid].correction_warning
        if s.is_effective:
            conclusion = "已生效，保价义务已产生（按原合同兑现并留痕）"
        elif cw is not None and cw["visible"]:
            conclusion = "未执行挂牌：其同渠道售罄依据已被更正，不得再按原依据执行"
        elif cw is not None:
            conclusion = "未执行挂牌：售罄依据存在未到达的更正，结论可能反转"
        elif qid in plan.blocked:
            conclusion = "未执行挂牌：无对外义务，须凭新证据重新评估"
        else:
            conclusion = "仅发令未生效，尚无对外义务"
        add(
            f"| {qid} | {s.state} | {s.sent} | {s.seen} | {s.public_at} | {conclusion} |"
        )
    add("")

    # 6. 保价核算
    add("## 6. 已售票保价核算（逐单去重后）\n")
    base = engine.evaluate(as_of)
    add(f"- 当前已生效挂牌：{plan.current_effective}；"
        f"现行保价义务合计 **{_yuan(base.union_total_cents)} 元**"
        f"（P1={_yuan(base.rule_totals_cents.get('P1', 0))}，"
        f"P2={_yuan(base.rule_totals_cents.get('P2', 0))}；"
        "同一差额多规则命中只兑现一次）。")
    add(f"- 已全退剔除订单：{base.excluded_refunded or '无'}。")
    add(f"- 财务准备额上限 {_yuan(plan.provision_limit_cents)} 元，"
        f"现行义务后余量 {_yuan(base.headroom_cents)} 元。\n")
    add("### 压力情景（假设追加生效，不改原始记录）\n")
    add("| 追加挂牌 | 合并义务(分) | 新增承诺(分) | 准备额余量(分) | 是否可行 |")
    add("| --- | --- | --- | --- | --- |")
    for extra in (set(), {"N1"}, {"N3"}, {"N1", "N3"}):
        r = engine.evaluate(as_of, extra_effective=extra)
        add(
            f"| {sorted(extra) or '无'} | {r.union_total_cents} | "
            f"{r.union_total_cents - base.union_total_cents} | "
            f"{r.headroom_cents} | {'是' if r.headroom_cents >= 0 else '否'} |"
        )
    add("")

    # 7. 可同时发布组合
    add("## 7. 可同时发布的价档组合\n")
    add("单渠道各自可行不等于整组可行；组合按同一次发布的合并保价义务考核。\n")
    add("| 组合 | 新增承诺(分) | 合并义务(分) | 发布后余量(分) | 在准备额内 |")
    add("| --- | --- | --- | --- | --- |")
    for b in plan.bundles:
        add(
            f"| {list(b.quote_ids) or '（不新增发布）'} | {b.incremental_cents} | "
            f"{b.total_union_cents} | {b.headroom_after_cents} | "
            f"{'是' if b.within_provision else '否'} |"
        )
        for note in b.notes:
            add(f"  - {note}")
    add("")
    add("## 8. 结论\n")
    if plan.blocked:
        add("以下价档保留在求证区间，不进入本轮发布：")
        for qid, reasons in plan.blocked.items():
            add(f"- **{qid}**：" + "；".join(reasons[:2]))
    add("- 已生效挂牌产生的保价按原合同处理，兑现以退款回执为准并留痕。")
    add("- 受理中的 E2 等效（deck/seat-plus）在公告前不能释放任何保价占用。")
    return "\n".join(lines)


def _json_default(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(type(obj))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶梯票价挂牌复核报告")
    parser.add_argument("--records", default="records")
    parser.add_argument("--as-of", type=int, default=160)
    parser.add_argument("--market", default="east")
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)

    store = RecordStore(Path(args.records))
    if args.json_path:
        plan = plan_round(store, args.as_of, args.market)
        Path(args.json_path).write_text(
            json.dumps(asdict(plan), ensure_ascii=False, indent=2,
                       default=_json_default),
            encoding="utf-8",
        )
        print(f"已写出 {args.json_path}")
    else:
        print(render_markdown(store, args.as_of, args.market))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
