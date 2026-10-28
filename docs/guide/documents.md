# 新材料与内容生成（M2）

M2 为 API 阶段：旧 Word 只提供用户选择并冻结的格式，新材料提供业务内容。新工作台在 M3；本页不声明浏览器或 Word/LibreOffice 视觉验收完成。

## 准备

按 [有效模板指南](effective-templates.md) 上传、选择并冻结模板，保存返回的 `template_id` 与具体 `version`。V2 不需要领域模型或旧文档类型档案。

```bash
python -m pip install -e ".[server,model]"
python -m uvicorn server.main:create_app --factory --host 127.0.0.1 --port 8000
```

在 `/docs` 操作新版接口。材料与文档修订保存在存储根的 `v2/`，应与生成产物一起备份。真实材料与 `.env` 不提交到 Git。

## 内容流程

1. 提交粘贴文本，或上传 `.txt`、`.md`、`.docx`。材料不可变，返回的块定位和引用用于来源追溯。外部链接不会自动抓取，不支持的内容会列为待确认。
2. 创建文档，绑定新材料和冻结模板的具体版本。选择 `format_only` 保持材料结构，或 `organize` 允许模型整理；项目名不能代替事实材料。
3. 生成大纲后改名、增删、排序并确认。确认前检查章节和材料引用；旧模板章节不会进入撰写请求。
4. 逐章生成、审核并编辑正文。人工修改显式标记为编辑内容；来源和数字校验不能代替人工语义审核。
5. 提交渲染，查看运行、事件、待确认与产物。下载 DOCX 后检查版式，并在 Word/LibreOffice 更新目录页码。

修改须使用最近响应的 `ETag` 作为 `If-Match`。冲突时读取新修订再合并；不要盲目重试覆盖。运行固定提交时的材料、文档修订和模板版本，不能在执行期间改查最新版本。

## 模型与回退

来源引用只表示对应材料位置，数字校验只检查可明确核对的事实，不保证所有陈述的语义正确。待确认项仍须人工处理。新增无来源的内容应标记为人工编辑，不应伪造材料引用或“已验证”状态。

模型仅使用服务端的显式配置，见 [模型设置](README.md#模型与降级) 和 [环境变量样例](../../.env.example)。客户端不能覆盖端点或密钥。`format_only` 和总开关关闭均不得发起模型请求。

`organize` 只发送新材料、用户要求、确认的大纲和通用语义能力；不发送旧模板正文、旧章名、样式 ID、XML 或布局。输出存在错误引用、无来源数字、非法结构或请求失败时，回退到新材料的规则草稿并标记待确认。不会以旧业务事实填补缺项。

每次运行记录输入摘要、模型身份、用量、采用或回退原因、章节进度与产物。取消后不能发布迟到结果；运行期间文档已有新修订时，应保留运行结果供人工合并，不能覆盖当前编辑。重启后通过运行与事件接口恢复进度。

## API 对照

| 操作 | 请求 |
| --- | --- |
| 粘贴材料 | POST /api/v2/materials，JSON 包含 name、text、markdown |
| 上传与读取材料 | POST /api/v2/materials/upload，multipart 字段 file；GET /api/v2/materials/{material_id} |
| 创建与列出文档 | POST /api/v2/documents；GET /api/v2/documents |
| 读取与编辑 | GET /api/v2/documents/{document_id}；PATCH 同路径，带 If-Match |
| 历史修订 | GET /api/v2/documents/{document_id}/revisions/{revision} |
| 提交运行 | POST /api/v2/documents/{document_id}/{outline,content,render}-runs，JSON 对象 |
| 查询与回放 | GET /api/v2/runs/{run_id}；GET 同路径 /events?cursor=0 |
| 取消 | POST /api/v2/runs/{run_id}/cancel |
| 产物与下载 | GET /api/v2/runs/{run_id}/artifacts；GET 同路径 /{artifact_id} |

创建文档的最小请求如下，将示例标识替换为前两步响应中的实际值：

```json
{
  "name": "购物软件设计",
  "template_id": "t_example",
  "template_version": 1,
  "material_ids": ["m_example"],
  "mode": "organize",
  "instructions": "只依据新材料，缺失内容标记待确认",
  "variables": {}
}
```

运行提交返回 202；轮询运行状态至 succeeded、failed 或 canceled。可使用 Idempotency-Key 防止相同提交重复排队。事件 cursor 表示已收到的最后序号，只回放更大的序号。使用产物列表返回的 artifact_id 下载，不接受文件系统路径。

content-runs 可提交 {"section_id":"当前大纲中的章节 ID"}，只生成这一章并保留其他章节；提交空对象处理全部章节，人工编辑章节默认跳过。outline-runs、render-runs 不接受 section_id。重复幂等键绑定相同运行类型、输入修订和所选章节，不可用于不同请求。

运行保留提交时的模型和提示身份。部署后身份发生变化时不发起新模型请求，使用规则草稿并列出待确认；已完成章节的断点继续保留。大纲的章节来源发生改变时，相应旧正文失效，旧修订仍可查询；确认新大纲后重新生成或人工补写。

PATCH 可修改名称、模式、用户要求、大纲、确认状态、章节和变量。人工提交章节须显式 edited=true。材料与模板绑定属于文档身份；需要不同绑定时创建新文档。详细字段与错误结构以服务的 OpenAPI 和三个 Beta Schema 家族为准。
