# 示例资产：同一份事实源 → 三种文档（需求 10.10 / OS-20）

本目录与 `demo-templates/` 一起构成开源版的**可体验资产**：不引入任何真实模板、不发一次
网络请求，就能跑完整条五阶段流水线，并当场看到「同一份事实源 → 三份结构形态不同的文档」。

```
demo-templates/                 # 自制示例模板包（生成物，入库）
  demo-software-design/         #   template.docx + template.json + package/（入库产物）
  demo-operations/
examples/                       # 演示用的声明与输入
  data/openapi.json             #   事实源一：接口描述文件
  data/operations.xlsx          #   事实源二：作业步骤表（由 build_demo.py 生成，字节确定）
  sources.json                  #   两条事实源（含 xlsx 的逐行形态）
  domains.json                  #   领域模型：endpoint / parameter / response / schema /
                                #              worksheet / procedure_step
  project.json                  #   项目声明：三份文档 + 分页 + 术语 + 规则集
  profiles/*.json               #   三份文档类型档案（文件名 = profile_id）
  build_demo.py                 #   一键跑三份文档
scripts/make_demo_templates.py  #   重建示例模板包（--check 校验入库资产可复现）
```

## 一键可体验

```bash
# 1. （可选）重建示例模板：仓库里已入库，只有改生成脚本时才需要
python -m pip install -e .
python scripts/make_demo_templates.py
python scripts/make_demo_templates.py --check

# 2. 同一份事实源 → 三份文档
python examples/build_demo.py
```

产物落在 `var/examples/jobs/<job_id>/artifacts/*.docx`，每次运行的 `job_id` 由输入摘要
决定，**同输入必得同 id**。也可以只跑一份：`--only api-summary`（按 `doc_key`）。

> Web 形态的"一键可体验"见 [`deploy/README.md`](../deploy/README.md)：
> `docker compose up` 之后首次启动会把这批模板装进库（`server/seed.py`），
> 于是打开浏览器就有模板可用。**注意**：首启装载装的是模板，`examples/` 里的档案 /
> 领域模型 / 事实源要靠界面或接口另行登记——`tests/integration/test_one_click.py`
> 的第三个用例把这条路径走了一遍，可作为操作参考。

## 10.10 覆盖表（一）：示例模板须覆盖的能力项

| 能力项（10.10 原文） | demo-software-design | demo-operations | 结构判据（`tests/unit/test_demo_assets.py`） |
| --- | --- | --- | --- |
| 多级标题 + 自动编号 | ✅ | ✅ | ≥2 个标题层级，且骨架标题样式都绑定了 `w:numPr` |
| 封面 + 修订记录 + 页眉页脚 | ✅ | ✅ | 块索引里有 `cover` / `revision` 块，且页眉、页脚族齐全 |
| 多种表格原型（含合并单元格、跨行跨列） | ✅ | ✅ | 内容区 ≥2 张表，且至少一张含 `w:gridSpan` / `w:vMerge` |
| 目录域 + 页码 + 分节 | ✅ | ✅ | 目录块里能读出 `TOC \o "1-3"` 域；页脚有 `PAGE` 域；≥2 个分节 |
| 长图与宽图槽位 | ✅ | — | 图片槽位里长（高>宽）与宽（宽>高）两种都在 |
| 带编写说明的占位内容 | ✅ | ✅ | 模板包登记的占位符里有「此处填写…」 |
| 列表段落原型（项目符号） | ✅ | ✅ | 内容区能采到项目符号原型（否则注入时静默回落到正文原型） |
| 冗余模块（如"参考资料"） | — | ✅ | 内容区有一章，在某份档案的骨架里没被用到 |
| 跨模板组合（封面取 A、修订表取 B） | ❌ 缺口 | ❌ 缺口 | 见下文「已知缺口」第 1 条 |

两处"—"是**刻意少声明**，不是漏项：`covers` 是一句对外承诺，所以
`tests/unit/test_demo_assets.py` 会逐个模板核对"自称的能力在 docx 里找不找得到"，
多写一项当场失败。这张表按**资产集合**记能力归属：

- **长图 / 宽图槽位**只由软件类模板承载。图片字节在 `ingest → render` 之间还没有通路，
  槽位目前是"模板里存在、产物里不会出现"（见缺口 2），故不把它算到非软件域模板头上。
- **冗余模块**由 `demo-operations` 承载且**是真的被裁掉了**：模板里有 6 章（含「参考资料」），
  而 `procedure-sop` 的骨架不声明该章，产物因此只有 5 节——`demo-procedure-sop.docx` 里
  从「签核记录」直接收尾，没有「参考资料」。`demo-software-design` 的同一章被两份档案都用到
  （`chapter[4]` 填内容），它不是冗余模块，故不声明。

## 10.10 覆盖表（二）：文档类型须覆盖 ≥3 类且结构形态不同

| 文档类型 | `doc_key` | 模板包 | 结构形态 | 关键投影声明 |
| --- | --- | --- | --- | --- |
| 一接口一节型 | `api-sectioned` | demo-software-design@1 | 一条接口一节：`Heading2` + 参数表 + 响应表，逐条展开 | `unit=section, layout=one_section_per, for_each=endpoint` |
| 一模块一表型 | `api-summary` | **同一个模板包** | 一个模块一张汇总表，粒度换到模块 | `unit=table, group_by=module, layout=one_table_per` |
| 非软件域（作业规程：步骤 / 阈值 / 签核栏） | `procedure-sop` | demo-operations@1 | 作业步骤逐条一节 + 步骤要点列表 + 阈值表 + 签核表 | `for_each=procedure_step`（来自 xlsx 逐行形态） |

前两份文档**共用同一个模板包、读同一个来源**（`interface-surface`），差别只在档案里的投影
声明。这是"引擎不认识文档类型"这条宣称的可执行证据：换类型 = 换声明，引擎代码零改动。

## 同源多型的证据

`examples/build_demo.py` 的实际输出（末列是产物文件名，脚本里打的是绝对路径）：

```
文档             阶段状态                                                                              门禁结论                                 产物
api-sectioned  ingest:succeeded compose:succeeded render:succeeded refresh:skipped qa:succeeded  warning（FR-M4-09 FR-M7-12）               demo-api-sectioned.docx
api-summary    ingest:succeeded compose:succeeded render:succeeded refresh:skipped qa:succeeded  warning（FR-M4-09 FR-M7-12）               demo-api-summary.docx
procedure-sop  ingest:succeeded compose:succeeded render:succeeded refresh:skipped qa:succeeded  warning（FR-M4-09 FR-M5-05 FR-M7-12）  demo-procedure-sop.docx

同源证据（事实包摘要由输入文件内容算出；读同一来源的文档必须逐字相同）
  api-sectioned: 来源 interface-surface；事实包摘要 interface-surface=9e525bdaad7c
  api-summary: 来源 interface-surface；事实包摘要 interface-surface=9e525bdaad7c
  procedure-sop: 来源 operations-workbook；事实包摘要 operations-workbook=3bc10cb4561a
```

摘要由**文件内容**算出，不由脚本承诺：两份 API 文档读同一份 `data/openapi.json`、同一个
来源声明，因此事实包摘要必须逐字相同。想自己核对，看 `var/examples/jobs/<job_id>/work/facts.json`
的 `source_digests`。

三份产物的结构（`Heading1` / `Heading2` 后的编号来自样式，不是段落直设）：

```
demo-api-sectioned.docx   Heading1 概述 → 5 条要点；Heading1 接口清单 → 汇总表(6 行)
                          → 5×(Heading2 接口 + 参数表 + 响应表)；Heading2 参数说明 → 索引表(6 行)
                          → Heading1 数据表 → 表(5 行)；Heading1 参考资料 → 4 条要点
demo-api-summary.docx     Heading1 概述 → 5 条要点；Heading1 接口清单 → 3 张模块表(2/4/2 行)
                          → Heading2 参数说明 → 鉴权接口表(6 行)；Heading1 数据表 → 表(5 行)
                          → Heading1 参考资料 → 表(5 行)
demo-procedure-sop.docx   Heading1 示例分析仪适用范围 → 工作表清单(3 行)
                          → Heading1 作业步骤 → 6×(Heading2 步骤号 + 正文)
                          → Heading2 步骤要点 → 6 条要点；Heading1 阈值与判定 → 表(7 行)
                          → Heading1 签核记录 → 表(7 行)      ← 模板里的第 6 章「参考资料」被裁掉
```

## 对照说明：本系统产出 vs 手工排版

同一份事实源下，逐项对照。**"差异可接受"一列才是这张表要回答的问题**。

| 项 | 本系统产出 | 手工排版（在 Word 里照模板填） | 差异是否可接受 |
| --- | --- | --- | --- |
| 样式层 | 完全来自模板原件，一个 `w:pPr` 都不自己拼 | 靠"格式刷"和人手小心 | ✅ 无差异，且不受人操作影响 |
| 章节顺序 | 由档案 `chapter_skeleton` 决定，正文按 IR 重建 | 人按目录顺序逐节写 | ✅ 顺序可控、可复核；模板章节顺序只是素材 |
| 标题编号 | 由 `Heading1/Heading2` 样式上的 `w:numPr` 承担（段落不带 `numId`） | 常在正文里手打 1 / 1.1 | ✅ 更正确：手打编号会被 FR-M2-12 判违规（Word 里改层级时编号会错） |
| 表头行加粗/底纹 | 来自模板表样式的 `w:tblStylePr` 条件格式 | 人常直接加粗单元格 | ✅ 更正确：直设加粗会被 FR-M5-01 判"未通过样式的属性直设" |
| 表格列宽 | 逐列按模板原型的列宽归一（FR-M5-06） | 人拖列宽 | ✅ 可复现；人拖出来的宽度无法复核 |
| 表格原型 | 逐行深拷贝正文第一张表（含合并单元格的那张**只作原型展示**，随示例内容区被裁） | 复制粘贴一张表再改内容 | ⚠️ 表内合并结构不会从数据里产生；合并单元格原型目前只在模板侧存在 |
| 要点列表 | 逐条深拷贝模板的列表段原型，符号来自编号契约的 bullet 定义 | 人点项目符号按钮 | ✅ 符号形态与模板一致 |
| 模板自带的冗余章 | 注入即替换：模板 6 章 → 产物 5 节 | 人得记得手动删掉 | ✅ 更可靠：漏删会留下模板示例内容 |
| 封面 / 修订记录 / 页眉页脚 | 原样深拷贝，不重排 | 原样保留 | ✅ 无差异 |
| 占位内容 | 全部替换或清除（FR-M5-09 = 0） | 人逐处替换，容易漏掉页眉里那处 | ✅ 更可靠：页眉占位符会**每页**重复出现 |
| 术语 | 按项目术语表替换，并把旧称归一（`forbidden`） | 人靠查找替换 | ✅ 可复核；术语表在 `project.json` 里 |
| 目录页码 | 由本机 Word（COM）算好后**回读进产物**：刷新在临时副本上做，产物包本身不被 Word 重写 | Word 打开时自动刷新 | ✅ 无差异：页码是 Word 真排版算出来的；无 Office 的机器降级为 `w:updateFields`，并出 FR-M7-12 major |
| 目录层级样式 | 由模板现有条目的样式顺序**推断**（`{1: TOC1, 2: TOC2}`），报告出 FR-M4-09 minor | 人从样式下拉里选 | ⚠️ 推断结果正确但未经契约确认；档案契约里没有 `style_for_level` 字段可声明（见缺口 3） |
| 分组标题 | 「作业步骤」下紧接 `Heading2` 步骤号，报告出 FR-M5-05 minor（孤立标题） | 人同样会这么排 | ✅ 属体例固有：判据对"纯分组标题"必然命中，不是产物缺陷 |
| 图片 | 不产出；长/宽图槽位只在模板侧存在 | 人插图、拖大小 | ❌ 见缺口 2 |
| 确定性 | 同一输入 → 同一 `job_id`、同一事实包摘要、同字节产物 | 每次手工保存的 rsid 都不同 | ✅ 更可靠：可做 golden 比对与 diff |

**五分钟结论**：样式保真、表格规范化、占位清理、术语与结构合规这五项，本系统的产出比手工
排版更可控也更可复核；真正的差距只剩一条——**图片通路尚未打通**。其余差异都是"本系统更
严格"，不是"本系统做不到"。

## 已知缺口

1. **跨模板组合**（封面取 A、修订表取 B）**未实现**：`render` 不认识
   `BlockEntry.cross_template_ref`，注入只认工作副本这一个包。两份 `template.json` 的
   `gaps` 都如实记了这一项。
2. **图片通路断裂**：`FactSnapshot` 不带 assets，`render_stage` 不注入 `asset_loader`，
   故图槽位只存在于模板侧。需求 10.10 的"长图与宽图槽位（版心内等比缩放）"这一项，
   目前能证明的是**模板里有这两种槽位**，产物里不会出现图片。
3. **`style_for_level` 没有契约字段**：目录层级样式映射只存在于渲染期 `TocPolicy`，
   档案契约（`DocTypeProfile`）里无处声明，故每个产物都会带一条 FR-M4-09 minor。
   `project.json` 里没有可声明的字段——不是没写，是写不了。
4. **域刷新依赖本机 Word**：刷新走 `Word.Application`（COM），本机没有 Office 时
   降级为"只置 `w:updateFields`，页码留给使用者打开时算"，并出 FR-M7-12 major。
   刷新是在**产物的临时副本**上做的：`Word.Document.Save()` 会整包重写并把模板样式
   压成 Word 自己的 styleId 方案，直接让它写回产物就等于"本机有没有 Word"进了产物
   字节——golden 比对与"样式层零改动"都会当场失效。
5. **`FR-M5-05` 属体例固有**：`procedure-sop` 的「作业步骤」是纯分组标题，下一条就是
   `Heading2` 步骤号，中间没有正文——`check_structure` 的"孤立标题"判据必然命中。
   `api-*` 两份产物没有这条判定。
6. **Web 侧的一键体验已具备容器形态**（`deploy/`，TASK-P0-20）：首次启动自动装载
   `demo-templates/` 的两份模板（`server/seed.py`），前端由 api 进程同源托管。
   尚缺一条：界面上的「一键创建演示项目」（`demo_init`）引用的是开发期现造的
   `demo-package@1` + `demo-profile`，与本目录这批资产不是同一出身——详见
   `deploy/README.md` 第 6 节。

## 相关门禁

```bash
PYTHONPATH="src;." .venv/Scripts/python.exe scripts/make_demo_templates.py --check   # 入库资产可复现
PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/unit/test_demo_assets.py         # covers ↔ 结构
PYTHONPATH="src;." .venv/Scripts/python.exe examples/build_demo.py                    # 端到端三份产物
```

`tests/unit/test_demo_assets.py` 是这套资产的"防虚假声明"门禁：它把 `covers` 的每一行钉到
一个结构判据上，并强制每个能力项都有已登记的判据——新加一行 `covers` 而不给判据，用例直接失败。
