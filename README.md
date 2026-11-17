# 公交刷卡换乘优惠与日封顶回算

供公交清分中心复核员核对原始记录、处理差异并封存证据。

## 启动

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

打开 http://localhost:18245 。首次进入自动运行仓内脏样例。工具条可重跑、切换样例或上传CSV。SQLite保存在命名卷records中，重建服务保留批次。

## 模块

- 1. 刷卡采信：`/taps`，`/api/taps`。
- 2. 线路换乘：`/routes`，`/api/routes`。
- 3. 票价版本：`/fares`，`/api/fares`。
- 4. 行程配对：`/journeys`，`/api/journeys`。
- 5. 优惠回算：`/discounts`，`/api/discounts`。
- 6. 清分对账：`/clearing`，`/api/clearing`。
- 7. 日结归档：`/settlement`，`/api/settlement`。
- 8. 换卡并账：`/recon`，`/api/recon`（坏卡换卡同人权益候选、补证并账与幂等重试）。

## 换卡并账裁决（M8）

乘客当天坏卡换卡，旧卡行程与新卡换乘被当成两张卡：换乘优惠中断、封顶额度重新起算。换卡凭证常次日才到，但当天服务结果必须按时交出。

**裁决：凭证未齐时先保留候选权益，不做正式并账。**

1. 当天按分卡现行结果交付；同业务日时间相邻的新旧卡生成 `PROVISIONAL` 候选链，预占一份同人日额度并挂账，不产生正式减免。
2. 补证到达后候选转正（`CONFIRMED`），两卡合成一条时间序卡链、共享一个日封顶额度，形成该补证版本下的唯一同人权益结果，并给出退差。
3. 每次补证输出不可变版本快照，差异明细逐行程列出两卡的换乘锚点、换乘优惠、封顶减免、应扣与日累计变化，以及两卡封顶余量口径变化。
4. 额度账本先释放两笔分卡 `CARD_DAY` 预占，再登记一笔同人 `PERSON_DAY` 占用，两卡不重复占用同一笔额度。
5. 同一 `command_id` 补证重试返回同一版本（`applied=false`），不重复减免；原始刷卡记录与来源行不改写，全程可追溯。
6. 凭证始终不到则候选到期（`EXPIRED`），释放预占，维持分卡结果。

接口：`POST /api/recon/cases` 建案卷、`GET /api/recon/cases/<id>` 取现行结果、`POST /api/recon/cases/<id>/claim` 补证、`POST /api/recon/cases/<id>/expire/<link_id>` 候选到期。

## 核心规则

换乘按上次有效付费行程的时间窗计算，优惠与日封顶按确定顺序裁决并保留分币守恒。

## 演示

1. 打开首页，核对当次计算、差异和隔离原因。
2. 切换模块查看源行及裁决依据。
3. 重跑相同样例，批次号与行数保持一致。
4. 复核后导出一份证据CSV。版本已变化的复核提交返回409。

## 输入与输出

输入列见 `docs/DATA-DICTIONARY.md`。项目包含dirty.csv、clean.csv、legacy.csv，统一走实际导入路径。上传限制4MB，拒绝不带时区的时间。业务键按字符串保留，Decimal参与计算，空值不会当零。

`POST /api/runs` 接收sample或csv；`GET /api/runs`返回历史批次；`POST /api/runs/<id>/review`需要version与decision；`GET /api/runs/<id>/archive`返回唯一证据导出。

## 范围

样例均为虚构。单机复核工具不连接生产系统；不提供登录、远端数据库或自动支付。内存计算按4MB输入上限控制，SQLite事务保证结果完整写入。

## 目录

app为服务与规则，sample_data为输入，tests为测试，docs为定义与深度登记。
