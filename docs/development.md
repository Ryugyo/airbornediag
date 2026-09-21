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
| `python -m pytest` | 200 passed（`test_cli.py` 4 + `test_contract_examples.py` 10 + `test_llm_prompt.py` 2 + `test_llm_config.py` 15 + `test_llm_client.py` 17 + `test_llm_cli.py` 12 + `test_knowledge_model.py` 37 + `test_knowledge_index.py` 33 + `test_knowledge_cli.py` 15 + `test_knowledge_search.py` 55） |
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
```

当前提供使用说明、版本查询与 `llm`、`kb` 两个子命令，诊断流程尚未实现；不带参数时打印帮助并返回退出码 0。

各子命令的输出约定一致：**结果走标准输出，诊断信息（端点、索引路径、耗时等）走标准错误**，便于管道使用。

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

知识库测试分四组：

- `tests/test_knowledge_model.py`（37 项）：工程内真实知识文件可读且 id 唯一、两类外设均有条目、知识条目不引用模拟案例的预期报告编号，以及文件级/条目级格式错误、重复 id、出处未登记、条目芯片超出来源范围等负例。
- `tests/test_knowledge_index.py`（33 项）：分词与 MATCH 表达式构造（含 FTS5 语法字符不破坏查询）、构建与查询往返、中文子串匹配、大小写、跨字段取交集、过滤与条数限制、重复重建不产生重复条目、重建拾取改动，以及索引缺失/损坏/版本不符三类异常。
- `tests/test_knowledge_cli.py`（15 项）：`kb` 子命令的输出分流、过滤与条数限制的回显、无匹配返回空结果、退出码 2/7/8，以及一次经命令行入口的子进程端到端构建与查询。
- `tests/test_knowledge_search.py`（55 项）：**直接使用 `knowledge/curated/` 的真实知识**，按代表性查询验证检索效果——中英混排查询、中文子串匹配、`docs/scenarios.md` 依据核对情况表中的全部字段名均可检索到条目、芯片与外设过滤、条数限制，以及无匹配返回空结果。

前两组与 CLI 组使用 `tests/knowledge_helpers.py` 构造的最小知识文件，不依赖真实知识内容；检索效果组使用真实知识文件。两者索引都建在临时目录中。

这些测试通过测试进程内的假 HTTP 服务（`tests/fake_mindie.py`）走真实的 HTTP 调用路径，**工程代码中没有任何模拟或降级分支**：调用失败一律报错，不会返回替代回答。假服务只存在于 `tests/`，不属于产品代码。

说明：可编辑安装会把 `src` 加入 `sys.path`（`.venv` 中的 `__editable__.*.pth`），此时 `src/airbornediag.egg-info` 与 `site-packages` 中的 `dist-info` 都会被识别为发行版，入口点可能被枚举多次。该现象不影响命令执行，测试按集合比较。构建产物已由 `.gitignore` 排除。

## 契约校验

契约的字段、类型、枚举与分支约束由 `schemas/` 下的 JSON Schema 声明，用 `jsonschema` 实际执行；Schema 表达不了的跨文档关系由 `scripts/validate_contracts.py` 补充。两层都通过才退出码 0。

```powershell
.\.venv\Scripts\python.exe scripts\validate_contracts.py
```

脚本接受 `--root 工程根目录`，默认使用脚本所在工程的根目录；跨文件 `$ref` 从本地 Schema 解析，不访问网络。Linux 下将 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

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

**板端尚未执行构建与查询**，见下文"未验证项"。

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

6. 验证真实模型服务调用（`AIRBORNEDIAG_LLM_*` 未配置时使用默认的 `http://127.0.0.1:1025/generate`）：

   ```bash
   .venv/bin/airbornediag llm --prompt "你好"; echo "exit=$?"
   ```

   退出码含义见"模型服务调用（MindIE）"。退出码 0 且打印出模型回答，说明真实调用通过；退出码 3 表示服务未启动或地址不可达。

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

**待验证（知识库）**：知识库与检索是本轮新增内容，板端尚未执行。需要重新同步源码后运行 `kb build`、`kb query` 与完整的 `python -m pytest`（当前 200 项），确认板端 SQLite 的 FTS5 分词行为与 Windows 一致、且检索结果相同。结果确认后，把上面这张表与 [README.md](../README.md) 的验证范围一并更新。

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
- **Windows 直连板端模型服务**：已验证的真实调用在板端本机执行，Windows 到板端 1025 端口的连通性仍未验证（`docs/architecture.md` 早有此遗留项）；
- `AIRBORNEDIAG_LLM_API_KEY` 非空时的认证分支，从未执行过；
- 模型服务的非默认配置：只验证了默认的服务地址、接口路径与模型名；
- 模型回答的内容质量与诊断适用性；
- **板端知识库构建与查询**：`kb build`、`kb query` 与知识库部分的测试只在 Windows 执行过，板端 SQLite 的 FTS5 分词行为是否与 Windows 一致尚未确认。

已验证的范围：Python 包在 Windows 与 RDC300I 上的安装、命令行 `--help`/`--version`/`llm`/`kb` 的输出与退出码、测试执行、契约校验脚本在两端的结果、模型调用在假服务上的成功与失败路径、知识库在 Windows 上的构建与检索效果，以及**对真实 MindIE 服务的一次成功调用**。

以下内容**不在验证范围内**，不能由上述结果推断为通过：

- 真实模型调用的回答质量。**接口调用成功不等于诊断结论正确**，生成报告成功也不等于诊断正确；
- 故障 JSON、诊断流程与诊断结论的正确性；
- 知识库的覆盖完整性：本轮只整理了首批场景（FC-01、FC-02、DS-01、DS-02）所需的内容，未覆盖手册的其他章节，也未覆盖 TMS320F28335；
- 板端系统 Python、MindIE 容器与 CANN 环境的行为（本工程未修改这些环境）。
