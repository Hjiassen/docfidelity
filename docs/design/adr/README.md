# 架构决策记录（ADR）

本目录登记 `docfidelity` 的架构决策。编号与需求文档第 4 章一致（ADR-001 ~ ADR-011），
**不重编号、不删除**；被推翻的 ADR 保留原文件并标注 `Superseded by ADR-nnn`。

## 索引

| 编号 | 标题 | 上游依据 | 状态 |
| --- | --- | --- | --- |
| [ADR-001](ADR-001-orchestration-render-decoupling.md) | 编排层与渲染层解耦，内容以 Content IR 表达 | 需求 4.1 决策一、4.7 | Accepted |
| [ADR-002](ADR-002-template-parsing-upfront.md) | 模板解析前置到入库流程，生成期只查契约 | 需求 4.1 决策二、4.4 | Accepted |
| [ADR-003](ADR-003-agent-decides-only.md) | Agent 只做决策，不做 OOXML 操作 | 需求 4.1 决策三、5.3 | Accepted |
| [ADR-004](ADR-004-kernel-has-no-web-deps.md) | Web 端是引擎的前端，内核零 Web 依赖 | 需求 4.1 决策四、2.5 | Accepted |
| [ADR-005](ADR-005-contracts-as-dependency-root.md) | 契约模块为依赖之根，OOXML 只在渲染与门禁层 | 需求 4.1 决策五、4.6 | Accepted |
| [ADR-006](ADR-006-job-state-persistence-and-resume.md) | 作业与阶段状态持久化，按阶段续跑 | 需求 4.8、6.7 | Accepted |
| [ADR-007](ADR-007-extension-point-stability-tiers.md) | 五类扩展点的稳定度分级 | 需求 4.5 | Accepted |
| [ADR-008](ADR-008-open-source-full-version.md) | 开源版即完整版，不做 Open Core | 需求 2.4 | Accepted |
| [ADR-009](ADR-009-job-granularity-orchestration-plus-children.md) | 一个编排作业 + 多个子作业（失败隔离、局部重跑） | 需求 3.2 场景 G、4.7、4.8、6.7 | Accepted |
| [ADR-010](ADR-010-small-model-baseline.md) | 配置模型逐点实测与分级降级（2026-10-09 修订） | 需求 1.3、1.4、5.2 | Accepted |
| [ADR-011](ADR-011-engine-is-doc-type-agnostic.md) | 引擎与文档类型无关 | 需求 1.3、4.1 决策六、4.9 | Accepted |

## 新增 ADR

复制 `_template.md`，按 `ADR-nnn-<slug>.md` 命名，并在此表登记。
跨模块或影响契约的决策必须走 L4 流程（见 [GOVERNANCE.md](../../../GOVERNANCE.md) 第 4 节）。

## 与代码的对应

每条 ADR 在代码中的落点与自动校验方式，统一维护在设计文档 DD-1.1 的
「约束 → 实现手段 → 自动校验方式」三列映射表中。**校验列为空的 ADR 视为实现缺口。**
