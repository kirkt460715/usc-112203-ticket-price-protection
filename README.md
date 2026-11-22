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

## 核对资料

运行环境为 Python 3.11，使用标准库，无需下载依赖。用 `python3 -m unittest discover -v` 核对发行资料的完整性与约定的输入关系。
