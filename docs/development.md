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
| 版本来源 | `src/airbornediag/__init__.py` 的 `__version__`，由 `pyproject.toml` 动态引用 |

**为什么是 3.9**：RDC300I 系统 Python 为 3.9.9，且已在该版本上验证可创建独立虚拟环境。首版采用与板端一致的 Python 次版本，避免出现"Windows 验证通过但板端不通过"的情况。

**为什么引入 `jsonschema`**：契约以 JSON Schema 声明，校验必须实际执行 Schema，而不是在脚本里再写一套结构规则。下界 `4.18` 是 `registry`/`resolver` API 的引入版本，跨文件 `$ref` 的本地解析依赖该 API；上界 `<5` 用于防止主版本变更改变解析行为。该依赖及其传递依赖在板端离线安装，见下文 wheelhouse 一节。

**已知代价**：Python 3.9 已停止维护。`jsonschema` 当前支持 3.9，其传递依赖 `rpds-py` 是编译扩展，不再是纯 Python 包，板端离线安装需要对应平台（Linux ARM64 / CPython 3.9）的 wheel；后续如需引入要求更高版本的依赖，需要先评估板端能否获得相应解释器，再统一升级，不单独升级 Windows 侧。

**`format` 未完全生效**：Schema 中的 `format: date-time` 需要额外的格式校验器才会执行，本工程未引入该校验器，因此日期时间只按字符串类型校验。该限制在校验脚本的输出与 [contracts.md](contracts.md) 的"已知限制"中均有说明。

**不在本任务范围内**：MindIE、CANN、PyTorch 及模型推理依赖。应用后续通过 HTTP 调用 MindIE，与推理环境分开管理。

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
| `python -m pytest` | 14 passed（`test_cli.py` 4 项 + `test_contract_examples.py` 10 项） |
| `python scripts/validate_contracts.py` | 退出码 0，并打印日期时间格式未执行的说明 |

Windows 侧解释器为 3.9.25，板端为 3.9.9，次版本一致、补丁版本不同；两端各自独立验证，板端记录见下文。

## 命令行使用

安装后可直接调用：

```powershell
airbornediag --help
airbornediag --version
```

当前仅提供使用说明与版本查询，诊断功能尚未实现；不带参数时打印帮助并返回退出码 0。

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

说明：可编辑安装会把 `src` 加入 `sys.path`（`.venv` 中的 `__editable__.*.pth`），此时 `src/airbornediag.egg-info` 与 `site-packages` 中的 `dist-info` 都会被识别为发行版，入口点可能被枚举多次。该现象不影响命令执行，测试按集合比较。构建产物已由 `.gitignore` 排除。

## 契约校验

契约的字段、类型、枚举与分支约束由 `schemas/` 下的 JSON Schema 声明，用 `jsonschema` 实际执行；Schema 表达不了的跨文档关系由 `scripts/validate_contracts.py` 补充。两层都通过才退出码 0。

```powershell
.\.venv\Scripts\python.exe scripts\validate_contracts.py
```

脚本接受 `--root 工程根目录`，默认使用脚本所在工程的根目录；跨文件 `$ref` 从本地 Schema 解析，不访问网络。Linux 下将 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

校验通过**不代表诊断结论正确**，只代表格式与引用成立。脚本会在通过时一并说明哪些约束没有执行（当前为日期时间格式），详见 [contracts.md](contracts.md) 的"已知限制"。

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

**待完成**：引入 `jsonschema` 与其传递依赖后，板端需重新执行第 3、4 步。本轮**尚未在板端验证**，重点确认 `rpds-py` 的 aarch64/cp39 wheel 能在板端安装、`jsonschema` 能导入、契约校验与 14 项测试全部通过。在完成之前，不得认为本轮变更已在板端通过。

已有验证仅覆盖安装、命令行入口与测试的可运行性。诊断功能尚未实现，MindIE 接入、知识检索与诊断结论均不在验证范围内。

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

- 引入 `jsonschema` 后整轮在板端的离线安装与校验（见"验证记录（RDC300I）"的"待完成"）；
- `rpds-py` 在 RDC300I 上的安装与导入；本文只确认了其 aarch64/cp39 wheel 可从 PyPI 取得，未在板端安装过；
- "备用方案"中的所有分支：板端升级 pip、`--no-build-isolation` 安装、普通（非可编辑）安装；
- 板端 locale 非 UTF-8 时的中文输出处理；
- 板端已安装依赖的具体版本（未收集）；
- Schema 中的 `format: date-time`（见 [contracts.md](contracts.md) 的"已知限制"）。

已验证的范围仅限于：Python 包在 Windows 与 RDC300I 上的安装、命令行 `--help`/`--version` 的输出与退出码、测试执行、契约校验脚本在 Windows 上的运行结果。

以下内容**不在验证范围内**，不能由上述结果推断为通过：

- MindIE 接入与模型调用；
- 故障 JSON、诊断流程、知识库与诊断结论的正确性；
- 板端系统 Python、MindIE 容器与 CANN 环境的行为（本工程未修改这些环境）。
