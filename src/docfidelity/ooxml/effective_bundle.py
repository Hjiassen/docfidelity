"""冻结包的只读消费入口，不包含原件采集。"""

from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import TemplateVersion
from docfidelity.platform.hashing import digest_payload, sha256_file


def effective_content_hash(version: TemplateVersion) -> str:
    """纯函数：内容摘要不包含版本身份或时间。"""
    payload = version.model_dump(mode="json")
    for field in ("content_hash", "version", "template_id", "name"):
        payload.pop(field)
    return digest_payload(payload)


def load_template_version(bundle_dir: Path) -> TemplateVersion:
    """加载契约并校验字节完整性，不解析 DOCX 和原型 XML。"""
    try:
        result = TemplateVersion.model_validate_json((bundle_dir / "contract.json").read_bytes())
        if result.format_version != "1.0.0":
            raise EngineError(ErrorCode.FORMAT_VERSION, "有效模板契约版本不兼容")
        required = {"effective.docx", "selection.json", "cleanup-report.json"}
        required.update(p.path for p in result.prototypes)
        required.update(p.row_path for p in result.prototypes if p.row_path is not None)
        if required != set(result.file_hashes):
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结清单缺少必需文件或含未声明文件")
        for name, expected in result.file_hashes.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise EngineError(ErrorCode.PATH_ESCAPE, "冻结清单路径越界")
            path = bundle_dir / name
            if (
                not path.resolve().is_relative_to(bundle_dir.resolve())
                or sha256_file(path) != expected
            ):
                raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结文件摘要不一致")
        if effective_content_hash(result) != result.content_hash:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结契约内容摘要不一致")
        actual = {
            p.relative_to(bundle_dir).as_posix() for p in bundle_dir.rglob("*") if p.is_file()
        }
        if actual != required | {"contract.json"}:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结目录含未声明文件")
        return result
    except (OSError, ValidationError, ValueError) as exc:
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "无法加载冻结契约") from exc
