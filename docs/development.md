# AirborneDiag 开发说明

本文记录经过实际验证的环境配置、安装、运行和测试方法。

标注为**未验证**的步骤尚未在目标环境执行，不能视为通过。

## 环境基线

| 项 | 约定 |
|---|---|
| Python | 3.9（`requires-python = ">=3.9"`） |
| 依赖管理 | PEP 621 `pyproject.toml` + setuptools 构建后端 + 标准库 `venv` + `pip` |
| 运行期依赖 | 无 |
| 开发依赖 | `pytest`（`[project.optional-dependencies] dev`） |
| 版本来源 | `src/airbornediag/__init__.py` 的 `__version__`，由 `pyproject.toml` 动态引用 |

**为什么是 3.9**：RDC300I 系统 Python 为 3.9.9，且已在该版本上验证可创建独立虚拟环境。首版采用与板端一致的 Python 次版本，避免出现"Windows 验证通过但板端不通过"的情况。

**已知代价**：Python 3.9 已停止维护，首版依赖仅 `argparse` 与包元数据，功能上不受影响；后续如需引入要求更高版本的依赖，需要先评估板端能否获得相应解释器，再统一升级，不单独升级 Windows 侧。

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
| 安装结果 | `airbornediag-0.1.0`（可编辑安装）+ `pytest-8.4.2` |
| `airbornediag --help` | 退出码 0，输出 usage 与参数说明 |
| `airbornediag --version` | 退出码 0，输出 `airbornediag 0.1.0` |
| 仓库外目录执行 | 退出码 0，确认命令来自安装而非当前目录 |
| `python -m pytest` | 4 passed |

Windows 侧解释器为 3.9.25，板端为 3.9.9，次版本一致、补丁版本不同，板端验证不可省略。

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

`tests/test_cli.py` 覆盖：帮助输出与退出码、版本输出与退出码、版本号与安装元数据一致、`console_scripts` 入口点已注册。

说明：可编辑安装会把 `src` 加入 `sys.path`（`.venv` 中的 `__editable__.*.pth`），此时 `src/airbornediag.egg-info` 与 `site-packages` 中的 `dist-info` 都会被识别为发行版，入口点可能被枚举多次。该现象不影响命令执行，测试按集合比较。构建产物已由 `.gitignore` 排除。

## RDC300I 板端部署与验证

### 已在板端确认

- 系统 Python 3.9.9 可创建独立 venv，venv 内 Python 为 3.9.9，pip 21.3.1 可运行。
- SQLite 3.37.2 的 FTS5 可用（后续知识检索需要）。

### 步骤（未验证）

不要把 Windows 的 `.venv` 复制到板端：其中的可执行文件与 `.pth` 含 Windows 绝对路径，必须在板端重新创建。

1. 迁移源代码（仅源码，不含 `.venv/`、`__pycache__/`、`*.egg-info/`、`.pytest_cache/`）：

   ```bash
   git clone <仓库地址> airbornediag   # 或 rsync/scp 源码目录
   cd airbornediag
   ```

2. 在工程根目录建立独立环境：

   ```bash
   python3 -m venv .venv
   ```

3. 安装工程与开发依赖：

   ```bash
   .venv/bin/python -m pip install -e ".[dev]"
   ```

4. 执行与 Windows 相同的验证：

   ```bash
   .venv/bin/airbornediag --help;    echo "exit=$?"
   .venv/bin/airbornediag --version; echo "exit=$?"
   .venv/bin/python -m pytest
   ```

以上步骤均在板端 venv 内进行，不修改系统 Python、MindIE 容器或 CANN 环境。

### 已知风险与备用方案（未验证）

- **pip 版本较旧**：板端 venv 内 pip 为 21.3.1，可编辑安装（PEP 660）是 pip 21.3 才引入的能力，且构建隔离需要从索引获取 `setuptools>=64`。若第 3 步失败，先在 venv 内单独升级 pip（只影响该 venv）：`.venv/bin/python -m pip install --upgrade pip setuptools wheel`，再重试。
- **可编辑安装不可用**：备用方案是改为普通安装 `.venv/bin/python -m pip install ".[dev]"`。该方式在 Windows 的新建虚拟环境中已验证可用（`airbornediag --version` 退出码 0，`import airbornediag` 正常），但未在板端验证。代价是修改源码后需要重新安装。
- **索引不可达**：若板端无法访问 PyPI 或内网镜像，则安装无法获取构建后端与 pytest。此时需要离线 wheel 方案（需确认板端可用的镜像或离线包来源），本任务未设计该方案。
- **输出编码**：`--help` 文本含中文。Linux 下 Python 3.9 通常会自动做 locale 强制转换；若板端 `locale` 为非 UTF-8 且中文输出异常，使用 `PYTHONIOENCODING=utf-8`。

## 未验证项

以下内容在本任务结束时**尚未在 RDC300I 上执行**，不得视为通过：

- 板端依赖下载与 `pip install -e ".[dev]"`；
- 板端 `airbornediag --help` 与 `--version` 的实际输出与退出码；
- 板端 `pytest` 执行结果；
- 上述"已知风险与备用方案"中的所有分支。
