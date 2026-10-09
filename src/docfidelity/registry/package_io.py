"""模板包读写与完整性校验（DD-7.1 ``package_io`` / ``register`` 两阶段）。

包目录布局（与 ``contracts.template.TemplatePackage`` 的文档字符串一致）::

    <package>/
    ├── contract/manifest.json
    ├── contract/styles.json
    ├── contract/page_setup.json
    ├── contract/numbering.json
    ├── contract/used_styles.json
    ├── contract/outline.json
    ├── contract/blocks.json
    ├── placeholders.json
    ├── fingerprint.json          # 可选（P0-05 产出）
    ├── overrides.json            # 可选（P05 第 4 步的人工确认留痕）
    └── blocks/                   # 块原件（图片等）

``package_hash`` 到底覆盖什么（必须说清楚，否则必然自指）
--------------------------------------------------------
DD 写的是"覆盖 ``contract/`` + ``blocks/`` + ``placeholders.json``，不含
``fingerprint.json``（避免自指）"。但 ``manifest.json`` 本身就在 ``contract/`` 里，
而它**含有** ``package_hash`` 字段——把它算进去必然自指。因此实现上还要再排除
``manifest.json``。这不是放宽要求：清单的其余字段（编号、版本、原件摘要、
创建时间）都不影响包内容摘要，把清单排除不会放过任何"内容被改过"的情况。

``overrides.json`` 同理不在 :data:`HASHED_PATHS` 里（它甚至不在
:data:`REQUIRED_PATHS` 里）。理由：包哈希回答"这份模板解析出了什么"，而该文件回答
"人就解析结果确认了什么"。算进去的话，"同一份模板 + 同一个决定"会因为确认时刻
不同而算出两个哈希，版本比对会把一次人工确认显示成一次模板变更。

不静默覆盖
----------
同 ``template_id@version`` 已存在时直接抛 ``E_VERSION_CONFLICT``，**不做**
"自动改个版本号再写"这种事：模板版本号是档案引用与历史产物追溯的键，
自动加后缀会让引用在一瞬间全部指向一个没人记得的版本。冲突交给界面选择
（重命名上传 / 显式覆盖 / 取消）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import (
    BlockIndex,
    NumberingContract,
    OutlineSkeleton,
    PageSetupContract,
    PlaceholderList,
    StyleContract,
    TemplateFingerprint,
    TemplateManifest,
    TemplateOverrides,
    TemplatePackage,
    UsedStylesContract,
)
from docfidelity.platform.fs import (
    atomic_write_bytes,
    iter_files,
    read_bytes,
    relative_posix,
    safe_join,
)
from docfidelity.platform.hashing import canonical_json, digest_entries

CONTRACT_DIR: Final = "contract"
BLOCK_DIR: Final = "blocks"

MANIFEST_PATH: Final = "contract/manifest.json"
STYLES_PATH: Final = "contract/styles.json"
PAGE_SETUP_PATH: Final = "contract/page_setup.json"
NUMBERING_PATH: Final = "contract/numbering.json"
USED_STYLES_PATH: Final = "contract/used_styles.json"
OUTLINE_PATH: Final = "contract/outline.json"
BLOCKS_PATH: Final = "contract/blocks.json"
PLACEHOLDERS_PATH: Final = "placeholders.json"
FINGERPRINT_PATH: Final = "fingerprint.json"
OVERRIDES_PATH: Final = "overrides.json"

#: 完整性校验要求的文件。``fingerprint.json`` 不在其中——它由 P0-05 产出，
#: 缺它只意味着"还没算过指纹"，不意味着包不完整。
REQUIRED_PATHS: Final[tuple[str, ...]] = (
    MANIFEST_PATH,
    STYLES_PATH,
    PAGE_SETUP_PATH,
    NUMBERING_PATH,
    USED_STYLES_PATH,
    OUTLINE_PATH,
    BLOCKS_PATH,
    PLACEHOLDERS_PATH,
)

#: 参与 ``package_hash`` 的路径。清单与指纹都被排除（理由见模块文档字符串）。
HASHED_PATHS: Final[tuple[str, ...]] = tuple(
    path for path in REQUIRED_PATHS if path != MANIFEST_PATH
)


@dataclass
class PackageWriteReport:
    """一次模板包写入的结果。"""

    target_dir: Path
    package_hash: str
    written: list[str] = field(default_factory=list)
    block_assets: int = 0


def build_contract_payloads(package: TemplatePackage) -> dict[str, Any]:
    """把聚合视图拆成"相对路径 → 可 JSON 化对象"。

    拆解口径**只有这一处**：写入按它拆，摘要按它算，读取按它合。三处各写一遍
    一定会漂移，而漂移的表现是"明明没改内容，读回来却少了一个分区"。
    """
    return {
        STYLES_PATH: [style.model_dump(mode="json") for style in package.styles],
        PAGE_SETUP_PATH: package.page_setup.model_dump(mode="json"),
        NUMBERING_PATH: package.numbering.model_dump(mode="json"),
        USED_STYLES_PATH: package.used_styles.model_dump(mode="json"),
        OUTLINE_PATH: package.outline.model_dump(mode="json"),
        BLOCKS_PATH: package.blocks.model_dump(mode="json"),
        PLACEHOLDERS_PATH: package.placeholders.model_dump(mode="json"),
    }


def compute_package_hash(
    payloads: Mapping[str, Any],
    *,
    block_assets: Mapping[str, bytes] | None = None,
) -> str:
    """包内容摘要：覆盖除清单与指纹以外的全部内容。

    ``blocks/`` 里的二进制资产按 ``blocks/<名>`` 参与，与文本部件同处一个摘要，
    因此"换了一张封面图"也一定会改变 ``package_hash``。
    """
    entries: list[tuple[str, bytes]] = [
        (path, canonical_json(payload)) for path, payload in payloads.items()
    ]
    entries.extend((f"{BLOCK_DIR}/{name}", data) for name, data in (block_assets or {}).items())
    return digest_entries(entries)


def write_package(
    package: TemplatePackage,
    target_dir: str | Path,
    *,
    block_assets: Mapping[str, bytes] | None = None,
) -> PackageWriteReport:
    """写出模板包。

    ``package_hash`` 由本函数计算并回填，调用方**不需要**（也不应该）自己算
    ——调用方算的话，它就得知道"哪些文件参与、哪些不参与"这条规则，规则一旦
    有两处实现就会漂移。

    Raises
    ------
    EngineError
        ``E_VERSION_CONFLICT``：目标目录已存在同 ``template_id@version`` 的包。
    """
    target = Path(target_dir)
    if target.is_dir() and any(target.iterdir()):
        raise _version_conflict(package, target)
    if target.exists() and not target.is_dir():
        raise EngineError(
            ErrorCode.VERSION_CONFLICT,
            f"目标路径已被同名文件占用：{target}",
            where={"target": str(target)},
            remediation=["换一个目标目录", "改名后重试"],
        )

    payloads = build_contract_payloads(package)
    assets = dict(block_assets or {})
    package_hash = compute_package_hash(payloads, block_assets=assets)

    manifest_payload = package.manifest.model_dump(mode="json")
    manifest_payload["package_hash"] = package_hash

    written: list[str] = []
    for path, payload in payloads.items():
        _write_json(target, path, payload)
        written.append(path)
    if package.fingerprint is not None:
        _write_json(target, FINGERPRINT_PATH, package.fingerprint.model_dump(mode="json"))
        written.append(FINGERPRINT_PATH)
    for name, data in sorted(assets.items()):
        atomic_write_bytes(safe_join(target, BLOCK_DIR, name), data)
        written.append(f"{BLOCK_DIR}/{name}")
    _write_json(target, MANIFEST_PATH, manifest_payload)
    written.append(MANIFEST_PATH)

    return PackageWriteReport(
        target_dir=target,
        package_hash=package_hash,
        written=sorted(written),
        block_assets=len(assets),
    )


def read_package(package_dir: str | Path) -> TemplatePackage:
    """读回模板包，并做完整性校验。

    Raises
    ------
    EngineError
        ``E_PACKAGE_INCOMPLETE``：缺文件，或内容摘要与清单对不上。
    """
    root = Path(package_dir)
    missing = [path for path in REQUIRED_PATHS if not (root / path).is_file()]
    if missing:
        raise _incomplete(root, missing, "缺少必需文件")

    manifest = TemplateManifest.load(_load_json(root, MANIFEST_PATH))
    payloads = {path: _load_json(root, path) for path in HASHED_PATHS}
    assets = read_block_assets(root)

    actual = compute_package_hash(payloads, block_assets=assets)
    if actual != manifest.package_hash:
        raise _incomplete(
            root,
            [],
            f"内容摘要与清单不一致（清单 {manifest.package_hash[:12]}…，实际 {actual[:12]}…）",
        )

    fingerprint_path = root / FINGERPRINT_PATH
    return TemplatePackage(
        manifest=manifest,
        styles=[StyleContract.load(item) for item in payloads[STYLES_PATH]],
        page_setup=PageSetupContract.load(payloads[PAGE_SETUP_PATH]),
        numbering=NumberingContract.load(payloads[NUMBERING_PATH]),
        used_styles=UsedStylesContract.load(payloads[USED_STYLES_PATH]),
        outline=OutlineSkeleton.load(payloads[OUTLINE_PATH]),
        blocks=BlockIndex.load(payloads[BLOCKS_PATH]),
        placeholders=PlaceholderList.load(payloads[PLACEHOLDERS_PATH]),
        fingerprint=(
            TemplateFingerprint.load(_load_json(root, FINGERPRINT_PATH))
            if fingerprint_path.is_file()
            else None
        ),
    )


def verify_package(package_dir: str | Path) -> list[str]:
    """完整性校验，返回问题清单（空列表表示通过）。

    与 :func:`read_package` 的分工：**登记前预检**用它拿到全部问题一次性呈现，
    而 ``read_package`` 只报第一个致命问题。使用者在界面上更需要前者。
    """
    root = Path(package_dir)
    problems: list[str] = []
    missing = [path for path in REQUIRED_PATHS if not (root / path).is_file()]
    problems.extend(f"缺少文件：{path}" for path in missing)
    if missing:
        return problems
    try:
        manifest = TemplateManifest.load(_load_json(root, MANIFEST_PATH))
    except EngineError as error:
        return [*problems, f"清单无法解析：{error.message}"]

    payloads = {path: _load_json(root, path) for path in HASHED_PATHS}
    actual = compute_package_hash(payloads, block_assets=read_block_assets(root))
    if actual != manifest.package_hash:
        problems.append("内容摘要与清单不一致，包可能被外部修改过")
    return problems


def read_overrides(root: str | Path) -> TemplateOverrides | None:
    """读包内的 ``overrides.json``；**没有就返回 ``None``**。

    "没有"与"空"严格区分：``None`` = 这份包从来没被人确认过（第 4 步未做），
    空 ``entries`` = 确认过但没有任何需要覆盖的项。合并成一种的话，P05 第 4 步
    的"待确认项为 0 时跳过本步"就与"本步被跳过了"分不出来了。
    """
    path = Path(root) / OVERRIDES_PATH
    if not path.is_file():
        return None
    return TemplateOverrides.load(_load_json(Path(root), OVERRIDES_PATH))


def write_overrides(root: str | Path, overrides: TemplateOverrides) -> str:
    """写包内的 ``overrides.json``，返回写入的相对路径。

    **不校验 ``template_ref`` 与清单是否一致**：这个文件可以在"改版本号后重新登记"
    之前写下（人确认时模板还没定版）。写一个与清单不符的引用进包是不好看，
    但为此拒绝写入会把"先确认、后定版"这条正常路径堵掉；而真正需要一致性的地方
    （生成期按引用取件）本来就会校验引用。
    """
    _write_json(Path(root), OVERRIDES_PATH, overrides.model_dump(mode="json"))
    return OVERRIDES_PATH


def read_block_assets(root: str | Path) -> dict[str, bytes]:
    """块原件：``blocks/`` 下的全部文件，键是相对 ``blocks/`` 的名字。

    公开出来是因为指纹校验也需要它：``blocks`` 分区把二进制资产算进摘要，
    而校验的入口只拿到一个目录。让调用方自己去遍历 ``blocks/`` 就是把
    "资产怎么命名、怎么算重"这件事复制一份，两份迟早不一致。
    """
    directory = Path(root) / BLOCK_DIR
    if not directory.is_dir():
        return {}
    return {relative_posix(path, directory): read_bytes(path) for path in iter_files(directory)}


def _write_json(root: Path, relative: str, payload: Any) -> None:
    """写一个包内 JSON 文件。

    必须用 ``safe_join`` 而不是 ``ensure_within``：后者把参数当**已经成形的路径**，
    传相对片段会被按当前工作目录解析，于是每次都"越界"——而这道检查本来是为了
    拦住真正的越界，误报会让它变成噪音。
    """
    atomic_write_bytes(safe_join(root, relative), canonical_json(payload))


def _load_json(root: Path, relative: str) -> Any:
    try:
        return json.loads(read_bytes(root / relative).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EngineError(
            ErrorCode.PACKAGE_INCOMPLETE,
            f"模板包文件无法解析：{relative}",
            where={"package": str(root), "path": relative},
            remediation=["重新入库模板原件以重建模板包"],
        ) from exc


def _version_conflict(package: TemplatePackage, target: Path) -> EngineError:
    return EngineError(
        ErrorCode.VERSION_CONFLICT,
        f"模板包已存在：{package.manifest.ref}",
        where={"target": str(target), "ref": package.manifest.ref},
        remediation=[
            "改版本号后重新入库",
            "在界面确认后删除旧包再写入",
            "取消本次入库",
        ],
    )


def _incomplete(root: Path, missing: Sequence[str], reason: str) -> EngineError:
    return EngineError(
        ErrorCode.PACKAGE_INCOMPLETE,
        f"模板包不完整：{reason}",
        where={"package": str(root), "missing": list(missing)},
        remediation=["重新入库模板原件以重建模板包"],
    )


__all__ = [
    "BLOCKS_PATH",
    "BLOCK_DIR",
    "CONTRACT_DIR",
    "FINGERPRINT_PATH",
    "HASHED_PATHS",
    "MANIFEST_PATH",
    "NUMBERING_PATH",
    "OUTLINE_PATH",
    "OVERRIDES_PATH",
    "PAGE_SETUP_PATH",
    "PLACEHOLDERS_PATH",
    "PackageWriteReport",
    "REQUIRED_PATHS",
    "STYLES_PATH",
    "USED_STYLES_PATH",
    "build_contract_payloads",
    "compute_package_hash",
    "read_block_assets",
    "read_overrides",
    "read_package",
    "verify_package",
    "write_overrides",
    "write_package",
]
