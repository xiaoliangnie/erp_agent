# docs 目录索引

仓库根的 `README.md` 是**全部业务口径的唯一权威**。`CLAUDE.md` / `AGENTS.md` 是协作约定。
本地文件在 `files/`：配置、模板、运行时库、生成物。

## 日常只看这些

1. 仓库根 `README.md` —— 业务口径
2. `开发.md` —— **当前执行文档**（做什么、已拍板）
3. `Agent进度.md` —— **Agent 完成度**（功能细分 + 百分比；完成/新增能力必须改）
4. `architecture/` —— 现在系统怎么工作
5. `预测.md` —— 换模型 + 下一阶段预测契约

## 目录

| 路径 | 定位 |
|---|---|
| `开发.md` | 执行清单、已拍板、验收门 |
| `Agent进度.md` | Agent 功能细分完成度；完成或新增一项都要改 |
| `architecture/数据链路.md` | 镜像库、合同、本机供应商/映射、图片 |
| `architecture/Agent.md` | 工具注册表、确认状态机、风险分级 |
| `architecture/会话与钉钉.md` | 多群路由、上下文、换单分类 |
| `预测.md` | 当前 Forecaster 接入 + 目标契约 |
| `reference/聚水潭数据接口.md` | ERP `#_jt_data` 字段清单 |
| `reference/供应链代理API.md` | 安全代理 API 调用说明（2026-08-17，14 个聚水潭只读接口） |
| `archive/` | V5、v2/v3、Word 原文、旧调查笔记；不往里加新执行项 |

## 产物在哪

- 合同、品控日报：`files/outputs/`（不进版本库）
- 模型工件：`files/data/models/`（gitignored）
- 日志与 SQLite：`files/data/`
- 供应商主数据 Excel：`files/config/供应商管理.xlsx`（gitignored，本机覆盖）
