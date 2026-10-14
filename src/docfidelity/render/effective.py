"""V2 生成入口：仅消费有效模板包、冻结空原型和新内容。"""

from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.ir import ContentIR
from docfidelity.ooxml.doc import find_first
from docfidelity.ooxml.effective_bundle import load_template_version
from docfidelity.ooxml.package import DocxPackage

from .effective_blocks import FrozenBlocks
from .variables import fill_variables, marked_control


def render_effective(
    bundle_dir: Path, content: ContentIR, variables: Mapping[str, str], output: Path
) -> Path:
    """生成新文档；图片逻辑名只可引用 ``output.parent/assets`` 下的相对路径。

    调用方负责将新材料图片登记至该目录。拒绝绝对路径、路径穿越和符号链接越界；
    不读取原件、不采集原型，也不重加未选择的版块。产物不可写入冻结包。
    """
    if output.resolve().is_relative_to(bundle_dir.resolve()):
        raise EngineError(ErrorCode.PATH_ESCAPE, "产物不能写入冻结模板包")
    try:
        version = load_template_version(bundle_dir)
        if content.template_ref != f"{version.template_id}@{version.version}":
            raise EngineError(ErrorCode.VERSION_CONFLICT, "内容引用与冻结模板版本不一致")
        if content.duplicate_unit_ids():
            raise EngineError(ErrorCode.SCHEMA_INVALID, "内容单元标识重复")
        with DocxPackage.open(bundle_dir / "effective.docx", read_only=False) as package:
            fill_variables(package, version.variables, variables)
            bindings = version.format_bindings
            root = package.part(bindings.insertion_part).tree
            control = marked_control(root, bindings.insertion_marker)
            parent = control.getparent()
            if parent is None or find_first(control, "w:sdtContent") is None:
                raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结内容插入锚点结构非法")
            blocks = FrozenBlocks(bundle_dir, version, package, control, output.parent / "assets")
            for unit in content.walk():
                for element in blocks.build(unit):
                    control.addprevious(element)
            parent.remove(control)
            output.parent.mkdir(parents=True, exist_ok=True)
            package.save(output)
        return output
    except (OSError, ValidationError, ValueError, TypeError) as exc:
        raise EngineError(ErrorCode.RENDER_BLOCKED, "冻结模板渲染失败") from exc
