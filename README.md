# AirborneDiag

面向民机机载系统的智能故障诊断工程，结合大语言模型、领域知识库与诊断工具，对故障记录进行分析，输出诊断依据、候选原因及排查和处理建议。

## 当前状态

项目处于初始化阶段，诊断功能尚在开发中。

已建立可安装的 Python 包与命令行入口，可执行 `airbornediag --help` 和 `airbornediag --version`。安装与最小测试已在 Windows 和 RDC300I（Linux ARM64，Python 3.9.9）的独立虚拟环境中验证通过，环境配置与板端离线安装步骤见 [docs/development.md](docs/development.md)。

首版聚焦 **MPC5554、TMS320F28335 等机载控制器设备 MCU 的片上资源及外设故障**，采用自主设计的 JSON 故障记录验证完整诊断流程，后续逐步接入实际控制器数据。

## 首版目标

* 定义统一的 JSON 故障记录与诊断报告格式。
* 基于芯片手册和检测定义构建可追溯的知识库。
* 通过固定工具完成知识查询、状态解析和规则判断。
* 接入 Qwen2.5-1.5B-Instruct，生成结构化故障分析。
* 使用模拟案例验证诊断结果，并在 RDC300I 平台上开展运行测试。

## 处理流程

故障 JSON 输入 → 数据校验 → 诊断工具与知识检索 → Qwen 分析 → 输出校验 → 诊断报告。

报告区分已确认的异常与待验证的候选原因，提供引用依据、缺失信息和后续检查建议。首版仅生成处理建议，不自动执行设备控制操作。

## 技术方案

* **开发语言**：Python 3.9
* **推理模型**：Qwen2.5-1.5B-Instruct
* **推理服务**：MindIE
* **知识检索**：RAG，首版计划采用 SQLite 全文检索
* **开发环境**：Windows
* **目标运行平台**：RDC300I

模型权重独立于代码仓库管理，应用通过模型服务接口调用推理能力。

## 文档

* [docs/architecture.md](docs/architecture.md)：模块职责、核心数据流与关键边界。
* [docs/development.md](docs/development.md)：环境配置、安装、运行、测试及板端部署步骤。

## 开发管理

通过 GitHub Issues 跟踪任务，使用 Project 看板管理进度，通过分支和 Pull Request 提交变更。
