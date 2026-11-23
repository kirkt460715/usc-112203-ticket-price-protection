# 电竞赛事阶梯票价的需求识别与挂牌限制

报价 price 和 fee 为整数货币单位，退款回执与实收 paid_cents 为分。补录库存不用修改原记录。

records 保存可离线使用的合成业务记录。每行一个 UTF-8 JSON 对象，id 在各数据表内唯一，跨表键沿用业务记录中的标识。catalog.json 保存行数及原始文件校验值。

| 记录 | 业务含义 |
| --- | --- |
| quotes.jsonl | 每人完整挂牌金额为 price 加 fee，时间为该轮开售后的分钟。 |
| inventory.jsonl | held 表示暂时锁位，available 是渠道实际还能买的数量。seen 是票务台收到时间。 |
| journeys.jsonl | 开始查询到交易结束，实收票据金额用分。 |
| equivalence.jsonl | 等效只适用于明示的地区和套餐版本。 |
| protection.jsonl | 期内的旧订单遇到同等含费挂牌降价可退差；不能给已退全款重复补。 |
| next_quotes.jsonl | 预备挂牌与实际公示是不同事件。 |
| refunds.jsonl | 已退回款的记录，退款为全额或特定保价来源下的部分。 |
| postings.jsonl | 同一报价可能发令、已生效、待回执。时钟以挂牌轮次为准。 |
| equivalence_notices.jsonl | 公告后的公布时间和兑现市场；受理中的变更不能抢先释放保价占用。 |
| provision_limits.jsonl | 向财务确认的本轮保价准备额，单位为分。 |

## 核对资料

运行环境为 Python 3.11，使用标准库，无需下载依赖。用 `python3 -m unittest discover -s tests -v` 核对发行资料的完整性与约定的输入关系。

## 调价复核能力（pricing 包）

`pricing/` 在不修改原始记录的前提下，把报价、库存、订单旅程、等效公告、
保价规则与挂牌台账组合成一轮调价复核：

| 模块 | 职责 |
| --- | --- |
| `store.py` | 不可变记录仓；按 `as_of` 重建当时可见事实，迟到的退款、补放、更正不倒填，补录库存只新增版本 |
| `evidence.py` | 区分售罄、短暂锁位、渠道未放票、无人下单四类需求证据；单列未满足尝试与见面会污染 |
| `sensitivity.py` | 同开售阶段、同权益口径的可比组、可辨识价格区间与候选准入；区间外价位保留在求证区间 |
| `protection.py` | 逐单保价核算：含费比较、原渠道/全渠道口径、等效规则效力、全退剔除、多承诺同一差额只兑现一次 |
| `posting.py` | 发令与生效分离的挂牌台账，识别未执行挂牌及其失效依据 |
| `engine.py` | 枚举可同时发布的价档组合，按同一次发布的合并义务考核财务准备额，输出净收入与应补款区间 |
| `report.py` | Markdown/JSON 复核报告的命令行入口 |

常用命令：

```bash
python3 -m unittest discover -s tests -v          # 资料完整性 + 业务规则测试
python3 -m pricing.report --records records --as-of 160
python3 -m pricing.report --records records --as-of 130   # 更正到达前的历史回看
python3 -m pricing.report --records records --as-of 160 --json plan.json
```

第 9 轮的复核过程与结论见 `docs/decision_round9.md`。
