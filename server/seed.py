"""首启装载示例模板（DD-18.1「首次启动若 ``packages/`` 为空 → 自动装载 demo-templates/」）。

为什么需要它：镜像里**不含任何真实模板**（DD-18.1 的数据边界），于是 ``docker compose up``
之后打开浏览器看到的是一个空模板库——用户第一步就卡住，而 README 承诺的是"用示例数据
跑通全流程"。装载 ``demo-templates/`` 就是把那个空状态填上。

三条纪律
--------

1. **只在库为空时动手**。判据是"模板库里一条登记都没有"，而不是"某个 id 不存在"：
   用户删掉其中一个示例之后重启，不该被硬塞回来——那时库不是空的，是"用户编辑过"。
   拿"某个 id 在不在"当判据，正是"我明明删了它又回来了"这类投诉的来源。
2. **走真实的入库流水线**（``Services.import_template`` + ``register_template``），不自己
   往模板库里拷目录。自己拷一份的后果是示例模板与用户上传的模板**走两条解析路径**，
   而"示例能跑、上传的跑不了"这种差异只会在部署现场才暴露。代价是慢一点（要真解析），
   换回来的是"示例资产本身就是一份通过入库门禁的资产"。
3. **失败不影响启动**。示例资产坏了不该让服务起不来（同 ``main.create_app`` 的启动纪律）：
   逐份隔离、只记 warning，并把结果留在 ``/health`` 上——部署者能看到"样例没装上"，
   而不是面对一个起不来的进程。

装进库的是 ``template.docx``（注册原件）而不是预生成的 ``package/``
----------------------------------------------------------------
``demo-templates/<slug>/`` 下有两样东西：``template.docx``（原件）与 ``package/``（由
``scripts/make_demo_templates.py`` 预生成的包，供 ``examples/build_demo.py`` 直接引用）。
入库要的是**原件**：库里的包必须由本次解析产出，否则"库里的包"与"原件"之间没有可核对的
关系，``package_hash`` 也就失去意义。

身份来自资产自己的声明，不从文件名推
------------------------------------
两份示例的原件都叫 ``template.docx``。拿文件名当身份会让它们撞成同一个 ``template@1.0``：
后一份覆盖前一份，库里最后只剩一条，而 ``/health`` 上那句"已装载 2 份示例模板"就成了假话。
故身份（``template_id`` / ``version``）与描述（``doc_type`` / ``notes``）一律读
``<slug>/template.json``——那是 ``scripts/make_demo_templates.py`` 生成模板包时**写下的
同一份声明**，不需要在这里另立一套。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from docfidelity.platform.fs import read_bytes, read_text

from .services import Services

__all__ = [
    "DEMO_DECLARATION_NAME",
    "DEMO_TEMPLATE_GLOB",
    "DEMO_SEED_AUTHOR",
    "SeedOutcome",
    "SeededTemplate",
    "demo_sources",
    "seed_demo_templates",
]

_LOGGER = logging.getLogger(__name__)

#: 示例模板原件的相对形态：``<slug>/template.docx``。
DEMO_TEMPLATE_GLOB: Final = "*/template.docx"

#: 原件旁边的那份声明（``scripts/make_demo_templates.py`` 生成模板包时写下）。
#: 身份与描述都从它读，见模块文档「身份来自资产自己的声明」。
DEMO_DECLARATION_NAME: Final = "template.json"

#: 首启装载在登记记录里留下的操作人。与真实用户区分开，"这个包哪来的"才有答案。
DEMO_SEED_AUTHOR: Final = "docfidelity 首启样例"


@dataclass(frozen=True)
class SeededTemplate:
    """一份装载成功的示例模板。"""

    template_id: str
    version: str
    source: str

    @property
    def ref(self) -> str:
        return f"{self.template_id}@{self.version}"


@dataclass(frozen=True)
class SeedOutcome:
    """装载结果。"""

    #: 库里已经有登记，本次**刻意没动手**（见模块文档第 1 条）。
    skipped: bool
    seeded: tuple[SeededTemplate, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def demo_sources(root: Path | str) -> list[Path]:
    """按 slug 排序列出示例模板原件。

    排序是刻意的：装载顺序决定登记的先后，而"同一批资产在两台机器上装出两个顺序"
    会让界面上的模板库排列不一致。``sorted`` 用的是路径字符串序，稳定且与语言无关。
    """
    directory = Path(root)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob(DEMO_TEMPLATE_GLOB) if path.is_file())


def seed_demo_templates(services: Services, *, root: Path | str) -> SeedOutcome:
    """把 ``root`` 下的示例模板装进模板库；库非空或没有资产时不做任何事。

    返回结果而不是抛异常：调用方是**应用启动**，它的纪律是"起得来"。装载失败要能在
    日志与 ``/health`` 上看见，但不该演变成"服务启动失败"——那会把一个可降级的
    演示缺口升级成全站不可用。
    """
    sources = demo_sources(root)
    if not sources:
        return SeedOutcome(skipped=False)
    if services.repository.list_templates():
        _LOGGER.info("模板库非空，跳过示例模块装载（避免把用户删掉的示例塞回去）")
        return SeedOutcome(skipped=True)

    seeded: list[SeededTemplate] = []
    errors: list[str] = []
    for source in sources:
        try:
            seeded.append(_seed_one(services, source))
        except Exception as exc:  # 单份失败不拖累其余，也不阻断启动
            _LOGGER.warning("示例模板装载失败：%s", source, exc_info=True)
            errors.append(f"{source.parent.name}: {type(exc).__name__}: {exc}")
    if seeded:
        _LOGGER.info(
            "已装载 %d 份示例模板：%s", len(seeded), ", ".join(item.ref for item in seeded)
        )
    return SeedOutcome(skipped=False, seeded=tuple(seeded), errors=tuple(errors))


def _seed_one(services: Services, source: Path) -> SeededTemplate:
    """按资产自己的声明解析一份原件并登记进库。"""
    declaration = _declaration_of(source)
    record = services.import_template(
        filename=source.name,
        payload=read_bytes(source),
        template_id=str(declaration["template_id"]),
        version=str(declaration["version"]),
        doc_type=_optional_text(declaration.get("doc_type")),
        notes=_optional_text(declaration.get("notes")),
        author=DEMO_SEED_AUTHOR,
    )
    if record.package_dir is None:
        # ``package_dir`` 为空即"仅登记原件"（DD-7.1 的降级路径）：原件打不开或解析不出
        # 包。此时登记进库会得到一个 ``parsed=False`` 的条目——界面上"装上了"，构建时
        # 读包却报 E_PACKAGE_INCOMPLETE。与其留一个半成品，不如当场报错、让日志说话。
        detail = "；".join(
            f"{stage.stage}: {stage.error or stage.note or '未通过'}"
            for stage in record.stages
            if not stage.ok
        )
        raise RuntimeError(f"示例模板未能解析出模板包（{detail or '原因未记录'}）")

    registered = services.register_template(
        record.import_id,
        # 首启装载是幂等的运维动作：同名同版本时"以发行版里那份为准"是唯一能自洽的处置。
        # 用 abort 会让一次"上次装到一半"的重启变成一次启动错误。
        on_conflict="overwrite",
        operator=DEMO_SEED_AUTHOR,
    )
    return SeededTemplate(
        template_id=registered.template_id,
        version=registered.version,
        source=source.name,
    )


def _declaration_of(source: Path) -> Mapping[str, Any]:
    """读 ``<slug>/template.json``；缺字段或读不出来即报错（由调用方逐份隔离）。

    只认这一份声明，不做"没有就退回 ``slug@1``"的兜底：两套身份规则一旦并存，就会
    出现"同一批资产在两台机器上装出不同 ``template_id``"，而配置里的 ``template_ref``
    是按 ``template_id@version`` 写的引用键——那意味着配置在一台机器上有效、在另一台上
    报"引用的模板包未登记"。
    """
    path = source.parent / DEMO_DECLARATION_NAME
    try:
        raw: object = json.loads(read_text(path))
    except OSError as exc:
        raise RuntimeError(f"读不到模板声明 {path.name}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path.name} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path.name} 的顶层必须是对象，实际是 {type(raw).__name__}")
    for field in ("template_id", "version"):
        if not str(raw.get(field) or "").strip():
            raise RuntimeError(f"{path.name} 缺少 {field}（身份不能从文件名推）")
    return raw


def _optional_text(value: object) -> str | None:
    """声明里可选文本字段：``None`` 原样传下去，其余转成字符串。"""
    return None if value is None else str(value)
