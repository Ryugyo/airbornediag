# AirborneDiag 开发说明

本文记录经过实际验证的环境配置、安装、运行和测试方法。

标注为**未验证**的步骤尚未在目标环境执行，不能视为通过。

## 环境基线

| 项 | 约定 |
|---|---|
| Python | 3.9（`requires-python = ">=3.9"`） |
| 依赖管理 | PEP 621 `pyproject.toml` + setuptools 构建后端 + 标准库 `venv` + `pip` |
| 运行期依赖 | `jsonschema>=4.18,<5`（执行 `schemas/` 下的契约校验） |
| 开发依赖 | `pytest`（`[project.optional-dependencies] dev`） |
| 模型调用 | 标准库 `urllib`，不引入第三方依赖 |
| 知识检索 | 标准库 `sqlite3` 的 FTS5，不引入向量模型、分词库或独立数据库服务 |
| 版本来源 | `src/airbornediag/__init__.py` 的 `__version__`，由 `pyproject.toml` 动态引用 |

**为什么是 3.9**：RDC300I 系统 Python 为 3.9.9，且已在该版本上验证可创建独立虚拟环境。首版采用与板端一致的 Python 次版本，避免出现"Windows 验证通过但板端不通过"的情况。

**为什么引入 `jsonschema`**：契约以 JSON Schema 声明，校验必须实际执行 Schema，而不是在脚本里再写一套结构规则。下界 `4.18` 是 `registry`/`resolver` API 的引入版本，跨文件 `$ref` 的本地解析依赖该 API；上界 `<5` 用于防止主版本变更改变解析行为。该依赖及其传递依赖在板端离线安装，见下文 wheelhouse 一节。

**已知代价**：Python 3.9 已停止维护。`jsonschema` 当前支持 3.9，其传递依赖 `rpds-py` 是编译扩展，不再是纯 Python 包，板端离线安装需要对应平台（Linux ARM64 / CPython 3.9）的 wheel；后续如需引入要求更高版本的依赖，需要先评估板端能否获得相应解释器，再统一升级，不单独升级 Windows 侧。

**`format` 未完全生效**：Schema 中的 `format: date-time` 需要额外的格式校验器才会执行，本工程未引入该校验器，因此日期时间只按字符串类型校验。该限制在校验脚本的输出与 [contracts.md](contracts.md) 的"已知限制"中均有说明。

**推理环境不在本工程内**：CANN、PyTorch、MindIE 与模型权重由板端推理环境管理，不进入本工程的依赖与仓库；应用只通过 HTTP 调用已部署的服务，见下文"模型服务调用（MindIE）"。

## Windows 开发环境

### 前置条件

本机默认 `python` 指向 3.14，`py -3.9` 不可用，需要显式指定 3.9 解释器。推荐用 uv 获取独立解释器（不修改系统 Python）：

```powershell
uv python install 3.9
python3.9 -V          # 安装后由 %USERPROFILE%\.local\bin\python3.9.exe 提供
```

### 建立环境并安装

```powershell
python3.9 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 验证记录（Windows，已执行）

本机实际执行结果：

| 项 | 结果 |
|---|---|
| 解释器 | Python 3.9.25（uv 独立发行版） |
| venv 内 pip | 23.0.1 |
| 安装结果 | `airbornediag-0.1.0`（可编辑安装）+ `pytest-8.4.2` + `jsonschema-4.25.1`（及 `attrs`/`jsonschema-specifications`/`referencing`/`rpds-py`） |
| `airbornediag --help` | 退出码 0，输出 usage 与参数说明 |
| `airbornediag --version` | 退出码 0，输出 `airbornediag 0.1.0` |
| 仓库外目录执行 | 退出码 0，确认命令来自安装而非当前目录 |
| `python -m pytest` | 329 passed（`test_cli.py` 4 + `test_contract_examples.py` 10 + `test_llm_prompt.py` 2 + `test_llm_config.py` 15 + `test_llm_client.py` 17 + `test_llm_cli.py` 12 + `test_llm_diagnosis.py` 30 + `test_knowledge_model.py` 37 + `test_knowledge_index.py` 33 + `test_knowledge_cli.py` 15 + `test_knowledge_search.py` 55 + `test_mcu_flexcan2.py` 24 + `test_mcu_dspi.py` 14 + `test_mcu_tools.py` 15 + `test_diag_cli.py` 10 + `test_report_cli.py` 36） |
| `python scripts/validate_contracts.py` | 退出码 0，并打印日期时间格式未执行的说明 |
| `airbornediag llm --help` | 退出码 0，输出子命令说明 |
| `airbornediag llm --prompt "你好"`（本机无 MindIE 服务） | 退出码 3，打印端点与连接失败原因；**未返回任何模拟回答** |

Windows 侧不运行 Qwen，因此上表中模型调用的验证只覆盖到"失败被如实上报"，真实调用未执行。

Windows 侧解释器为 3.9.25，板端为 3.9.9，次版本一致、补丁版本不同；两端各自独立验证，板端记录见下文。

## 命令行使用

安装后可直接调用：

```powershell
airbornediag --help
airbornediag --version
airbornediag llm --help      # 模型服务调用
airbornediag kb --help       # 知识库构建与查询
airbornediag diag --help     # MCU 诊断工具
airbornediag report --help   # 完整诊断流程
```

当前提供使用说明、版本查询与 `llm`、`kb`、`diag`、`report` 四个子命令；不带参数时打印帮助并返回退出码 0。

四个子命令的关系：`llm`、`kb`、`diag` 各自只做一件事，可独立使用；`report` 是完整流程，内部依次用到输入校验、诊断工具、知识检索与模型调用。调试单个环节时用前三个，出报告时用 `report`。

各子命令的输出约定一致：**结果走标准输出，诊断信息（端点、索引路径、耗时、保存路径等）走标准错误**，便于管道使用。`report` 的结果是中文简述，完整报告以 JSON 落盘。

未激活虚拟环境时使用显式路径：

- Windows：`.\.venv\Scripts\airbornediag.exe`
- Linux：`.venv/bin/airbornediag`

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试配置位于 `pyproject.toml` 的 `[tool.pytest.ini_options]`，`testpaths = ["tests"]`。

`tests/test_cli.py`（4 项）覆盖：帮助输出与退出码、版本输出与退出码、版本号与安装元数据一致、`console_scripts` 入口点已注册。

`tests/test_contract_examples.py`（10 项）覆盖契约的格式与引用：示例与期望报告通过校验、输入缺省 `observations` 时报告回显为空数组，以及 5 项负例（证据引用不存在、来源未登记、回显字段被改写、观测 id 重复、`register` 观测缺 `value` 与 `fields`）、1 项未知观测种类、1 项跨文件 `$ref` 确实生效、1 项校验脚本如实声明日期时间格式未执行。

`tests/test_llm_prompt.py`（2 项）、`tests/test_llm_config.py`（15 项）、`tests/test_llm_client.py`（17 项）、`tests/test_llm_cli.py`（12 项）覆盖模型调用：ChatML 提示词格式、配置优先级与取值校验、请求体是否与历史脚本一致、回答提取、连接/超时/HTTP/响应格式四类失败，以及子命令的输出与退出码。

`tests/test_llm_diagnosis.py`（30 项）覆盖诊断提示词与回答解析：

- 提示词：ChatML 格式与 system 要求、列出全部观测 id、写明记录来源（模拟案例与真实设备记录措辞不同）、带工具状态与出处并注明不得改写、缺失与不支持小节、知识条目含 id 与出处；空小节明说「没有」而不是省略。
- 必要内容不截断、不省略：单条观测的字段值、观测条数（40 条）、确认状态条数（30 条）与知识正文（超过原先 240 字符上限）都按原样完整列出。
- 规模控制：整体受 `PROMPT_CHAR_LIMIT` 约束，超限时只整条移除未被工具结论引用的补充知识并在提示词内写明省略条数，被引用的知识、状态、缺失信息与不支持项仍在；被引用的知识本身放不下（必要内容超限）或补充知识全部移除后仍超限时直接报错。
- 解析：合法回答（含被代码围栏包裹的回答）、缺键、非 JSON、JSON 非法、空 `statement`、候选原因缺字段、候选原因的 `supporting` 为空数组、引用不存在的观测、记录无观测时任何引用都算编造；契约没有的顶层键不进入报告但记入 `ignored_keys`；报错信息附回答开头（超长时截断）。

`tests/test_report_cli.py`（36 项）是完整流程的端到端测试，**除模型服务外全部使用真实实现**（真实 Schema 校验、真实诊断工具、对 `knowledge/curated/` 建真实索引），模型服务用测试进程内的假服务代替。覆盖：CAN 与 DSPI 两份示例记录的程序组装部分与期望报告一致（状态按内容与证据配对，不比较映射后的 id 字面值）、**`examples/` 下全部案例经参数化逐条比对期望报告**（记录目录中新增文件即自动纳入）、状态 id 唯一且带检测对象前缀、规则 id 后缀集合一致、候选原因由程序编号、被引用的知识条目确实进入提示词、证据不足时允许不提出候选原因、同一外设两个实例的状态 id 可区分、回答里的多余字段不进报告但提示；**部分对象不支持的记录照常分析并退出码 0，全部对象都不支持的记录退出码 9、不产出报告且假服务收到 0 个请求**；失败路径另覆盖不支持的芯片与不合规记录（都在调用模型前结束、假服务收到 0 个请求）、索引缺失、连接失败、超时、HTTP 错误、响应格式异常、回答非 JSON、引用不存在的观测、缺 `recommended_checks`，以及运行期报告自检被触发时的退出码。报告落盘与终端简述另有一组用例：默认模式在 `results/` 下生成带记录号与运行时间的报告、标准输出为简述而不是完整 JSON、同一秒重复运行追加序号而不覆盖、`--out` 只写显式路径且不另建 `results/`（已存在则覆盖）、报告标识里的分隔符不会把文件写出 `results/`、`results/` 建不出来与 `--out` 指向不存在目录两种情况都报错并保持标准输出为空、简述把程序结论与模型建议分节标注且没有候选原因时不写成设备正常。**假服务只返回文本，工程侧对回答的解析与校验都是真的。**

知识库测试分四组：

- `tests/test_knowledge_model.py`（37 项）：工程内真实知识文件可读且 id 唯一、两类外设均有条目、知识条目不引用模拟案例的预期报告编号，以及文件级/条目级格式错误、重复 id、出处未登记、条目芯片超出来源范围等负例。
- `tests/test_knowledge_index.py`（33 项）：分词与 MATCH 表达式构造（含 FTS5 语法字符不破坏查询）、构建与查询往返、中文子串匹配、大小写、跨字段取交集、过滤与条数限制、重复重建不产生重复条目、重建拾取改动，以及索引缺失/损坏/版本不符三类异常。
- `tests/test_knowledge_cli.py`（15 项）：`kb` 子命令的输出分流、过滤与条数限制的回显、无匹配返回空结果、退出码 2/7/8，以及一次经命令行入口的子进程端到端构建与查询。
- `tests/test_knowledge_search.py`（55 项）：**直接使用 `knowledge/curated/` 的真实知识**，按代表性查询验证检索效果——中英混排查询、中文子串匹配、`docs/scenarios.md` 依据核对情况表中的全部字段名均可检索到条目、芯片与外设过滤、条数限制，以及无匹配返回空结果。

前两组与 CLI 组使用 `tests/knowledge_helpers.py` 构造的最小知识文件，不依赖真实知识内容；检索效果组使用真实知识文件。两者索引都建在临时目录中。

这些测试通过测试进程内的假 HTTP 服务（`tests/fake_mindie.py`）走真实的 HTTP 调用路径，**工程代码中没有任何模拟或降级分支**：调用失败一律报错，不会返回替代回答。假服务只存在于 `tests/`，不属于产品代码。

说明：可编辑安装会把 `src` 加入 `sys.path`（`.venv` 中的 `__editable__.*.pth`），此时 `src/airbornediag.egg-info` 与 `site-packages` 中的 `dist-info` 都会被识别为发行版，入口点可能被枚举多次。该现象不影响命令执行，测试按集合比较。构建产物已由 `.gitignore` 排除。

## 契约校验

契约的字段、类型、枚举与分支约束由 `schemas/` 下的 JSON Schema 声明，用 `jsonschema` 实际执行；Schema 表达不了的跨文档关系由 `src/airbornediag/report.py` 补充，**`scripts/validate_contracts.py` 调用的是同一份实现**，`report` 子命令在输出报告前也用它自检，两处要求不会各自漂移。两层都通过才退出码 0。

```powershell
.\.venv\Scripts\python.exe scripts\validate_contracts.py
```

脚本接受 `--root 工程根目录`，默认使用脚本所在工程的根目录；跨文件 `$ref` 从本地 Schema 解析，不访问网络。Linux 下将 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

脚本需要先导入工程包（`from airbornediag.report import ...`），因此**必须先完成上面的安装步骤**；未安装时脚本会在导入处报 `ModuleNotFoundError`，而不是静默跳过校验。

校验通过**不代表诊断结论正确**，只代表格式与引用成立。脚本会在通过时一并说明哪些约束没有执行（当前为日期时间格式），详见 [contracts.md](contracts.md) 的"已知限制"。

## 知识库构建与检索

知识检索不依赖模型服务，可单独构建和查询。使用 Python 标准库 `sqlite3` 的 FTS5，**不引入向量模型、分词库或独立数据库服务**，因此 `wheelhouse/` 无需变更（板端 SQLite 3.37.2 的 FTS5 已在板端确认可用）。

### 知识文件

- 知识条目：`knowledge/curated/*.json`，**是源文件，随代码提交**。每条包含 `id`、`kind`、`title`、`body`、`locator`（章节号与表/图编号）与可选的 `quote`（手册原文摘录）。
- 来源登记：`knowledge/source-registry.json`，条目通过 `source_id` 引用。读取时会核对该 `source_id` 已登记、且条目的 `chip` 在该来源的 `applies_to` 范围内。
- 原始手册 PDF：`knowledge/sources/`，**不提交**（`.gitignore` 已排除）。
- 生成的索引：`knowledge/index/knowledge.sqlite3`，**不提交**，按下面的命令在本地重建。

### 构建与查询

```powershell
# 构建索引（整表重建，可反复执行）
.\.venv\Scripts\airbornediag.exe kb build

# 查询
.\.venv\Scripts\airbornediag.exe kb query "总线关闭"
.\.venv\Scripts\airbornediag.exe kb query "TFUF" --chip MPC5554 --peripheral DSPI --limit 3
```

`kb build` 打印写入的条目数与索引路径。`kb query` 的命中结果（条目 id、标题、芯片/外设、依据、原文、内容）输出到标准输出，索引路径、查询表达式、过滤条件与命中数输出到标准错误。

`kb build` 的参数 `--db`、`--curated-dir`、`--registry` 默认指向工程内的上述路径；`kb query` 的参数为 `--db`、`--chip`、`--peripheral`、`--limit`（默认 5，必须为正整数）。查询文本可省略，此时从标准输入读取。

检索方式：中文按单字切分、英文按词切分，查询时连续的中文合并为一个短语、各词之间取交集。因此中文按子串命中（`线关` 能查到含「总线关闭」的条目），寄存器名不区分大小写（`fltconf` 与 `FLTCONF` 等价），而查询中的标点与 `_`、`.` 等字符只作分隔符——它们不会造成 FTS5 语法错误，但也不参与匹配。

`kb` 子命令的退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功；**无匹配也是成功**，命中为空时不输出任何结果，只在标准错误说明 0 条 |
| 2 | 参数或配置错误：查询文本为空、查询中没有任何可检索字符、`--limit` 非正整数 |
| 7 | 知识数据错误：知识目录或来源登记表缺失、JSON 非法、字段缺失、id 重复、出处未登记或芯片超出来源范围 |
| 8 | 索引错误：索引不存在（提示先运行 `kb build`）、文件损坏、索引版本与当前代码不符，或运行环境不支持 FTS5 |

重建索引会整表重建（删除并新建虚拟表，在同一事务内写入），因此**重复构建不会产生重复条目**；构建失败时事务回滚，原有索引保持不变。

### 首版限制

- 只做关键词全文检索，不做语义检索、同义词扩展或相关性调参，排序由 FTS5 的 `bm25` 决定。
- 过滤维度只有芯片与外设，且为精确匹配（不区分大小写）。
- 知识条目为整体检索，不按句子或段落进一步分块。
- 检索**不代表诊断结论**：命中条目只是可引用的依据，判据的适用条件与不可推出项仍以 [scenarios.md](scenarios.md) 为准。

### 验证情况

Windows 侧已按上述命令构建并查询通过：索引写入 22 条知识条目，代表性查询（中文、寄存器名、中英混排、按出处回查）均返回预期条目，连续三次构建后索引仍为 22 条、`id` 无重复；退出码 0/2/7/8 各路径均已实际触发确认。检索效果由 `tests/test_knowledge_search.py` 固化。

**板端已验证通过**（2026-09-22）：`kb build` 写入 22 条，`kb query` 命中结果与 Windows 侧一致，详见下文"验证记录（RDC300I）"。

## 诊断工具（diag）

`diag` 读取一条故障记录，按规则分析记录中**已解码的位域**，输出工具结果：确认的状态、输入中的矛盾、因缺失而无法判定的内容，以及本版不支持的范围。每条状态与矛盾带观测证据与出处，缺失信息也带判定依据，出处包含手册定位与对应的知识条目 id。

**不访问模型服务，也不使用知识检索**，可脱离两者单独运行；判据与知识条目的关联由 `basis.knowledge_refs` 表达，需要展开条目内容时用 `kb query`。

### 使用

```powershell
# 可读文本
.\.venv\Scripts\airbornediag.exe diag examples\REC-2026-0918-002.json

# 机器可读
.\.venv\Scripts\airbornediag.exe diag examples\REC-2026-0918-002.json --json

# 换用其他输入契约文件（默认 schemas/fault-record.schema.json）
.\.venv\Scripts\airbornediag.exe diag <记录文件> --schema <schema 文件>
```

结果输出到标准输出，汇总条数与说明输出到标准错误，与其他子命令一致。`--json` 的输出含 `supported` 字段：**它为 `false` 时表示本记录没有任何工具处理过，不能读成「未发现异常」**。

### 支持范围

- 芯片：MPC5554。其他芯片不借用同名外设、寄存器与状态定义，一律报告为不支持。
- 模块实例：手册确认存在的实例，即 FlexCAN2 的 `CAN_A`/`CAN_B`/`CAN_C` 与 DSPI 的 `DSPI_A`～`DSPI_D`。清单之外的实例名（如 `CAN_D`、`DSPI_E`）报告为不支持，不假定该实例存在。
- 观测：只使用 `kind` 为 `register` 且给出了已解码位域（`fields`）的观测。以下三类都不参与判断，但报告方式不同——**已观测而本版没有判据**的位域或寄存器按不支持列出并附观测 id，不得读成未观测；只有原始值（`value`）、寄存器名不带模块实例前缀的观测按不支持报告；`can_frame`/`spi_transfer`/`timeout` 等没有判据的观测种类报告为未使用。
- 同一字段出现多个不同取值时统一报为「存在多个不同取值，无法合并判断」：本版不比较观测时刻、不做跨观测的时序分析，既不挑一个取值当作当前值，也不声称这些取值来自不同采集时刻。
- 判据、取值写法与适用条件见 [scenarios.md](scenarios.md)。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 运行完成；**结果为「未判定」「无结论」也是 0**，是否得出结论要看输出内容 |
| 2 | 记录文件缺失、不是合法 JSON、未通过 Schema 校验，或 `--schema` 指向的文件不可读 |
| 9 | 记录合规，但芯片或全部检测对象不在本版支持范围内，没有运行任何工具 |

3～8 分别属于 `llm` 与 `kb` 子命令，`diag` 不使用。

### 依赖

**未新增依赖。** 输入校验复用运行期已有的 `jsonschema` 与 `schemas/fault-record.schema.json`，工具本身只用标准库，`wheelhouse/` 无需变更。

### 验证情况

Windows 侧已实际执行：`examples/` 下 **10 条示例记录全部退出码 0**，打印出与 [scenarios.md](scenarios.md) 一致的结论与缺失项（清单见 [scenarios.md](scenarios.md) 的示例清单；其中 2026-09-28 新增的 8 条在写入时就逐条核对过工具输出，确认状态、矛盾、缺失信息与不支持项的条数与 [scenarios.md](scenarios.md) 一致）；不支持的芯片记录退出码 9；Schema 不合规、文件缺失、JSON 非法、Schema 文件缺失四条路径均退出码 2 并指出具体路径或原因。判据行为由 `tests/test_mcu_flexcan2.py`、`tests/test_mcu_dspi.py`、`tests/test_mcu_tools.py`、`tests/test_diag_cli.py` 固化，其中 `test_mcu_tools.py` 校验每条依据引用的知识条目 id 在 `knowledge/curated/` 中确实存在。

**板端已验证通过**（2026-09-23）：两份示例记录在 RDC300I 上的输出与退出码与 Windows 侧一致，详见下文"验证记录（RDC300I）"。

## 完整诊断流程（report）

`report` 用一条命令走完输入校验 → 工具分析 → 知识检索 → 模型分析 → 报告输出。各环节的实现与单独执行时相同，编排顺序由程序固定，**不依赖模型自主选择工具**。

**需要 MindIE 服务可用**：模型调用失败、回答不合规或组装出的报告未通过契约校验时**不产出报告**，也不降级为模拟回答或"正常"结论。

### 使用

```powershell
# 报告保存到 results\，终端显示中文简述
.\.venv\Scripts\airbornediag.exe report examples\REC-2026-0918-002.json

# 报告写入指定文件（已存在则覆盖），终端同样显示简述
.\.venv\Scripts\airbornediag.exe report examples\REC-2026-0918-002.json --out report.json
```

约定从工程根目录运行，`results/` 是当前工作目录下的目录，属生成物、不纳入 Git。省略 `--out` 时报告写到 `results/`，文件名形如 `RPT-2026-0918-002_20260928T161234.json`（报告标识 + 运行时间）；目录不存在时自动创建，同一秒内重复运行会追加 `-2`、`-3` 序号，**不覆盖已有报告**。指定 `--out` 时只写该路径，沿用显式路径的既有策略：**不自动创建父目录，已存在则直接覆盖**，也不再另外向 `results/` 保存。

两种情况下终端都显示可读的中文简述（分析对象、故障状态与根因判定、确认状态、矛盾与缺失、候选原因、检查建议、不支持项），完整报告只在文件里。简述直接从最终报告提取，不新增模型调用、不改写结论；程序给出的结论与模型给出的候选原因、检查建议分节标注来源——**没有候选原因不等于没有故障**。进度、检索命中、保存路径与退出原因走标准错误。保存失败时明确报错并返回退出码 2，**不显示简述，也不出现任何成功提示**。

参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `record` | 必填 | 故障记录 JSON 的路径 |
| `--out` | 空 | 报告写入的文件路径；省略时写到当前工作目录下的 `results/`。指定时不建父目录、已存在则覆盖 |
| `--schema` | `schemas/fault-record.schema.json` | 输入契约的 Schema 路径 |
| `--schema-dir` | `schemas` | 契约 Schema 所在目录；报告 Schema 用相对 `$ref` 引用输入 Schema，两者必须同目录 |
| `--db` | `knowledge/index/knowledge.sqlite3` | 知识库索引路径，**需先 `kb build`** |
| `--curated-dir` | `knowledge/curated` | 知识文件目录 |
| `--registry` | `knowledge/source-registry.json` | 来源登记表路径 |
| `--max-knowledge` | 8 | 送入模型的知识条目目标条数，必须为正整数。**工具结论引用的条目不受此限，始终全部送入** |
| `--base-url` | 配置值 | 覆盖模型服务地址 |
| `--max-tokens` | 配置值 | 单次请求最大生成 token 数；**这只限制生成长度，不是输入长度限制** |
| `--timeout` | 配置值 | 单次请求超时秒数 |

### 输出与职责边界

报告中的内容按「谁有依据谁写」划分：

- **程序保留**：`device`、`test`、`observations` 按输入记录原样回显，`confirmed_states`、`inconsistencies`、`insufficient_data`、`unsupported`、`fault_state`、`root_cause` 由工具结论组装，候选原因的 id（`CC-1`、`CC-2`……）由程序编号。**模型不得改写这些内容**，运行期自检会逐字段比对。
- **模型提供**：解释、候选原因与检查建议。候选原因的证据引用只能是记录中已有的观测，引用不存在的观测即判为不合规、流程终止。
- 状态 id 统一写为 `<检测对象>/<规则 id>`（如 `CAN_A/FC-BUSOFF-STATE`）：规则 id 不含模块实例名，同一外设的多个实例会产生同名规则，加检测对象前缀后每条状态都能唯一识别并区分所属实例，规则来源仍可读。
- 模型回答里契约没有的顶层字段不进入报告，但会在标准错误中列出，便于发现模型答了预期之外的内容。

字段语义见 [contracts.md](contracts.md)。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功，报告已产出并保存 |
| 2 | 记录文件缺失、不是合法 JSON、未通过 Schema 校验，契约 Schema 读不到，或报告无法保存 |
| 3 | 连接失败：模型服务未启动或地址不可达 |
| 4 | 请求超时 |
| 5 | HTTP 错误：模型服务返回非 2xx |
| 6 | 响应格式异常：非 JSON、缺少 `text`、`text` 为空数组或类型不符 |
| 7 | 知识文件或来源登记表有问题 |
| 8 | 索引缺失、损坏或版本不符（提示先运行 `kb build`） |
| 9 | 记录合规，但芯片或全部检测对象不在本版支持范围内，**没有调用模型** |
| 10 | 模型回答不合规，或组装出的报告未通过契约校验 |

退出码 3～6 与 `llm` 子命令一致，7～8 与 `kb` 子命令一致。**除 0 以外都不产出报告**；报告无法保存（退出码 2）时报告已经在内存里组装完成，但既不落盘也不显示简述，不留下半份交付物。

退出码 9 只表示**全部**检测对象都不支持（或芯片不支持），此时没有运行任何工具、没有调用模型。记录里只要还有对象能分析，流程照常走完并退出码 0，不支持的那部分记进报告的 `unsupported`：**报告里出现不支持项不等于流程没跑**，两者不要混看。示例 `REC-2026-0928-008` 属于后者。

### 依赖

**未新增依赖。** 复用已有的 `jsonschema`、`urllib`、`sqlite3` 与全部诊断工具，`wheelhouse/` 无需变更。

### 验证情况

Windows 侧已实际执行：

| 命令 | 结果 |
|---|---|
| `report --help` | 退出码 0，退出码说明与参数完整 |
| `report` 全流程（假模型服务） | 由 `tests/test_report_cli.py` 的 36 项覆盖，含正常流程、报告落盘与简述、全部关键失败路径与部分/全部不支持的行为差异 |
| `python -m pytest` | 329 passed（完整清单见上文"验证记录（Windows，已执行）"） |

`diag` 的输出就是报告里程序组装的那部分（`confirmed_states`、`inconsistencies`、`insufficient_data`、`unsupported`、`fault_state`、`root_cause`），`diag` 的验证情况见上文"诊断工具（diag）"。

### 报告保存与终端简述（2026-09-28，已执行）

Windows 侧对真实 MindIE 服务执行 `report examples/REC-2026-0928-007.json` 两次，均退出码 0：报告分别写入 `results\RPT-2026-0928-007_20260928T164458.json` 与 `…T164520.json`，第一份未被覆盖；标准输出是中文简述（分析对象、故障状态「已确认」、根因「未确定」、6 条确认状态、候选原因写明「本次未提出候选原因，根因尚未确定；不表示设备没有故障」、4 条检查建议、不支持项为「无」），标准错误末行为实际保存路径的绝对路径。`results/` 已由 `.gitignore` 排除，`git status` 中不出现。

落盘、重名追加序号、`--out` 只写显式路径、两类保存失败路径与简述措辞由 `tests/test_report_cli.py` 的模拟服务用例覆盖，未为这些机械路径额外调用真实模型。**本次只验证了保存与展示，诊断结论是否正确仍以工具结论与知识条目为准**（该次运行模型仍把观测 id 当作检查建议，属下文"模型内容质量"的已知问题）。

### 真实服务联调（2026-09-24，已完成）

在 Windows 上对真实 MindIE 服务执行了完整流程，**连接经由端口转发到达板端服务**，不是在板端本机执行的：

| 项 | 结果 |
|---|---|
| `llm --prompt "你好"` | 退出码 0，1.94 s 取回回答 |
| `report examples/REC-2026-0918-002.json` | 退出码 0，提示词 5431 字符，回答 210 字符，5.70 s |
| `report examples/REC-2026-0918-001.json` | 退出码 10（模型回答不合规），**未产出报告** |

**程序组装部分与工具结论逐条一致**——用 `diag --json` 的输出比对真实报告：顶层回显三项、`confirmed_states`（3 条的规则 id + 语句 + 证据）、`insufficient_data`（2 条）、`unsupported`（4 条）、`inconsistencies`（0 条）全部相同，候选原因中**没有编造的证据引用**。

**模型给出的那部分存在质量问题**，详见下文"模型内容质量"：候选原因始终为空、检查建议中出现材料里不存在的位域名、约每 8 次有 1 次回答结构不合规（此时流程如实报错、退出码 10、不产出报告，处理正确）。其中结构不合规一项已在 2026-09-28 针对提示词做过修正，见下节。

### 真实服务联调（2026-09-28，新增案例）

对 2026-09-28 新增的 8 条示例记录各执行**一次**真实 `report`（同样经端口转发到板端服务，模型 `Qwen2.5-1.5B`），**首次结果即记录，不反复重试到成功**：

| 项 | 结果 |
|---|---|
| 退出码 | 8/8 为 0，均产出报告 |
| 提示词字符数 | 2069～4392，均未触发裁剪（上限 8000） |
| 模型回答 | 91～146 字符，耗时 1.3～2.2 s |
| 回答结构合规 | 8/8 通过，未出现对象数组的 `recommended_checks` |
| 程序组装部分 | 8/8 与 `tests/fixtures/` 下的期望报告逐条一致（`fault_state`、`confirmed_states` 的语句与证据、`inconsistencies`、`insufficient_data`、`unsupported`） |
| `candidate_causes` | 8/8 为 0 条，含 `fault_state` 已为 `confirmed` 的 `002` 与 `007`，`root_cause` 落到 `not_determined` |

**模型给出的那部分仍存在质量问题**（单次样本，只作观察不作比率）：

1. **把观测 id 当作检查建议。** 4 条记录（`003`、`005`、`006`、`007`）的 `recommended_checks` 是 `OBS-1`、`OBS-2`……`007` 的 4 条建议全是观测 id。契约只要求非空字符串，这类内容不会被拦下。
2. **写出材料里不存在的名称。** `004` 一次回答建议「查看 **MLC_C** 的 LOM 状态」，而该记录的工具结论对象是 `CAN_C`，`MLC_C` 在材料中不存在。
3. **建议重述已有事实。** `001` 的建议「检测 CAN_B 总线通信自检结论 fail」等于复述 `test.result`，没有指向报告列出的缺失项（`CAN_B.CR` 的 `LOM`）。
4. **已确认状态时仍不给候选原因。** `002` 与 `007` 的 `fault_state` 为 `confirmed`，8 次调用仍全部返回空数组——与 2026-09-24 的观察一致，原因见下节对知识库成因类条目的说明。

检索侧未发现问题：`008` 没有工具状态，工具引用 0 条、关键词补充 1 条，属预期。

### 模型内容质量

#### 2026-09-24 首次联调观察到的问题

下面是首次真实联调观察到的问题。第 2、3、4 项属于模型能力与提示词约束力，不是流程缺陷；第 1、4 项在 2026-09-28 做过针对性修正。

1. **候选原因始终为空，`root_cause` 一律落到 `not_determined`。** 对 `REC-2026-0918-002` 连续多次真实调用，`candidate_causes` 无一例外是空数组。契约允许证据不足时不提候选原因，因此这不算流程错误，但报告只有状态罗列与通用建议，**诊断价值有限**。
2. **检查建议中出现材料里不存在的位域名。** 一次回答写了「检查 CAN_A 的 CRC 和 **FRLERR** 错误标志」，而 `FRLERR` 在 `knowledge/curated/` 中不存在（FlexCAN 的错误标志为 `CRCERR`、`FRMERR`、`STFERR`，计数器为 `TXECTR`、`RXECTR`）；另有若干次把 `CRCERR`、`STFERR` 这类**标志位说成"计数器"**。提示词第 1 条明令"不得引入材料以外的寄存器定义、位域含义"，模型未守住。**`candidate_causes.statement` 与 `recommended_checks` 是自由文本，契约只校验非空字符串，这类错误不会被校验拦下。**
3. **建议与已有结论重复。** 例如建议"确认 CAN_A 是否处于总线关闭状态"，而工具已确认该状态。
4. **回答结构不合规。** 约每 8 次有 1 次把 `recommended_checks` 写成对象数组（`[{"check_item": "..."}]`）而不是字符串数组。

#### 2026-09-28 的修改与复测

针对上述问题中的**提示词与校验**部分做了一轮修正，不涉及诊断规则与知识库：

- 输出要求不再给出带具体观测 id 的示例（`["OBS-1"]` 会被照抄），改为用两个空数组示例顶层结构，另用文字说明非空候选的字段要求；`recommended_checks` 明确写成字符串数组。
- `candidate_causes[].supporting` 必须非空：解析器报错，Schema 用 `minItems: 1` 拦下。这条只保证存在支持引用，**不表示该原因已被验证**。
- 取消知识正文、单条观测的 240 字符硬截断，以及按观测条数与状态条数省略必要内容的逻辑（它们会**改变**送入模型的证据，而不是只压缩篇幅）。超限时只整条移除未被工具结论引用的补充知识；必要内容本身超限直接报错。

用两份已核对一致性的示例记录（`REC-2026-0918-001`、`REC-2026-0918-002`）各复测 3 次，共 6 次真实调用：

| 观察项 | 结果 |
|---|---|
| 提示词字符数 | 001 为 2462，002 为 5756，均未触发裁剪 |
| 回答结构合规 | 6/6 通过，**没有出现对象数组的 `recommended_checks`** |
| `candidate_causes` 条数 | 6 次全为 0 |
| 材料外的名称 | 6 次中出现 1 次（`REC-001` 一次回答把知识条目 id `MPC5554-DSPI-SR-TFUF` 当成寄存器名，并写出材料里没有的 `TX_FIFO`） |

两点说明，避免把复测结果读得比实际更强：

- **候选原因为空在这两条记录上符合当前契约。** 本轮确定的口径是"只有所给知识与当前观测共同支持某个原因时才提出"。这两条记录送入的知识条目全是判据与适用边界（`FLTCONF` 编码、总线关闭期间 `TXECTR` 语义变化、读清除语义、`LOM` 对 `FLTCONF` 的伪装、`BOFFREC` 的作用），没有一条描述某个成因。把提示词第 5 条换成更宽松的"只要有可引用观测就应提出"再测 3 次，结果同样是 0 条，说明这两条记录上的空结果不是措辞造成的。**若期望这两条记录给出候选原因，缺的是知识库里的成因类条目，不是提示词。**
- **候选原因为空时，"引用是否相关""是否编造观测 id"这两项没有可核查的对象。** 6 次复测因此没有覆盖这两条验收项；结构合规与材料外名称的样本量（6 次）也不足以给出比率。

检索侧未发现问题：送入的 6 条知识条目全部与总线关闭场景相关，工具结论引用的条目都被完整送入（取消正文截断后不再有内容损失）。

## 模型服务调用（MindIE）

应用通过 HTTP 调用 RDC300I 上已部署的 MindIE 服务，使用标准库 `urllib`，**不引入第三方依赖**，因此 `wheelhouse/` 无需变更。

本模块只做一次非流式请求：发送一段文本，取回回答。不重试、不降级、不缓存，**失败时不返回任何模拟回答**。不含知识检索、诊断规则与多轮会话。

### 接口依据

请求格式取自 RDC300I 上**已验证可用**的历史脚本（`mindie_chat.py`、`mindie_load_test.py`，两者一致），不是按文档推测的：

| 项 | 值 |
|---|---|
| 方法 | `POST` |
| 地址 | `http://127.0.0.1:1025/generate` |
| 请求头 | `Content-Type: application/json`；历史脚本**未发送任何认证头** |
| 请求体 | `{"prompt": "<ChatML 文本>", "max_tokens": 1000, "stream": false, "model": "Qwen2.5-1.5B"}` |
| 响应 | `{"text": "<回答>"}`，`text` 也可能是字符串数组 |

`/generate` 是 MindIE 原生文本生成接口，接收原始文本、**不套用对话模板**，因此提示词由应用按 Qwen2.5 的 ChatML 格式构造（`src/airbornediag/llm/prompt.py`，与历史脚本逐字一致）。它不是 OpenAI 兼容接口：改接口路径指向 `/v1/chat/completions` 不会工作，两者的请求体与响应结构都不同。

历史脚本还处理了两种实测现象，本工程沿用：响应可能把输入提示一并回显；可能在回答之后继续生成下一轮对话，需按 `<|im_end|>` 截断。

### 配置

服务地址、接口路径、模型名、超时与认证信息都不写在源码里。取值优先级：**命令行参数 > 环境变量 > 工作目录下的 `.env` > 内置默认值**。默认值即上表中的历史脚本取值。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AIRBORNEDIAG_LLM_BASE_URL` | `http://127.0.0.1:1025` | 服务地址，主机与端口 |
| `AIRBORNEDIAG_LLM_GENERATE_PATH` | `/generate` | 接口路径 |
| `AIRBORNEDIAG_LLM_MODEL` | `Qwen2.5-1.5B` | 模型名，须与 MindIE 服务 `config.json` 的 `modelName` 一致 |
| `AIRBORNEDIAG_LLM_MAX_TOKENS` | `1000` | 单次最大生成 token 数 |
| `AIRBORNEDIAG_LLM_TIMEOUT` | `300` | 单次请求超时秒数 |
| `AIRBORNEDIAG_LLM_API_KEY` | 空 | 非空时发送 `Authorization: Bearer <值>`；留空不发送认证头 |

复制 [.env.example](../.env.example) 为工程根目录下的 `.env` 后修改即可。`.env` 已被 `.gitignore` 排除，真实凭据不提交。

**认证默认关闭**，与历史脚本一致；板端真实调用在未配置凭据的情况下成功，说明该部署当前不需要认证。**需要认证时的分支尚未验证**。

### 命令行验证

```powershell
.\.venv\Scripts\airbornediag.exe llm --prompt "你好"
```

回答输出到标准输出，端点、模型、耗时等诊断信息输出到标准错误，便于管道使用。省略 `--prompt` 时从标准输入读取；另有 `--system`、`--max-tokens`、`--timeout`、`--base-url` 用于临时覆盖配置。

失败时按类型返回不同退出码，便于在无图形界面的板端区分原因：

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数或配置错误（与 argparse 的用法错误一致） |
| 3 | 连接失败：服务未启动或地址不可达 |
| 4 | 请求超时 |
| 5 | HTTP 错误：服务返回非 2xx |
| 6 | 响应格式异常：非 JSON、缺少 `text`、`text` 为空数组或类型不符 |

### 输入长度限制（2026-09-24 已核对）

`max_tokens` **只限制单次生成的 token 数，不是输入长度限制**，提示词过长时没有任何一端会因此拦住。应用侧的应对是 `src/airbornediag/llm/diagnosis.py` 中的 `PROMPT_CHAR_LIMIT`（字符数）：超限时只整条移除未被工具结论引用的补充知识条目，并在提示词内写明省略了哪些内容；观测、工具结论、输入矛盾、缺失信息与工具引用的知识都不截断也不省略，裁到无可再裁仍然超限就直接报错——截断会让模型引用记录中并不存在的证据。

**服务端当前的限制**（板端 `mindie-service` 配置）：

| 项 | 值 |
|---|---|
| 输入上限 | 10240 tokens |
| 生成长度上限 | 2560 tokens |
| 总长 | 12800 |
| `truncation` | `false` |

**`truncation=false` 是必要的**：它为 `true` 时服务端会静默截断超长提示词，而模型仍可能引用被截掉的那些观测 id，正好违背"候选原因只能引用记录中已有的观测"。关掉之后超限是明确报错，应用如实上报退出码 5、不产出报告。

服务端限制起初为输入 2048 tokens，当时实测的拒绝行为是：

```
HTTP 424  {"error":"Failed to enqueue inferRequest: This model's maximum input ids
length cannot be greater than 2048,the input ids length is 3095"}
```

**`PROMPT_CHAR_LIMIT` 的取值依据**：应用侧没有分词器，只能按字符数近似，于是实测了几种代表性内容的 token 密度——

| 内容 | 字符数 | token 数 | 密度 |
|---|---|---|---|
| 真实诊断提示词 | 5431 | 3095 | 0.57 /字符 |
| 纯中文散文 | 7500 | 4800 | 0.64 /字符 |
| 报告式编号混排 | 8600 | 5400 | 0.63 /字符 |
| 纯数字标点 | 9600 | 8399 | 0.875 /字符 |

按最密的中文散文 0.64 折算，当前的 `PROMPT_CHAR_LIMIT = 8000` 约 5100 tokens，占输入上限的 50%；即使按不现实的纯数字标点 0.875 折算也只有 7000 tokens，仍在 10240 以内。表中「真实诊断提示词」一行是 2026-09-24 按当时的提示词实测的，当时知识正文按 240 字符截断。**取消正文截断后的实际用量**：`report examples/REC-2026-0918-002.json` 的提示词为 5756 字符（`examples/REC-2026-0918-001.json` 为 2462 字符），均未触发裁剪。

这是按实测密度定的经验值，不是服务端限制的等价换算：内容比实测更密时仍可能超限，此时服务端会拒绝并如实报错（退出码 5），不会产出一份看起来正常的报告。

### 验证情况

对真实服务的调用已在板端验证通过（2026-09-21，见"验证记录（RDC300I）"）：默认配置下 `airbornediag llm` 退出码 0 并取回回答，认证头未发送。

仍然未验证的部分：**认证分支**（`AIRBORNEDIAG_LLM_API_KEY` 非空）从未执行过；**非默认配置**（其他服务地址、接口路径或模型名）未验证；**Windows 直连板端服务**未验证，已验证的调用是在板端本机执行的。

Windows 本机不运行 Qwen，Windows 侧的模型调用测试全部针对 `tests/fake_mindie.py` 提供的假服务，只覆盖成功与失败的代码路径，不构成对真实服务的验证。

## RDC300I 板端部署与验证

### 已在板端确认

- 系统 Python 3.9.9 可创建独立 venv，venv 内 Python 为 3.9.9，pip 21.3.1 可运行。
- SQLite 3.37.2 的 FTS5 可用（后续知识检索需要）。
- **板端无互联网**，不能通过 pip 在线下载依赖，安装必须使用离线 wheel 包。

### 离线 wheel 包（wheelhouse/）

板端不能联网，因此在本机（可联网的 Windows）预先下载全部依赖到工程根目录的 `wheelhouse/`，随源码一起复制到板端。

`wheelhouse/` 由 `pyproject.toml` 声明的依赖解析得到，下载命令（在本机工程根目录、使用 3.9 解释器执行）：

```bash
python -m pip download -d wheelhouse --only-binary=:all: \
  --platform manylinux2014_aarch64 --python-version 3.9 --implementation cp --abi cp39 \
  "setuptools>=64" "pytest>=7.0" "jsonschema>=4.18,<5" pip wheel
```

内容（16 个 wheel，约 5.1 MB）：

| 包 | 版本 | 用途 |
|---|---|---|
| setuptools | 82.0.1 | 构建后端（`pyproject.toml` 的 `build-system.requires`） |
| pytest | 8.4.2 | 开发依赖（`.[dev]`） |
| jsonschema | 4.25.1 | 运行期依赖，执行 `schemas/` 下的契约校验 |
| attrs / jsonschema-specifications / referencing / typing_extensions | — | jsonschema 的传递依赖（纯 Python） |
| **rpds-py** | 0.27.1 | `referencing` 的传递依赖，**编译扩展**，包名 `rpds_py-0.27.1-cp39-cp39-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` |
| iniconfig / packaging / pluggy / pygments / tomli / exceptiongroup | — | pytest 的传递依赖 |
| pip | 26.0.1 | 备用：需要时在 venv 内升级 pip |
| wheel | 0.48.0 | 备用：`--no-build-isolation` 路径需要 |

**`rpds-py` 不是纯 Python 包**，这是 wheelhouse 中唯一的平台专用二进制。它按 CPython 版本与平台分别发布 wheel，因此下载参数必须同时锁定 `--python-version 3.9 --implementation cp --abi cp39` 与 `--platform manylinux2014_aarch64`；换板端 Python 版本或 CPU 架构时需要重新下载。`manylinux2014_aarch64` 要求 glibc ≥ 2.17。

`colorama` 未包含：它是 pytest 在 `sys_platform == "win32"` 下的依赖，Linux 端 pip 不会请求。

下载时的注意事项：pip 的 `--platform` 只影响 wheel 标签选择，**环境标记仍按当前解释器判定**。因此在本机（Windows）执行下载会把 `colorama` 一并拉入，需要人工剔除；`python_version` 类标记（`tomli`、`exceptiongroup`）因本机使用 3.9 与板端一致，无需处理。

### 离线安装步骤

不要把 Windows 的 `.venv` 复制到板端：其中的可执行文件与 `.pth` 含 Windows 绝对路径，必须在板端重新创建。

1. 迁移源码与 wheel 包到板端并进入工程根目录。只需复制源码和 `wheelhouse/`，不要复制 `.venv/`、`__pycache__/`、`*.egg-info/`、`.pytest_cache/`、`build/`。

2. 建立独立环境：

   ```bash
   python3 -m venv .venv
   ```

3. 离线安装工程与开发依赖：

   ```bash
   .venv/bin/python -m pip install --no-index --find-links=wheelhouse -e ".[dev]"
   ```

4. 执行与 Windows 相同的验证：

   ```bash
   .venv/bin/airbornediag --help;    echo "exit=$?"
   .venv/bin/airbornediag --version; echo "exit=$?"
   .venv/bin/python -m pytest
   .venv/bin/python scripts/validate_contracts.py; echo "exit=$?"
   ```

5. 验证知识库构建与查询（不依赖模型服务）：

   ```bash
   .venv/bin/airbornediag kb build; echo "exit=$?"
   .venv/bin/airbornediag kb query "总线关闭"; echo "exit=$?"
   .venv/bin/airbornediag kb query "TFUF 从模式" --peripheral DSPI --limit 3; echo "exit=$?"
   ```

   构建应打印写入 22 条知识条目，查询应打印命中条目及其依据。退出码含义见"知识库构建与检索"；退出码 8 且提示不支持 FTS5 表示该解释器的 SQLite 缺少 FTS5 支持。

6. 验证诊断工具（不依赖模型服务，也不依赖知识索引）：

   ```bash
   .venv/bin/airbornediag diag examples/REC-2026-0918-002.json; echo "exit=$?"
   .venv/bin/airbornediag diag examples/REC-2026-0918-001.json --json; echo "exit=$?"
   ```

   两份示例记录应退出码 0，并打印出与 Windows 侧相同的确认状态与缺失项（`--json` 的输出可直接与 Windows 侧输出对比）。退出码含义见"诊断工具（diag）"。

7. 验证真实模型服务调用（`AIRBORNEDIAG_LLM_*` 未配置时使用默认的 `http://127.0.0.1:1025/generate`）：

   ```bash
   .venv/bin/airbornediag llm --prompt "你好"; echo "exit=$?"
   ```

   退出码含义见"模型服务调用（MindIE）"。退出码 0 且打印出模型回答，说明真实调用通过；退出码 3 表示服务未启动或地址不可达。

8. 验证完整诊断流程（需要第 5 步的索引和第 7 步可用的模型服务）：

   ```bash
   .venv/bin/airbornediag report examples/REC-2026-0918-002.json; echo "exit=$?"
   .venv/bin/airbornediag report examples/REC-2026-0918-001.json --out /tmp/report-001.json; echo "exit=$?"
   ```

   应退出码 0 并打印出诊断报告。报告的对象、确认状态、缺失信息与不支持项应与第 6 步 `diag` 的输出一致（`diag` 的输出就是报告里程序组装的那部分）；候选原因与检查建议由模型给出。退出码含义见"完整诊断流程（report）"，**除 0 以外都不产出报告**。

   服务端的输入长度限制已在 2026-09-24 核对（见"输入长度限制"）。本步是**在板端本机**执行——此前的真实联调在 Windows 侧发起并经端口转发到达板端服务，板端本机的运行尚未执行。

以上步骤均在板端 venv 内进行，不修改系统 Python、MindIE 容器或 CANN 环境。

### 验证记录（RDC300I）

**已完成（引入 `jsonschema` 之前）**：

| 项 | 结果 |
|---|---|
| 环境 | RDC300I（Linux ARM64），Python 3.9.9 |
| 虚拟环境 | 沿用此前已建立的独立 venv，未重建（第 2 步的创建方式此前已单独验证） |
| 离线安装 | 第 3 步命令执行成功 |
| `airbornediag --help` / `--version` | 正常 |
| `python -m pytest -v` | 全部通过（当时为 4 项 CLI 测试，契约测试尚未建立） |

当时使用的是默认安装命令，未执行 `--upgrade pip`、`--no-build-isolation` 或普通安装等备用方案。当时已安装依赖的版本清单未收集。

**已完成（2026-09-21，引入 `jsonschema` 与模型调用之后）**：

离线安装、基础验证（帮助、版本、测试、契约校验）与真实模型服务调用均在板端执行通过：

| 项 | 结果 |
|---|---|
| 离线安装（第 3 步） | 成功，`rpds-py` 的 aarch64/cp39 wheel 在板端正常安装 |
| `python -m pytest` | 全部通过（60 项：CLI 4 + 契约 10 + 模型调用 46） |
| `python scripts/validate_contracts.py` | 退出码 0，`jsonschema` 正常导入并执行两份 Schema |
| `airbornediag llm --prompt "你好"` | 退出码 0，取回模型回答 |

真实调用时的配置情况：

- 未配置 `AIRBORNEDIAG_LLM_API_KEY`，即按默认不发送认证头，调用成功——与历史脚本一致，该部署当前不需要认证；
- 未覆盖 `AIRBORNEDIAG_LLM_MODEL`，即默认值 `Qwen2.5-1.5B` 与该服务 `config.json` 的 `modelName` 一致；
- 调用在板端本机执行，未经过 Windows 侧。

板端解释器为 3.9.9，Windows 侧为 3.9.25，两端各自独立验证。

未收集：板端已安装依赖的具体版本清单。当时知识库尚未实现，知识检索不在验证范围内。

**已完成（2026-09-22，知识库与全文检索之后）**：

同步源码后执行第 5 步与完整测试，结果与 Windows 一致：

| 项 | 结果 |
|---|---|
| `kb build`（第 5 步） | 退出码 0，写入 22 条知识条目 |
| `kb query`（第 5 步） | 退出码 0，命中条目与 Windows 侧相同，含中英混排查询与芯片/外设过滤 |
| `python -m pytest` | 全部通过（200 项：CLI 4 + 契约 10 + 模型调用 46 + 知识库 140） |

板端 SQLite 3.37.2 的 FTS5 行为与 Windows 侧一致，索引无需随源码复制，在板端重建即可。未收集：板端逐条查询的完整输出；依赖无需重装（本轮未新增依赖），未记录板端当时的依赖版本。

**已完成（2026-09-23，诊断工具之后）**：

同步源码后执行第 6 步与完整测试，结果与 Windows 一致（本轮未新增依赖，无需重装，`wheelhouse/` 未变更）：

| 项 | 结果 |
|---|---|
| `python -m pytest` | 全部通过（262 项：CLI 4 + 契约 10 + 模型调用 46 + 知识库 140 + 诊断工具 62） |
| `diag examples/REC-2026-0918-002.json`（第 6 步） | 退出码 0，与 Windows 侧一致：3 条确认状态、2 条缺失信息、4 条不支持项（`CAN_A.ECR` 的 `RXECTR`；`CAN_A.ESR` 的 `TXWRN`/`RXWRN`/`IDLE`/`TXRX`/`BOFFINT`；`CAN_A.CR` 的 `BOFFMSK`；观测种类 `can_frame`/`timeout`） |
| `diag examples/REC-2026-0918-001.json --json`（第 6 步） | 退出码 0，与 Windows 侧输出一致 |

未收集：板端输出原文。`diag` 不访问模型服务，也不依赖知识索引，因此本步与第 5 步互不影响。

### 备用方案（未验证）

- **可编辑安装失败**（板端 pip 为 21.3.1，可编辑安装依赖 PEP 660，是 pip 21.3 才引入的能力）：改为先安装构建后端，再关闭构建隔离重试。

  ```bash
  .venv/bin/python -m pip install --no-index --find-links=wheelhouse --upgrade pip setuptools wheel
  .venv/bin/python -m pip install --no-index --find-links=wheelhouse --no-build-isolation -e ".[dev]"
  ```

- **仍无法安装**：改为普通（非可编辑）安装 `.venv/bin/python -m pip install --no-index --find-links=wheelhouse ".[dev]"`。该方式在 Windows 的新建虚拟环境中已验证可用（`airbornediag --version` 退出码 0，`import airbornediag` 正常），但未在板端验证。代价是修改源码后需要重新安装。
- **输出编码**：`--help` 文本含中文。Linux 下 Python 3.9 通常会自动做 locale 强制转换；若板端 `locale` 为非 UTF-8 且中文输出异常，使用 `PYTHONIOENCODING=utf-8`。

## 未验证项

以下内容**尚未验证**，不得视为通过：

- "备用方案"中的所有分支：板端升级 pip、`--no-build-isolation` 安装、普通（非可编辑）安装；
- 板端 locale 非 UTF-8 时的中文输出处理；
- 板端已安装依赖的具体版本（未收集）；
- Schema 中的 `format: date-time`（见 [contracts.md](contracts.md) 的"已知限制"）；
- **不经端口转发、从 Windows 直接访问板端 1025 端口**：2026-09-24 的联调中，Windows 上的应用调用的是板端真实 MindIE 服务，但连接**经由端口转发**（本机 `127.0.0.1:1025` 转发到板端），板端 1025 端口对 Windows 的直接可达性仍未验证；
- **在板端本机执行 `report`**：真实联调在 Windows 侧发起（见"真实服务联调"），板端本机的完整流程尚未执行，命令见 RDC300I 部署一节的第 8 步；
- **2026-09-28 新增的 8 条示例记录与参数化测试在板端执行**：本轮只做 Windows 侧验证，板端尚未同步；第 6 步与第 8 步列出的命令仍是原有两条记录；
- `AIRBORNEDIAG_LLM_API_KEY` 非空时的认证分支，从未执行过；
- 模型服务的非默认配置：只验证了默认的服务地址、接口路径与模型名；
- 提示词**接近** `PROMPT_CHAR_LIMIT` 时的行为：真实案例的提示词最大 5756 字符（`REC-2026-0918-002`），未触发裁剪，服务端在接近上限时的表现未经验证（服务端当前限制见"输入长度限制"）；
- **候选原因在工程意义上是否正确**：只能检查引用是否真实存在、文本里是否出现材料外的名称；候选原因的物理成因是否成立需要专业人员评审，目前没有评审依据（已观察到的质量问题见"模型内容质量"）；
- **知识库是否具备成因类条目**：记录送入的知识条目都是判据与适用边界，没有描述某个成因的条目，因此当前契约下给不出候选原因（见"模型内容质量"）；要产出候选原因需要先补这类知识，本轮未补；
- 判据在真实设备记录上的表现：工具只在构造的模拟记录上验证过，没有真实控制器记录的判据验证。

已验证的范围：Python 包在 Windows 与 RDC300I 上的安装、命令行 `--help`/`--version`/`llm`/`kb`/`diag`/`report` 的输出与退出码、测试执行、契约校验脚本在两端的结果、模型调用在假服务上的成功与失败路径、知识库在两端（Windows 与 RDC300I）的构建与检索效果、**`diag` 在两端对示例记录的判据行为及在 Windows 上对构造记录的判据行为**、**`report` 在假模型服务上的完整流程与全部关键失败路径**、**MindIE 服务端的输入长度限制与超限行为**，以及**应用在 Windows 上经端口转发对板端真实 MindIE 服务跑通完整流程、并逐条核对报告遵守了工具结论**（2026-09-24 两条记录，2026-09-28 新增 8 条各一次）。

以下内容**不在验证范围内**，不能由上述结果推断为通过：

- 真实模型调用的回答质量。**接口调用成功不等于诊断结论正确**，生成报告成功也不等于诊断正确；
- 完整流程中**模型给出的那部分**是否可用：真实联调已确认程序组装的部分与工具结论逐条一致，但候选原因始终为空、检查建议里出现材料外名称与观测 id 等问题说明**这部分目前还达不到可用水平**。2026-09-28 两轮共 14 次真实调用都没有出现结构不合规，但样本太小，不能据此认为已修正；由于候选原因仍为 0 条，**没有可核查的观测引用**，"引用是否相关"这条验收项仍未覆盖；
- 知识库的覆盖完整性：本轮只整理了首批场景（FC-01、FC-02、DS-01、DS-02）所需的内容，未覆盖手册的其他章节，也未覆盖 TMS320F28335；
- 板端系统 Python、MindIE 容器与 CANN 环境的行为（本工程未修改这些环境）。
