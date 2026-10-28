# Task1 报告：材料契约与接入

仅修改Task1拥有文件；无.env读取、真实材料、远程抓取或模板分析。

## API

- ingest.materials.ingest_text(text: str, *, name: str, material_id: str, markdown: bool=False) -> SourceMaterial
- pipeline.materials.ingest_material(payload: bytes, *, name: str, material_id: str) -> tuple[SourceMaterial, dict[str,bytes]]
- ooxml.materials.read_docx_material(payload: bytes) -> tuple[list[MaterialBlock], list[str], dict[str,bytes]]

上传支持.txt/.md/.markdown/.docx；文本UTF-8，可BOM。digest为实际原始字节SHA256（直接文本为原UTF-8全部字节），含空白/图片。块ID依原输入顺序b0001起，locator是line:start-end或word/document.xml/body/index。图片独立bytes，安全逻辑名images/{完整SHA256}.{扩展名}与figure.image_ref一致。

Markdown用已装markdown-it-py4.2.0 CommonMark+table；声明>=4.0,<5，NOTICE补MIT。支持多层标题/段落/列表/表格/code/Unicode；嵌套列表顺序展开并pending；HTML保留文字并pending；链接不抓取。
DOCX仅新材料只读：标题/段落/列表/表格/图片；SDT、文本框、公式尽可能保留文字；未知对象、复杂表格、图形、修订/域、非正文文字都有pending。外链图片不读取并pending。图片实际解码验证由Task3服务器边界负责。

## 精确共享契约

均继承FormatModel，extra forbid，format_version=1.0.0 generic；集合Field(default_factory)。

- MaterialBlock: block_id, kind(heading|para|bullets|numbered|table|figure), text='', level=None, items=[], columns=[], rows=[], image_ref=None, locator。
- SourceMaterial: material_id,name,media_type(text|markdown|docx),digest,blocks,pending=[]。
- OutlineSection: section_id,title,level=1,source_refs=[]。
- DraftBlock: kind(para|bullets|numbered|table|figure),text='',items=[],columns=[],rows=[],image_ref=None,source_refs=[]。
- SectionDraft: section_id,blocks,edited=False,pending=[]。无来源非空正文块要求edited=True；空blocks允许用作pending章节。
- DocumentDraft: document_id,revision,name,template_id,template_version,material_ids,mode(format_only|organize),instructions='',outline=[],outline_confirmed=False,sections=[],variables={},pending=[]。
- WritingRequest: task(outline|section),name,instructions,materials,outline=[],section_id=None,capabilities=[]（6种语义kind）。section请求必须给outline内section_id。
- WritingResult: outline=[],section=None,pending=[],model=None,tokens_in=0,tokens_out=0,error=None。
- ContentGenerator(Protocol): generate(self, request: WritingRequest) -> WritingResult。
- GenerationAttempt: section_id=None,attempt=1(1..10),model=None,tokens_in=0,tokens_out=0,adopted=False,fallback_used=False,error=None。
- GenerationRun: run_id,document_id,kind(outline|content|render),state=queued(queued|running|succeeded|failed|canceled),input_digest,document_revision,document_snapshot:DocumentDraft,materials_snapshot:list[SourceMaterial],template_id,template_version,model=None,model_identity=None,model_enabled=False,prompt_version,attempts=[],event_cursor=0,result_revision=None,result_document:DocumentDraft|None=None,artifact_ids=[],section_checkpoints:list[SectionDraft]=[],error:EngineErrorInfo|None=None,created_at/started_at/finished_at:datetime|None=None。显式文档/版本/模板与snapshot一致，材料ID集合与document.material_ids一致且唯一。
- RunEvent: event_id>=1,run_id,type(run|section|artifact|log),payload:dict[str,Any]={},created_at:datetime|None=None。

contracts.materials导出别名Identifier(ASCII安全128)、SourceRef(material/block)、Digest(64小写hex)、Text(100000)、Pending(1..2000)、AssetRef(安全相对路径256)、BlockKind。引用block实际存在由Task2校验；DocumentDraft校验材料归属。

边界：输入32MiB、文本4MiB；ZIP4096成员/单项16MiB/实际总展开64MiB/压缩比1000；拒绝重复/不安全路径/符号链接/宏/加密/缺件/DTD；材料1..2000块、序列化<=2M字符；每块<=2000项/行、100列，行列一致；文档/生成请求<=4M字符。各kind拒绝无关字段避免隐藏数据。Document拒绝重复材料/章节、游离章节、跨材料引用、空大纲确认。

## 测试证据

环境：PYTHONPATH=src PYTHONUTF8=1，D:/PythonProject/docfidelity/.venv/Scripts/python.exe -X utf8。

1. RED: -m pytest tests/unit/test_materials_v2.py tests/unit/test_document_contracts_v2.py -m unit →2 collection errors，缺失docfidelity.ingest.materials和contracts.documents；初次测试转义笔误修复后再次运行得到此真实RED。
2. 增量RED: 无关字段/未知图形测试4 failed,16 passed；修复后20 passed。
3. 增量RED: 公式与无效Unicode2 failed；修复后全部22 passed。
4. GREEN同聚焦命令：22 passed in0.99s，无warning。
5. -m ruff check 六生产模块与两测试：All checks passed!
6. -m mypy --strict 六生产模块：Success:no issues found in6 source files。
7. lint-imports:16 kept,0 broken；forbidden_words0；line_limit<=800；kernel_no_web OK。

未跑全量/真实模型/HTTP/Word视觉，由Task4控制器负责。复杂对象只保留可提取文字并待确认，不宣称原布局/二进制语义保真。
