# 有效模板 API（M1）

此入口复用所选 Word 格式，清理旧业务，再按明确的新内容 IR 生成 DOCX。材料导入、大纲和大模型逐章撰写使用 [M2 文档与运行 API](documents.md)；新浏览器工作台属于 M3。下列直接 IR 渲染不调用模型。

在仓库根安装并启动服务：

```bash
python -m pip install -e ".[server]"
python -m uvicorn server.main:create_app --factory --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/docs`，使用 `effective-templates` 分组；原有页面仍是旧链路。用户原件与版本保存在 `DOCFIDELITY_STORAGE_ROOT/v2/`（默认 `var/v2/`），与旧模板分开。

| 步骤 | 请求 | 结果 |
| --- | --- | --- |
| 上传 DOCX | `POST /api/v2/template-imports`，multipart 字段 `file` | 草稿、节点、候选、默认选择和修订；响应头 `ETag` |
| 重开 | `GET /api/v2/template-imports/{import_id}` | 最近成功保存的选择与当前修订 |
| 保存选择 | `PUT /api/v2/template-imports/{import_id}/selection` | 完整 `TemplateSelection`；请求头带最近的 `ETag` 作为 `If-Match` |
| 冻结 | `POST /api/v2/template-imports/{import_id}/finalize`，正文 `{"name":"购物软件格式"}` | 不可变模板版本；同样要求 `If-Match` |
| 模板列表 | `GET /api/v2/templates` | 仅已提交版本，忽略冻结失败的孤立包 |
| 模板详情 | `GET /api/v2/templates/{template_id}` | 当前版本号与全部已发布版本号 |
| 固定版本 | `GET /api/v2/templates/{template_id}/versions/{version}` | 清理报告、格式绑定及变量要求 |
| 生成下载 | `POST /api/v2/templates/{template_id}/versions/{version}/render` | DOCX 文件 |

保存后使用返回的新 `ETag`；冻结后重新读取导入草稿获取新修订。缺失或过期的 `If-Match` 返回 409，不覆盖他人的选择。修改选择再冻结会创建新版本，不覆盖旧版本。

选择中的 `ranges` 使用本次原件的完整节点范围；取消封面或目录时删除对应范围。`formats` 的页面、标题、正文、编号和表格开关分别控制源格式继承，关闭采用已冻结基础格式。`variables` 将整个文字容器绑定为字段；生成时填写新值。旧图片默认排除，固定 Logo 等素材只有明确确认才保留。不支持的选中对象返回错误，排除其范围后重试。

以下为最小生成请求；将 `template_ref` 替换为冻结结果的真实 ID 和版本：

```json
{
  "content": {
    "project_id": "shopping",
    "doc_key": "design",
    "template_ref": "t_example@1",
    "units": [
      {"unit_id": "intro", "type": "para", "text": "新购物软件支持商品浏览与订单管理。"}
    ]
  },
  "variables": {},
  "assets": {}
}
```

`profile_id` 不必填写。标题层级、表格列数或图片格式必须有冻结绑定；若缺少，重新导入并选择对应源格式，或明确取消继承以使用支持的基础格式。不能依赖旧文档正文补内容。

图片 `image_ref` 使用 `assets` 中的相对逻辑名；值为纯 base64 位图数据，禁止绝对本机路径。完整 IR 字段见 OpenAPI 的 `RenderRequest` 与 [Schema](../spec/README.md)。复杂合并表格、旧版 `caption_slot` 等未支持输入明确报错。目录域保留并标记待更新，实际页码需要 Word/LibreOffice 刷新；M1 不宣称视觉排版验收完成。
