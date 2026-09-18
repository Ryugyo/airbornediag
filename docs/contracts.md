# 输入输出契约

本文件说明故障记录与诊断报告的字段语义、缺失值约定和格式版本规则。**权威的字段约束以 Schema 文件为准，本文件不复制完整字段清单**；两者不一致时以 Schema 为准并应修正本文件的说明。

| 内容 | 位置 |
|---|---|
| 输入契约（故障记录） | [schemas/fault-record.schema.json](../schemas/fault-record.schema.json) |
| 输出契约（诊断报告） | [schemas/diagnostic-report.schema.json](../schemas/diagnostic-report.schema.json) |
| 文献来源登记表 | [knowledge/source-registry.json](../knowledge/source-registry.json) |
| 输入示例 | [examples/](../examples/) |
| 期望报告 | [tests/fixtures/](../tests/fixtures/) |
| 结构与引用校验 | `python scripts/validate_contracts.py` |

首批诊断场景及判据依据见 [scenarios.md](scenarios.md)。

## 格式版本

当前格式版本为 `0.1.0`，记录在每个文件的 `schema_version` 字段中。该字段为固定值而非范围，消费方应按版本选择对应的 Schema 校验。

| 变更类型 | 版本位 | 例子 | 消费方要求 |
|---|---|---|---|
| 破坏性 | MAJOR | 删除或重命名字段、改变类型或取值域、改变既有字段语义 | 必须显式升级，不得按旧版本解释 |
| 兼容性 | MINOR | 新增可选字段、新增枚举取值 | 同步更新 Schema；按枚举分支处理时须有默认分支 |
| 修订 | — | 文字说明、示例或注释的变化 | 不改变格式版本 |

Schema 中的对象均声明 `additionalProperties: false`，本版本未定义的字段会被判为不合规。因此**新增字段虽然是 MINOR，也必须同步更新 Schema 文件**，不能只改数据。

## 输入契约：故障记录

一条记录描述一次检测及其可选的补充证据。首版优先支持「芯片型号 + 检测对象 + 检测标识 + 通过/失败结果」这一最小形态，只有检测结论、没有任何寄存器快照的记录同样合法。

顶层字段的语义：

| 字段 | 语义 |
|---|---|
| `schema_version` | 格式版本，固定值 |
| `record_id` | 记录标识，同一批记录内唯一 |
| `origin` | 记录来源。`simulated` 为按手册构造的模拟案例，`manual_capture` 为人工采集，`device_log` 为设备记录。模拟案例与真实设备记录必须可区分 |
| `captured_at` | 整份记录的采集时刻，须含时区偏移。缺省表示未知 |
| `device.model` | 芯片型号。寄存器定义和故障判据必须与该型号及手册版本对应 |
| `test` | 检测上下文。`result` 只有 `pass` 与 `fail`，未执行的检测不产生记录 |
| `observations` | 补充证据，可缺省。见下节 |

### 观测的种类

`observations` 是一个数组，`kind` 是唯一的扩展点，首版支持四种：

| kind | 承载的证据 |
|---|---|
| `register` | 寄存器原始值或已解码位域 |
| `can_frame` | CAN 报文的发送与接收，含是否被应答 |
| `spi_transfer` | SPI 传输的发送值、实际接收值与完成情况 |
| `timeout` | 等待某事件未在限时内完成 |

新增外设时在此扩展 `kind`，不改变既有种类。

### 模块限定命名

`register` 必须写成 `<模块实例>.<寄存器名>` 的形式，例如 `CAN_A.ECR`。**不得只写裸助记符**：同一本手册中 `ESR` 同时是 e200z6 内核的 Exception Syndrome Register（第 3 章）与 FlexCAN 的 Error and Status Register（22.3.3.6），裸名必然产生歧义。

### 缺失表示未知

这是本契约最重要的约定，由文档、校验和诊断规则共同保证，不能依赖「用了列表结构」就自动成立。

1. **字段缺省即为未知**，不等于零值、正常或检测通过。例如 `observations` 整个缺省表示未采集，与空数组含义相同。
2. **`fields` 内部的字段缺省同样是未知**。一次寄存器快照可以只记录部分位域；未记录的位域不能用零值补齐。
3. **`value` 与 `fields` 二选一**。`register` 观测必须提供 `value` 或非空 `fields`，首版不同时提供两者。
4. **缺失的后果必须被显式记录**。诊断规则在需要某字段而该字段缺失时，不得跳过判据，必须把缺失项和它导致的不可判定写入报告的 `insufficient_data`。

### 采集语义

寄存器的采集背景只保留会影响判断的部分，无法确定时允许标为未知，不要求模拟数据编造采集过程。

| 字段 | 语义 |
|---|---|
| `at` | 该条观测的时刻。**数组顺序不承载时序语义**，需要先后关系时必须使用本字段 |
| `semantics` | `since_last_read` 表示读清除位，内容只反映上次读取以来的情况；`instantaneous` 表示瞬时状态位。缺省表示未知 |
| `covers_since` | 仅当 `semantics` 为 `since_last_read` 时有意义，表示被清除的标志所覆盖区间的起点。缺省表示未知 |
| `capture_id` | 同一次寄存器读取的关联标识 |

**`capture_id` 只表示来自同一次读取动作，不表示不同寄存器之间天然同步。** 不同寄存器的读取仍有先后，且部分寄存器在读取时就会改变自身状态。需要联合多个寄存器才能得出的结论，必须说明其依赖的采集关联；关联不成立或无法确定时，该结论不得进入 `confirmed_states`。

`semantics` 之所以必须显式存在，是因为 MPC5554 的 FlexCAN `CANx_ESR` 中，`BIT1ERR`～`STFERR` 为读清除位，手册明确要求不得投机读取并将覆盖该寄存器的 TLB 项配置为 guarded。**读取动作本身会销毁证据**，因此这类位的一次性快照不能当作当前状态反复核对。同一次 `CANx_ESR` 读取应按语义拆成两条观测：读清除位用 `since_last_read`，`FLTCONF`、`TXWRN` 等状态位用 `instantaneous`。

### 文献引用

诊断结论通过 `basis.source` 引用来源登记表中的 `id`，不在结论中重复文献的完整元数据。`basis.locator` 记录章节、表或图编号。

来源登记表集中维护文献的标题、编号、版本、日期、适用芯片与勘误情况。原始资料不随代码提交，核对时以 `order_number` 与 `revision` 为准。

## 输出契约：诊断报告

### 程序保留部分与分析部分

报告是一份完整文件，分为两段，边界必须清晰：

| 部分 | 内容 | 约束 |
|---|---|---|
| 顶层 | `device`、`test`、`observations` | 由主程序按输入记录的字段值保留，**模型不得改写**。输入缺省 `observations` 时，报告使用已解析出的空数组 |
| `analysis` | 全部分析结论 | 输入中不存在、由诊断工具与模型产出的内容 |

校验脚本会逐字段比对顶层三项与输入记录，不一致即判为失败。`provenance.report_status` 区分 `expected`（设计预期示例，不是工具运行结果）与 `actual`（工具实际产出），避免把预期样例当作实测结果。

### 分析结论

`analysis` 的各字段含义：

| 字段 | 语义 |
|---|---|
| `fault_state` | 是否存在**由手册判据确认的外设故障状态**。注意它与 `test.result` 无关：检测报失败不等于确认了某个故障状态，确认了故障状态也不代表检测一定报失败 |
| `root_cause` | 根因判定层级。首版只开放 `candidates_only` 与 `not_determined`，**不开放 `confirmed`**，因为尚无已确认根因的结构与实现 |
| `confirmed_states` | 规则确认的状态，每条必须带观测证据引用与文献依据 |
| `candidate_causes` | 待验证的候选原因 |
| `inconsistencies` | 输入中自相矛盾之处 |
| `insufficient_data` | 缺失信息及其导致的不可判定 |
| `recommended_checks` | 后续检查建议，含无案例证据支持的通用排查项 |

三条结构性约束：

1. **`confirmed_states` 的门槛是前提已满足**，而不是「结论比较有把握」。结论的适用边界写进 `statement` 语句本身，不另设条件字段。前提未被观测或无法确定的推断一律不得进入该数组，相关问题写入 `insufficient_data`。
2. **`candidate_causes` 只引用观测**，即 `supporting` 与 `contradicting` 中只出现观测 id。首版不允许候选原因引用其他结论或彼此引用，避免多层交叉引用。没有案例证据支持的通用排查项不列为候选原因，放在 `recommended_checks`。
3. **缺失信息只出现在 `insufficient_data`**，候选原因不含缺失信息字段，每项只有 `missing` 与 `reason`，`reason` 需说明该信息为何影响判断以及缺少它导致哪些内容无法判定。

## 校验方式

校验分两层，两层都通过才算校验通过：

| 层 | 负责 | 实现 |
|---|---|---|
| Schema | 字段、类型、枚举、取值域、分支约束（`oneOf`/`allOf`/`not`/`unevaluatedProperties`） | `jsonschema` 实际执行 `schemas/` 下的两份 Schema |
| 跨文档关系 | Schema 表达不了的部分 | `scripts/validate_contracts.py` |

Schema 是校验规则的唯一来源，脚本不重复维护字段结构。脚本只补充以下内容：观测 id 在一条记录内唯一、`record_id` 在 `examples/` 内唯一、报告引用的观测 id 存在、结论引用的文献来源已登记、报告的 `device`/`test`/`observations` 与输入记录一致、每个输入记录都有对应的期望报告。

跨文件 `$ref`（报告契约引用输入契约的 `$defs`）由校验脚本预先读入本地 Schema 并注册到本地引用表解析，不访问网络。

## 已知限制

以下内容在本版本中**未被机器校验**，属于已知缺口：

1. **日期时间格式未被执行。** `captured_at`、`at`、`covers_since` 声明了 `format: date-time`，但 `format` 关键字只在安装了对应格式校验器的环境中才会执行，当前依赖集不含该校验器。因此这三个字段只按字符串类型校验，**"须含时区偏移"这一要求目前没有机器检查**，只能依靠人工核对。校验脚本在通过时会打印该说明，不得据此宣称日期格式已检查。
2. 校验通过**不代表诊断结论正确**，也不代表字段取值在物理上合理。诊断质量需要独立的预期结果评估。
3. 场景判据、支持范围与明确不支持的结论见 [scenarios.md](scenarios.md)。本任务不实现寄存器解析与诊断规则。
