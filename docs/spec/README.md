# 对外规范（spec）

本目录存放**对外承诺**的格式规范，与各子目录下的 `schema.json` 配套。
`schema.json` 由 `scripts/export_schemas.py` 从契约模型自动生成。

| 目录 | 内容 | 稳定度 |
| --- | --- | --- |
| `template/` | 模板包契约（manifest / styles / page_setup / numbering / outline / blocks / placeholders / fingerprint） | Stable |
| `content-ir/` | 内容 IR 与内容单元 | Stable |
| `domain/` | 领域模型与领域对象 | Stable（字段） |
| `profile/` | 文档类型档案与投影 DSL | Stable（字段）/ Evolving（语义取值） |
| `fact/` | 事实包与事实快照 | Stable |
| `job/` | 作业、阶段、事件、决策留痕 | Stable |
| `qa/` | 门禁规则、判定、豁免、报告 | Evolving |
| `config/` | 项目配置 | Stable |

## schema.json 是生成物，不要手工编辑

修改契约模型后重新生成：

```bash
python scripts/export_schemas.py
```

CI 会以 `--check` 模式校验一致性，落后于模型即失败。

## 稳定度承诺的含义

分级依据见 [ADR-007](../design/adr/ADR-007-extension-point-stability-tiers.md)：

- **Stable**：可以增加可选字段；删除或改名必须走弃用流程，并在 `CHANGELOG.md`
  给出迁移指引。
- **Evolving**：结构调整可发生在次版本内，但判定语义保持稳定。
- **Beta**：接口可能调整，每次改动记入 `CHANGELOG.md`。

## 版本策略

每个格式家族独立维护版本号（`contracts/base.py` 的 `FORMAT_CURRENT`）：

| 情况 | 行为 |
| --- | --- |
| 主版本不同 | **拒绝加载**，并给出迁移指引 |
| 次版本更高 | 允许加载，未识别字段**原样保留**（前向兼容），不得丢弃 |

加载外部文件请用 `FormatModel.load()`（宽松）；内部构造走常规构造器（严格，
未知字段立即失败）。
