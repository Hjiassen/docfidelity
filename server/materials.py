"""新材料不可变存储；图片在服务器边界验证并按内容寻址。"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.materials import SourceMaterial
from docfidelity.pipeline.materials import ingest_material
from docfidelity.platform.fs import atomic_write_bytes, atomic_write_text, safe_join

from .ids import new_id
from .settings import ServerSettings
from .template_import_io import logical_path, validate_docx_zip, write_image_assets
from .template_imports import _segment
from .uploads import probe_upload, validate_upload


class Materials:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.root = settings.storage_root / "v2" / "materials"
        self.root.mkdir(parents=True, exist_ok=True)

    def accept(self, name: str, payload: bytes, *, markdown: bool | None = None) -> SourceMaterial:
        if (
            not name.strip()
            or len(name) > 512
            or any(c in name for c in ("/", chr(92), chr(0), ":"))
            or ".." in name
        ):
            raise EngineError(ErrorCode.PATH_ESCAPE, "材料名称不安全")
        if not payload or len(payload) > min(self.settings.max_upload_bytes, 32 * 1024 * 1024):
            raise EngineError(ErrorCode.UPLOAD_SIZE, "材料大小超限或为空")
        if markdown is None and Path(name).suffix.lower() == ".docx":
            members = validate_docx_zip(payload)
            validate_upload(probe_upload(name, payload, members=members), self.settings)
        parse_name = name if markdown is None else ("text.md" if markdown else "text.txt")
        material, assets = ingest_material(payload, name=parse_name, material_id=new_id("m_"))
        material = SourceMaterial.model_validate(material.model_dump() | {"name": name})
        directory = Path(tempfile.mkdtemp(prefix=".material-", dir=self.root))
        try:
            if len(assets) > 1000:
                raise EngineError(ErrorCode.UPLOAD_SIZE, "图片数量超限")
            for reference, data in assets.items():
                logical_path(reference)
                if Path(reference).stem != sha256(data).hexdigest():
                    raise EngineError(ErrorCode.SCHEMA_INVALID, "图片名称与内容摘要不一致")
            write_image_assets(
                directory / "assets",
                {k: base64.b64encode(v).decode("ascii") for k, v in assets.items()},
            )
            encoded = material.model_dump_json()
            atomic_write_bytes(directory / "source", payload)
            atomic_write_text(directory / "material.json", encoded)
            atomic_write_text(
                directory / "receipt.json",
                json.dumps({"contract_digest": sha256(encoded.encode()).hexdigest()}),
            )
            directory.rename(self.root / material.material_id)
            return material
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def get(self, material_id: str) -> SourceMaterial:
        directory = safe_join(self.root, _segment(material_id))
        try:
            encoded = (directory / "material.json").read_bytes()
            receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
            material = SourceMaterial.model_validate_json(encoded)
            if (
                material.material_id != material_id
                or sha256(encoded).hexdigest() != receipt["contract_digest"]
                or sha256((directory / "source").read_bytes()).hexdigest() != material.digest
            ):
                raise ValueError("材料摘要不一致")
            return material
        except FileNotFoundError as exc:
            raise EngineError(ErrorCode.NOT_FOUND, "材料不存在或不完整") from exc
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "材料快照损坏") from exc

    def assets(self, materials: list[SourceMaterial]) -> dict[str, bytes]:
        assets: dict[str, bytes] = {}
        for material in materials:
            if self.get(material.material_id) != material:
                raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "材料与冻结快照不一致")
            for block in material.blocks:
                if block.image_ref:
                    reference = logical_path(block.image_ref)
                    path = safe_join(self.root / material.material_id / "assets", reference)
                    try:
                        payload = path.read_bytes()
                    except OSError as exc:
                        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "材料图片不存在") from exc
                    if Path(reference).stem != sha256(payload).hexdigest():
                        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "材料图片摘要不一致")
                    assets[reference] = payload
        return assets
