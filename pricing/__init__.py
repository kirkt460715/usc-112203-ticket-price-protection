"""电竞赛事阶梯票价：需求识别、保价核算与挂牌编排能力。

模块划分
--------
store       不可变记录仓，按 as_of 重建当时可见的事实（迟到凭证、更正不回灌）
evidence    售罄 / 短暂锁位 / 渠道未放票 / 无人下单 四类需求证据
sensitivity 同开售阶段、同权益口径的可比组与可辨识价格区间
protection  保价规则、等效公告、去重与逐单差价
posting     发令与生效分离的挂牌台账
engine      候选价档准入、发布组合、准备额约束与净收入区间
"""

from .store import RecordStore
from .evidence import build_evidence
from .sensitivity import build_groups, evaluate_candidate
from .protection import ProtectionEngine
from .posting import PostingLedger
from .engine import plan_round

__all__ = [
    "RecordStore",
    "build_evidence",
    "build_groups",
    "evaluate_candidate",
    "ProtectionEngine",
    "PostingLedger",
    "plan_round",
]
