# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## M8 换卡并账

- `GET /api/recon`：模块索引，返回案卷清单。
- `POST /api/recon/cases`：基于内置换卡演示批（或 body 中 `csv`、`parameters`）走 M1→M4 真实导入路径建立案卷，返回分卡现行结果与 `PROVISIONAL` 候选链；同输入幂等复用同一 `case_id`。
- `GET /api/recon/cases/<id>`：返回案卷当前唯一同人权益结果 `current`、候选/并账链 `links`、补证版本 `versions`、原始事件号 `raw_event_ids`。
- `POST /api/recon/cases/<id>/claim`：body 为 `{"command_id","claim":{"business_day","old_card","new_card","replaced_at?"}}`。补证合法则候选转正并账，返回 `applied=true` 与新版本；同一 `command_id` 重试返回 `applied=false` 与同一版本，不重复减免。一卡已在确认链中再次补证返回 `CARD_ALREADY_MERGED`。
- `POST /api/recon/cases/<id>/expire/<link_id>`：候选到期，释放预占额度，维持分卡结果。

每个版本快照含 `before_paid/after_paid/refund`、逐行程 `changes`（字段级 from→to）、两卡 `card_view` 与 `quota_ledger`（两笔分卡额度释放 + 一笔同人共享额度占用）。金额全程 Decimal 字符串，满足 基础票价=应扣+换乘优惠+封顶减免 的分币守恒。
