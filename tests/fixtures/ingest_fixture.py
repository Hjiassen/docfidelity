"""接入层测试夹具：一个临时工作区，含五类来源与若干反面样本。

刻意都写成**真实文件**：``.xlsx`` 是真的 zip（自建 XML），图片是真的 PNG / JPEG 头，
源码是真的能被 ``ast.parse`` 的 Python。手搓一个"看起来像"的东西只会测出解析器的
自我幻想——而接入层的全部风险都来自"外面给过来的东西不按你想的长"。

反面样本与正面样本同等重要：密钥文件、越界压缩包条目、宏载荷、命名不合规的图片、
自引用的 ``$ref``。它们对应的判据在门禁里全是"报错"或者"记告警"，没有夹具就测不到。
"""

from __future__ import annotations

import json
import struct
import zipfile
import zlib
from io import BytesIO
from pathlib import Path

from docfidelity.contracts.fact import KindBinding, SourceConfig

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# -- OpenAPI ---------------------------------------------------------------

OPENAPI_DOC: dict[str, object] = {
    "openapi": "3.0.3",
    "info": {"title": "订单服务", "version": "1.0.0"},
    "security": [{"bearerAuth": []}],
    "paths": {
        "/orders": {
            "post": {
                "tags": ["order"],
                "summary": "创建订单",
                "operationId": "createOrder",
                "requestBody": {
                    "required": True,
                    "description": "订单体",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Order"}}
                    },
                },
                "responses": {"400": {"description": "入参错误"}, "200": {"description": "成功"}},
            },
        },
        "/orders/{id}": {
            "parameters": [
                {
                    "name": "id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "订单号",
                }
            ],
            "get": {
                "tags": ["order"],
                "summary": "查询订单",
                "responses": {"200": {"description": "成功"}},
            },
        },
        "/health": {"get": {"summary": "健康检查", "responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "schemas": {
            "Order": {
                "type": "object",
                "description": "订单",
                "properties": {
                    "id": {"type": "string"},
                    "parent": {"$ref": "#/components/schemas/Order"},
                },
            }
        }
    },
}

# -- 源码（ast 接入） ------------------------------------------------------

API_SOURCE = '''"""订单操作。"""

from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    """创建订输入参。"""

    order_id: str = Field(description="订单号")
    reason: str = ""
    urgent: bool = Field(default=False, description="加急开关")


class OrderView(BaseModel):
    order_id: str
    total: int = 0


def health() -> dict:
    """健康检查。"""
    return {}


@router.get("/orders/{order_id}", tags=["order"])
def read_order(order_id: str) -> dict:
    """查询订单。"""
    return {}


@router.post("/orders")
def create_order(payload: OrderCreate) -> dict:
    """创建订单。"""
    return {}


def helper(argument: str) -> str:
    """没有装饰器，不该被收成操作。"""
    return argument


@other.get("/ignored")
def ignored() -> None:
    """router 不在白名单时不收。"""
'''

# -- ORM 与迁移（orm 接入） ------------------------------------------------

DB_SOURCE = '''"""数据模型。"""

from sqlalchemy import Column, Integer, String


class OrderRecord(Base):
    """订单记录。"""

    __tablename__ = "order_record"
    __table_args__ = {"comment": "订单记录表"}

    id = Column(Integer, primary_key=True, comment="主键")
    order_no = Column(String(32), nullable=False, comment="订单号")
    note = Column(String(200))
'''

MIGRATION_SOURCE = '''"""建表迁移。"""

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(length=64), nullable=False, comment="动作"),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
'''

# -- 图片字节 --------------------------------------------------------------


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png_bytes(width: int, height: int) -> bytes:
    """最小但结构完整的 PNG（含正确 CRC），尺寸写在 IHDR 里。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return PNG_SIGNATURE + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IEND", b"")


def jpeg_bytes(width: int, height: int) -> bytes:
    """最小但能被头解析读懂的 JPEG（APP0 + SOF0）。"""
    app0 = (
        b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x03"
        + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


# -- xlsx 构造 -------------------------------------------------------------

_SHARED = [
    "编号",
    "模块",
    "变更内容",
    "负责人",
    "变更点登记表",
    "A-01",
    "订单",
    "新增取消原因",
    "张三",
]

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="变更点" sheetId="1" r:id="rId1"/>
    <sheet name="键值" sheetId="2" r:id="rId2"/>
  </sheets>
</workbook>
"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>
"""


def _shared_strings_xml() -> str:
    items = "".join(f"<si><t>{text}</t></si>" for text in _SHARED)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(_SHARED)}" uniqueCount="{len(_SHARED)}">{items}</sst>'
    )


#: 表头刻意落在第 3 行（前两行是合并过的标题区）——「偏移表头探测」的夹具。
_SHEET1 = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="2"><c r="A2" t="s"><v>4</v></c></row>
    <row r="3">
      <c r="A3" t="s"><v>0</v></c><c r="B3" t="s"><v>1</v></c>
      <c r="C3" t="s"><v>2</v></c><c r="D3" t="s"><v>3</v></c>
    </row>
    <row r="4">
      <c r="A4" t="s"><v>5</v></c><c r="B4" t="s"><v>6</v></c>
      <c r="C4" t="s"><v>7</v></c><c r="D4" t="s"><v>8</v></c>
    </row>
    <row r="5"><c r="A5"><v>2</v></c></row>
  </sheetData>
  <mergeCells count="1"><mergeCell ref="A2:D2"/></mergeCells>
</worksheet>
"""

_SHEET2 = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="inlineStr"><is><t>键</t></is></c><c r="B1" t="inlineStr"><is><t>值</t></is></c></row>
    <row r="2"><c r="A2" t="inlineStr"><is><t>版本</t></is></c><c r="B2" t="inlineStr"><is><t>1.0.0</t></is></c></row>
  </sheetData>
</worksheet>
"""


def xlsx_bytes() -> bytes:
    """两张 sheet 的工作簿：sheet1 用共享字符串 + 合并区 + 偏移表头，sheet2 用内联字符串。"""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/sharedStrings.xml", _shared_strings_xml())
        archive.writestr("xl/worksheets/sheet1.xml", _SHEET1)
        archive.writestr("xl/worksheets/sheet2.xml", _SHEET2)
    return buffer.getvalue()


# -- 工作区装配 ------------------------------------------------------------


def build_workspace(root: Path) -> Path:
    """在 ``root`` 下铺开全部来源与反面样本，返回 ``root``。"""
    (root / "specs").mkdir(parents=True, exist_ok=True)
    (root / "specs" / "openapi.json").write_text(
        json.dumps(OPENAPI_DOC, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "api.py").write_text(API_SOURCE, encoding="utf-8")
    (root / "src" / "db.py").write_text(DB_SOURCE, encoding="utf-8")
    (root / "migrations" / "versions").mkdir(parents=True, exist_ok=True)
    (root / "migrations" / "versions" / "0001_init.py").write_text(
        MIGRATION_SOURCE, encoding="utf-8"
    )

    (root / "tables").mkdir(parents=True, exist_ok=True)
    (root / "tables" / "changes.xlsx").write_bytes(xlsx_bytes())

    (root / "shots").mkdir(parents=True, exist_ok=True)
    (root / "shots" / "图 3-1 登录流程.png").write_bytes(png_bytes(800, 600))
    (root / "shots" / "图 3-2 关闭原因.jpg").write_bytes(jpeg_bytes(320, 96))
    (root / "shots" / "screenshot-final.png").write_bytes(png_bytes(64, 64))

    # 反面样本：密钥类文件（目录扫描必须跳过并告警）。
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return root


def openapi_config(
    source_id: str = "api_spec",
    *,
    root: str = "specs",
    priority: int = 0,
    options: dict[str, object] | None = None,
) -> SourceConfig:
    """OpenAPI 来源配置：``operation`` 形态绑到 ``endpoint``，``schema`` 故意不绑。"""
    return SourceConfig(
        source_id=source_id,
        source_type="openapi",
        root=root,
        priority=priority,
        options=dict(options or {}),
        bindings=[
            KindBinding(
                shape="operation",
                kind="endpoint",
                key=["module", "path"],
                field_map={"scope": "module"},
                priority=priority,
            )
        ],
    )


def ast_config(
    source_id: str = "code",
    *,
    root: str = "src",
    priority: int = 0,
    options: dict[str, object] | None = None,
) -> SourceConfig:
    """源码来源配置：``route`` 形态绑到 ``endpoint``，``model`` 形态绑到 ``parameter``。"""
    return SourceConfig(
        source_id=source_id,
        source_type="ast",
        root=root,
        priority=priority,
        options={"router_names": ["router"], **dict(options or {})},
        bindings=[
            KindBinding(
                shape="route",
                kind="endpoint",
                key=["module", "path"],
                field_map={"route": "path"},
                priority=priority,
            ),
            KindBinding(
                shape="model",
                kind="parameter",
                key=["name"],
                field_map={"model": "name"},
                priority=priority,
            ),
        ],
    )


def assets_config(
    source_id: str = "shots", *, root: str = "shots", priority: int = 0
) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        source_type="assets",
        root=root,
        priority=priority,
        bindings=[KindBinding(shape="asset", kind="figure", key=["logical_name"])],
    )


__all__ = [
    "API_SOURCE",
    "DB_SOURCE",
    "MIGRATION_SOURCE",
    "OPENAPI_DOC",
    "assets_config",
    "ast_config",
    "build_workspace",
    "jpeg_bytes",
    "openapi_config",
    "png_bytes",
    "xlsx_bytes",
]
