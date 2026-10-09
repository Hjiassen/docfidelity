"""档案包的**格式与搬运**（FR-M2-25 / 验收 45；DD-13.2 的 ``profiles/import`` + ``profiles/{pid}/export``）。

为什么单成一个模块
------------------
``server/profiles.py`` 管的是**一座库**（登记、列出、废弃），本模块管的是**一份包**
（打出来、解开、把随包带来的东西装进库）。两者的寿命不同：一次搬运会结束，库会一直在长。

直接动因是 NFR-09 的单文件行数上限——档案包的两条路径加上判定实现已经让那个文件过了
1100 行。但**分界线取自职责而不是行数**：真按行数切，会切出"前半段函数在 A、后半段在 B"
这种必须在两个文件间跳着读的东西。本模块只做纯粹的字节与磁盘操作，**不认识登记**——
"把包内那份档案登记进去"仍留在 ``server.profiles.ProfileMixin`` 那一头，故这里没有任何
跨 mixin 的调用。

一份包长什么样
--------------
============================  ==========================================================
``profile.json``              档案本体（**含已算好的指纹**）
``适用判据.md``                它什么时候适用（``applies`` 三条判据的可读版）
``样例输入.json``              它吃什么样的输入（按档案自身的投影声明生成）
``templates/<tid>@<ver>/…``    它依赖的版式（含 ``source/`` 原件）
``包说明.json``                这个包的格式标识与内容清单
============================  ==========================================================

确定性
------
条目按名排序、时刻钉死（唯一实现 ``platform.fs.zip_directory``），故**同一份输入
导出两次字节相同**。收件方据此核对"你发我的就是你库里的那一份"。

包是不可信输入
--------------
解包按不可信字节对待：不是 zip、条目路径越界（绝对路径 / ``..`` / 盘符）、超上限、
缺 ``profile.json``、引用了包里没有的模板——一律 ``E_PROFILE_PACKAGE_INVALID`` 并说清
是哪一条。**半个包不留**：先做完全部可判定性检查再动手，把"导到一半"压到只剩磁盘故障。
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING, Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.profile import DocTypeProfile, ProjectionSpec
from docfidelity.pipeline import (
    PROFILE_RULE_ID,
    read_overrides,
    read_package,
    register_package,
    verify_package,
)
from docfidelity.platform.fs import (
    atomic_write_bytes,
    ensure_dir,
    iter_files,
    relative_posix,
    zip_directory,
)
from docfidelity.platform.hashing import sha256_hex

from .ids import new_id
from .storage import RegisteredProfile, RegisteredTemplate
from .templates import resolve_template, same_template_content

if TYPE_CHECKING:
    from .settings import ServerSettings
    from .storage import Repository

__all__ = [
    "APPLICABILITY_FILENAME",
    "BundledTemplate",
    "PACKAGE_LIMITS",
    "PACKAGE_NOTES_FILENAME",
    "PROFILE_PACKAGE_FORMAT",
    "PreparedProfilePackage",
    "SAMPLE_INPUT_FILENAME",
    "TEMPLATE_BUNDLE_DIRNAME",
    "build_profile_package",
    "import_bundled_templates",
    "read_profile_package",
]

#: 导出件里"适用判据说明"的文件名（FR-M2-25 / 需求 2.6.1 要求导出件**必须携带**）。
APPLICABILITY_FILENAME: Final = "适用判据.md"

#: 导出件里"自制样例输入"的文件名（FR-M2-25）。见 :func:`_sample_input`。
SAMPLE_INPUT_FILENAME: Final = "样例输入.json"

#: 导出件里的**包说明**（格式标识 + 内容清单 + 复现步骤）。见 :func:`_package_notes`。
PACKAGE_NOTES_FILENAME: Final = "包说明.json"

#: 导出件里存放**随档案一起走的模板包**的目录（验收 45）。
#: 每个包落在 ``<本目录>/<template_id>@<version>/``，内部布局与
#: ``GET /templates/{tid}/package`` 的导出件**逐字节同构**——这样收件方把子目录
#: 单独取出来，就是一份可以直接走模板导入的包。
TEMPLATE_BUNDLE_DIRNAME: Final = "templates"

#: 档案包的格式标识（写进包内的 ``包说明.json``）。**带版本**：将来包内布局变化时，
#: 收件方能凭它判断"这个包我这一版读不读得懂"，而不是靠文件名或猜。
PROFILE_PACKAGE_FORMAT: Final = "docfidelity-profile-package/1"

#: 解包上限（验收 45 的对面：拒绝比崩溃好）。
#: 上限按**解压后**的总字节算，不是按 zip 文件的字节——压缩炸弹正是靠这个差值成立的。
PACKAGE_LIMITS: Final[Mapping[str, int]] = {
    "max_archive_bytes": 64 * 1024 * 1024,
    "max_total_bytes": 256 * 1024 * 1024,
    "max_members": 2000,
    "max_member_bytes": 64 * 1024 * 1024,
}


@dataclass(frozen=True)
class BundledTemplate:
    """档案包里**随包带来并被登记**的一个模板包（验收 45 的回执）。"""

    ref: str
    package_hash: str
    #: 库里是否**已经**有这个版本（``False`` = 这次新登记进来的）。
    #: 与 ``ProfileRegistration.replaced`` 同一姿态：界面据此说"这两个包里少了一个"
    #: 而不是把一次覆盖说成一次新增。
    replaced: bool


@dataclass(frozen=True)
class PreparedProfilePackage:
    """一份**已经验干净**的包：档案本体 + 它引用的那些模板包。

    ``read_profile_package`` 的产物。到这里为止，包里所有的字节都已经过闸门
    （不是 zip / 越界 / 超限 / 缺件都已被拒），剩下的只有"装进库"——而装进库要认识
    仓储与登记，那是 :mod:`server.profiles` 的事。分开的收益是"解包"这一步可以脱离
    服务端被单独测：它是唯一处理不可信字节的地方。
    """

    document: DocTypeProfile
    #: 包内那份档案**自报**的版本号（来自 ``包说明.json``；老包没有就是 ``None``）。
    #: 档案本体里没有这个字段（见 ``server.profiles`` 的模块文档），故它是包的元数据。
    version: str | None
    #: 包内 ``templates/<ref>/`` 的全部内容，按 ref 分组。
    bundled: Mapping[str, Mapping[str, bytes]]
    #: 档案 ``bindings.template_packages`` 声明要用的那些（也一定在 ``bundled`` 里）。
    wanted: tuple[str, ...]
    #: 包里带了、但这份档案**没引用**的那些。如实回报，不静默装进库。
    unused: tuple[str, ...]


def read_profile_package(payload: bytes, *, profile_filename: str) -> PreparedProfilePackage:
    """把一个不可信的 zip 字节读成 :class:`PreparedProfilePackage`。

    ``profile_filename`` 由调用方给（就是 ``server.profiles.PROFILE_FILENAME``），
    本模块不自己定一个：导出与导入两侧各写一份常量，迟早会出现"包里的叫 A、
    导入时找 B"，而那时的报错是"包内没有某文件"——听起来像包坏了。

    Raises
    ------
    EngineError
        ``E_PROFILE_PACKAGE_INVALID``：不是 zip、条目越界、超上限、缺档案本体、
        档案本体解析不了、格式标识读不懂、引用了包里没有的模板包。
        多种情形共用一个码，因为**收件方的下一步动作只有一种**——"去要一份能用的包"。
    """
    members = _read_package_members(payload)
    _reject_unknown_format(members)
    raw = members.get(profile_filename)
    if raw is None:
        raise _package_error(
            f"包内没有 {profile_filename}",
            remediation=["确认这个 zip 是 GET /profiles/{pid}/export 的导出件"],
            where={"members": sorted(members)[:20]},
        )
    try:
        document = DocTypeProfile.model_validate_json(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _package_error(
            f"包内的 {profile_filename} 无法解析：{exc}",
            remediation=["包可能在传输中被改动过；向发件方要一份新的导出件"],
            where={"filename": profile_filename},
        ) from exc

    bundled = _bundled_packages(members)
    wanted = tuple(document.bindings.template_packages)
    missing = [ref for ref in wanted if ref not in bundled]
    if missing:
        raise _package_error(
            f"包内的档案引用了包里没有的模板包：{missing}",
            remediation=[
                "向发件方要一份完整的导出件（导出端会把引用的模板包一并打进来）",
                "或先在模板库里登记这些包，再单独走 POST /profiles 登记档案本体",
            ],
            where={"missing": missing, "bundled": sorted(bundled)},
        )
    return PreparedProfilePackage(
        document=document,
        version=_package_version(members),
        bundled=bundled,
        wanted=wanted,
        unused=tuple(sorted(ref for ref in bundled if ref not in set(wanted))),
    )


def build_profile_package(
    entry: RegisteredProfile, document: DocTypeProfile, repository: Repository
) -> tuple[str, bytes]:
    """打出一份自足的档案包，返回 ``(建议文件名, zip 字节)``。

    五件东西各自回答收件方的一个问题：

    ====================================  ==========================================
    ``profile.json``                       这份档案是什么（含指纹，可复算核对）
    ``适用判据.md``                        它**什么时候适用**（需求 2.6.1 要求必须携带）
    ``样例输入.json``                      它**吃什么样的输入**（见 :func:`_sample_input`）
    ``templates/<tid>@<ver>/…``            它**依赖的版式**（含 ``source/`` 原件）
    ``包说明.json``                        格式标识与内容清单
    ====================================  ==========================================

    为什么把模板包也打进来：只发 ``profile.json`` 的包**不是**"打包交付"——档案的
    ``bindings.template_packages`` 是引用键，收件方拿着它还得回去找发件人要模板。
    验收 45 的原话是"档案**连同**模板包打包导出后，在另一环境导入即可复现"。

    引用的模板包不在库里（或已废弃、或读不回来）时**拒绝导出**，而不是打一个缺件的包
    出去：后者要到收件方那边才炸，而那时"谁该补什么"已经说不清了。

    确定性没有变：条目按名排序、时刻钉死（唯一实现 ``platform.fs.zip_directory``），
    故同一份输入导出两次字节相同——收件方据此核对"你发我的就是你库里的那一份"。
    """
    extra = _bundle_payloads(repository, document)
    extra[APPLICABILITY_FILENAME] = _applicability(document).encode("utf-8")
    extra[SAMPLE_INPUT_FILENAME] = _sample_input(document).encode("utf-8")
    contents = sorted(
        {
            *(relative_posix(path, entry.profile_dir) for path in iter_files(entry.profile_dir)),
            *extra,
        }
    )
    extra[PACKAGE_NOTES_FILENAME] = _package_notes(entry, document, contents).encode("utf-8")
    archive = zip_directory(Path(entry.profile_dir), extra=extra)
    return f"{entry.profile_id}@{entry.version}.zip", archive


def import_bundled_templates(
    prepared: PreparedProfilePackage,
    *,
    repository: Repository,
    settings: ServerSettings,
    now: Callable[[], datetime],
    on_conflict: str,
    author: str | None,
) -> list[BundledTemplate]:
    """把包内**被这份档案引用**的模板包登记进库（未引用的一个都不动）。

    未引用的包不是"顺便装上"的：模板库里的每一条都是配置与历史产物的引用键，
    一次档案导入不该往库里塞这份档案根本用不上的东西。多出来的那几条由
    :attr:`PreparedProfilePackage.unused` 如实回报。

    冲突判据与模板导入流水线**共用** ``same_template_content``：两条导入路径对
    "这次算不算冲突"必须给同一个答案，否则其中之一必然与界面告诉用户的话相反。

    参数是**显式传进来的**，不从某个 ``self`` 上取：本模块不认识 ``Services``，
    故它没有"顺手拿宿主字段"这条捷径可走——这正是切分带来的另一半收益。
    """
    staged_root = ensure_dir(settings.storage_root / f".pkg-{new_id('x_')}")
    try:
        receipts: list[BundledTemplate] = []
        for ref in prepared.wanted:
            template_id, _, version = ref.partition("@")
            staged = _materialise(staged_root / f"pkg_{new_id('t_')}", prepared.bundled[ref])
            staged_source = _single_source(staged, ref=ref)
            package_hash = _manifest_field(staged, "package_hash")
            source_hash = _manifest_field(staged, "source_hash") or sha256_hex(
                staged_source.read_bytes()
            )
            existing = repository.find_template(template_id, version)
            if existing is not None:
                same = same_template_content(
                    existing, package_hash=package_hash, source_hash=source_hash
                )
                if not same and on_conflict != "overwrite":
                    raise EngineError(
                        ErrorCode.VERSION_CONFLICT,
                        f"模板包已登记过且内容不同：{existing.ref}",
                        where={
                            "template_ref": existing.ref,
                            "existing_package_hash": existing.package_hash,
                            "incoming_package_hash": package_hash or "",
                        },
                        remediation=[
                            "换一个档案版本号，或换一份包含正确模板版本的档案包",
                            "或确认覆盖：既有产物可能引用了库里那一版",
                        ],
                    )
            library_dir = ensure_dir(settings.templates_root / template_id / version)
            # 与 ``register_template`` 同一处置：``write_package`` 拒写非空目录，
            # 而"内容相同也重写"是这个动作的幂等语义（包里的 overrides.json 可能刚
            # 更新过）。清空只发生在这里，库目录由服务端独占。
            rmtree(library_dir, ignore_errors=True)
            ensure_dir(library_dir)
            written = register_package(
                staged,
                library_dir,
                template_id=template_id,
                version=version,
                overrides=read_overrides(staged),
            )
            problems = verify_package(written.package_dir)
            if problems:
                raise _package_error(
                    f"随包带来的模板包写进库后复核不过：{ref}",
                    remediation=["包可能在传输中被改动过；向发件方要一份新的导出件"],
                    where={"template_ref": ref, "problems": problems},
                )
            target = ensure_dir(library_dir / "source") / staged_source.name
            atomic_write_bytes(target, staged_source.read_bytes())
            repository.add_template(
                RegisteredTemplate(
                    template_id=template_id,
                    version=version,
                    package_hash=written.package_hash,
                    source_hash=source_hash,
                    package_dir=str(library_dir),
                    registered_at=now(),
                    doc_type=_manifest_field(staged, "doc_type"),
                    source_filename=target.name,
                    notes=_manifest_field(staged, "notes"),
                    registered_by=author,
                )
            )
            receipts.append(
                BundledTemplate(
                    ref=ref, package_hash=written.package_hash, replaced=existing is not None
                )
            )
        return receipts
    finally:
        rmtree(staged_root, ignore_errors=True)


def _package_error(
    message: str, *, remediation: Sequence[str], where: Mapping[str, object]
) -> EngineError:
    """档案包这一类失败的统一出口：码只有 ``E_PROFILE_PACKAGE_INVALID`` 一个。

    统一而不是按情形分码，是因为**收件方的下一步动作只有一种**——"去要一份能用的包"。
    分成五个码只会让界面写五句意思一样的话。
    """
    return EngineError(
        ErrorCode.PROFILE_PACKAGE_INVALID, message, where=dict(where), remediation=list(remediation)
    )


def _read_package_members(payload: bytes) -> dict[str, bytes]:
    """把不可信的 zip 字节读成 ``{相对路径: 内容}``，逐条守住解包的四道闸门。

    ======================  ====================================================
    不是 zip                 ``BadZipFile`` → 拒，不抛底层异常
    条目数 / 单条 / 总量     三个上限（:data:`PACKAGE_LIMITS`），按**解压后**的字节算
    路径越界                 绝对路径、``..``、盘符前缀一律拒（zip-slip）
    目录项                   只取文件，目录项本身不产生条目
    ======================  ====================================================

    上限按**解压后**算这一点是关键的：按 zip 文件大小判，一个 1 MB 的包可以解出几十 GB。
    ``zipfile`` 读条目时长度不可信（头里的 ``file_size`` 可以撒谎），故这里**边读边累加**
    实际拿到的字节，超限立即停手——而不是先信任头再回头检查。
    """
    limits = PACKAGE_LIMITS
    if len(payload) > limits["max_archive_bytes"]:
        raise _package_error(
            f"档案包超过上限：{len(payload)} 字节",
            remediation=["确认上传的是档案包（zip），而不是别的大文件"],
            where={"bytes": len(payload), "limit": limits["max_archive_bytes"]},
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise _package_error(
            f"不是有效的 zip：{exc}",
            remediation=["确认这个文件是 GET /profiles/{pid}/export 的导出件"],
            where={"bytes": len(payload)},
        ) from exc

    with archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) > limits["max_members"]:
            raise _package_error(
                f"档案包条目过多：{len(infos)}",
                remediation=["确认这个 zip 是档案包；条目上限见 PACKAGE_LIMITS"],
                where={"members": len(infos), "limit": limits["max_members"]},
            )
        members: dict[str, bytes] = {}
        total = 0
        for info in infos:
            name = _safe_member_name(info.filename)
            if name in members:
                raise _package_error(
                    f"档案包内有重复条目：{name}",
                    remediation=["同一个路径出现两次时无法判断哪一份才是原件"],
                    where={"member": name},
                )
            data = archive.read(info)
            if len(data) > limits["max_member_bytes"]:
                raise _package_error(
                    f"档案包内单个条目超过上限：{name}",
                    remediation=["确认这个 zip 是档案包；单条上限见 PACKAGE_LIMITS"],
                    where={"member": name, "bytes": len(data)},
                )
            total += len(data)
            if total > limits["max_total_bytes"]:
                raise _package_error(
                    f"档案包解压后超过上限：{total} 字节",
                    remediation=["确认这个 zip 是档案包；总量上限见 PACKAGE_LIMITS"],
                    where={"bytes": total, "limit": limits["max_total_bytes"]},
                )
            members[name] = data
        return members


def _safe_member_name(filename: str) -> str:
    """把 zip 条目名归一成安全的内包相对路径（POSIX 分隔符），越界即拒。

    判据用**归一后的分段**，不用字符串前缀：``templates/../../x`` 与
    ``/etc/passwd`` 都要拦，而 ``./profile.json`` 这种无害写法要放行。

    "绝对路径"这一条单独判，**不能靠分段判**：``/etc/passwd`` 按 ``/`` 切开得到的
    分段是 ``["etc", "passwd"]``，一个可疑的段都没有——只看分段的话它会被当成一份
    叫 ``etc/passwd`` 的内包文件，解压时写到哪里则取决于平台对开头的 ``/`` 怎么解释。
    """
    name = filename.replace("\\", "/")
    if name.startswith("/"):
        raise _package_error(
            f"档案包内有绝对路径条目：{filename}",
            remediation=["包可能被篡改或由非本系统生成；不要从不受信任的来源导入档案包"],
            where={"member": filename},
        )
    parts = [segment for segment in name.split("/") if segment not in ("", ".")]
    if not parts or any(segment == ".." for segment in parts):
        raise _package_error(
            f"档案包内有越界条目：{filename}",
            remediation=["包可能被篡改或由非本系统生成；不要从不受信任的来源导入档案包"],
            where={"member": filename},
        )
    if ":" in parts[0]:
        raise _package_error(
            f"档案包内有带盘符的条目：{filename}",
            remediation=["包可能被篡改或由非本系统生成；不要从不受信任的来源导入档案包"],
            where={"member": filename},
        )
    return "/".join(parts)


def _bundled_packages(members: Mapping[str, bytes]) -> dict[str, dict[str, bytes]]:
    """把 ``members`` 里 ``templates/<tid>@<ver>/…`` 的部分聚成"每个包一份文件表"。"""
    prefix = f"{TEMPLATE_BUNDLE_DIRNAME}/"
    bundled: dict[str, dict[str, bytes]] = {}
    for name, data in members.items():
        if not name.startswith(prefix):
            continue
        ref, _, inner = name[len(prefix) :].partition("/")
        if not inner:
            # 只有 ``templates/<ref>/`` 这样一条目录项、里面什么都没有：这不是一个包。
            # 静默当成空包会让后面的"引用的模板在不在包里"判成"在"，然后登记出一个
            # 没有契约的模板——失败点被推到构建期。
            raise _package_error(
                f"包内的模板包目录是空的：{ref}",
                remediation=["向发件方要一份完整的导出件"],
                where={"template_ref": ref},
            )
        bundled.setdefault(ref, {})[inner] = data
    return bundled


def _materialise(root: Path, files: Mapping[str, bytes]) -> Path:
    """把一份内包文件表写到 ``root`` 下的磁盘目录，返回该目录。

    路径仍逐条过一遍 :func:`_safe_member_name` 的同类判定（内包相对路径已经在读取时
    归一过，这里只是让"写盘"这一步也自带越界检查，而不是依赖调用顺序）。
    """
    for inner, data in files.items():
        atomic_write_bytes(root / _inner_path(inner), data)
    return root


def _inner_path(inner: str) -> Path:
    parts = [segment for segment in inner.replace("\\", "/").split("/") if segment]
    if not parts or any(segment == ".." for segment in parts):
        raise _package_error(
            f"包内路径越界：{inner}",
            remediation=["包可能被篡改或由非本系统生成"],
            where={"member": inner},
        )
    return Path(*parts)


def _package_version(members: Mapping[str, bytes]) -> str | None:
    """包内**自报**的档案版本号（``包说明.json`` 的 ``profile_version``）。

    档案本体里没有版本号字段（见模块文档"档案没有文档内版本号可解析"），故版本是包的
    元数据、不是档案的内容——这正是 ``包说明.json`` 存在的理由之一。读不到就返回
    ``None``，由调用方回落到 :data:`DEFAULT_PROFILE_VERSION`（与 ``POST /profiles``
    同口径：不给版本号就是 ``1.0``）。
    """
    notes = _read_notes(members)
    if notes is None:
        return None
    value = notes.get("profile_version")
    return value if isinstance(value, str) and value.strip() else None


def _read_notes(members: Mapping[str, bytes]) -> dict[str, object] | None:
    """读 ``包说明.json``；缺失或读不动都返回 ``None``（老包没有它，这不算错）。"""
    raw = members.get(PACKAGE_NOTES_FILENAME)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _reject_unknown_format(members: Mapping[str, bytes]) -> None:
    """包说明里写了格式标识而**本端读不懂**时当场拒。

    只在标识存在时判：老包没有 ``包说明.json``（或没有 ``format`` 字段）仍然可导入——
    把"没写"当成"不认识"会让上一版导出的包在新服务端上全部失效。
    """
    notes = _read_notes(members)
    if notes is None:
        return
    declared = notes.get("format")
    if declared is None or declared == PROFILE_PACKAGE_FORMAT:
        return
    raise _package_error(
        f"档案包的格式本端不认识：{declared!r}",
        remediation=[
            f"本端支持的格式是 {PROFILE_PACKAGE_FORMAT}",
            "用与发件方相同的版本导出/导入，或升级本端服务",
        ],
        where={"declared": declared, "supported": PROFILE_PACKAGE_FORMAT},
    )


def _single_source(staged: Path, *, ref: str) -> Path:
    """内包里的**那一份**原件（``source/<filename>``）；零份或多份都拒。

    路径口径与 ``server.templates._copy_source`` 严格对齐——渲染的第一步就是打开它
    （``builder.template_source_path``），两边各拼一次就会出现"登记时放在 A、构建时
    去 B 找"。
    """
    sources = list(iter_files(staged / "source"))
    if not sources:
        raise _package_error(
            f"随包带来的模板包里没有原件（source/）：{ref}",
            remediation=["向发件方要一份完整的导出件（导出端会把原件与契约一并打进来）"],
            where={"template_ref": ref},
        )
    if len(sources) > 1:
        raise _package_error(
            f"随包带来的模板包里有 {len(sources)} 份原件，无法判断哪一份才是：{ref}",
            remediation=["向发件方确认该模板包；一份包只应有一个原件"],
            where={"template_ref": ref, "sources": [item.name for item in sources]},
        )
    return sources[0]


def _manifest_field(staged: Path, field_name: str) -> str | None:
    """读内包清单里的一个字段；包读不回来时返回 ``None`` 而不是抛错。

    调用点（``doc_type`` / ``notes`` / ``package_hash`` / ``source_hash``）都是
    **描述性**字段：读不到就不写，比让一次导入因为"备注读不出来"失败合理。
    真正的完整性判据是 ``verify_package``，它管的是包内容本身。
    """
    try:
        package = read_package(staged)
    except EngineError:
        return None
    value = getattr(package.manifest, field_name, None)
    return None if value is None else str(value)


def _package_notes(
    entry: RegisteredProfile, document: DocTypeProfile, contents: Sequence[str]
) -> str:
    """包说明（``包说明.json``）：格式标识 + 内容清单 + 复现步骤。

    它是给**收件方的人**读的：一份包被转手几次之后，"这里面有什么、我该先做什么"
    只能靠包内的文件回答，不能靠文件名、也不能靠发件人的邮件。

    ``contents`` 由调用方传**完整**清单（档案目录里的文件 + 随包附带的那些），不在这里
    重算一遍：重算就要把"包是怎么打的"再推一次，而包一改这里就会漏。``包说明.json``
    自己不在清单里——一份清单列不出自己。
    """
    items = sorted(contents)
    payload = {
        "format": PROFILE_PACKAGE_FORMAT,
        "profile_ref": entry.ref,
        "profile_id": document.profile_id,
        # 版本号是**包的元数据**而不是档案的内容：``profile.json`` 里没有这个字段
        # （见模块文档"档案没有文档内版本号可解析"）。导入端据此还原登记版本，
        # 免得收件方把每一份包都装成 ``1.0``、彼此覆盖。
        "profile_version": entry.version,
        "doc_type": document.doc_type,
        "title": document.title,
        "fingerprint": entry.fingerprint,
        "contents": items,
        "template_packages": list(document.bindings.template_packages),
        "how_to_use": [
            "① 一步导入：POST /api/v1/profiles/import，请求体是本 zip 的字节",
            "   包内的模板包会一并登记，随后才登记档案本体（顺序不可颠倒）",
            "② 或在界面上用「导入档案包」",
            "③ 复现产出还需要一份事实输入：形状见 样例输入.json，" "适用条件见 适用判据.md",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sample_input(document: DocTypeProfile) -> str:
    """按档案自身的投影声明生成一份**自制样例输入**（FR-M2-25 / 验收 45）。

    它是什么
    --------
    一份**输入形状的样例**：档案的 ``projections`` 声明了"读哪些领域集合、取哪些字段、
    哪些字段缺了要进裁决队列"，这里就按那些声明逐条造出样例。

    它**不是**可以直接喂给某个接入器的文件。本系统的输入来自事实源（``source_type``
    决定怎么读），而"这份档案配哪条事实源"是**项目配置**（``DocumentEntry.source_ids``）
    决定的，不在档案里——档案刻意不认识项目。故它回答的是"你的输入得长成这样"，
    与 ``适用判据.md`` 回答"你的模板得满足什么"是同一类东西。

    为什么由代码生成而不是人手写一份放在仓库里
    ------------------------------------------
    手写的那份会与档案漂移：加一个 ``required`` 字段而样例没跟上，收件方照着样例准备
    输入，构建时才在裁决队列里发现缺字段。生成出来的那份**必然**随档案一起变。

    字段排序固定（先 ``fields`` 后 ``columns`` 再 ``required`` 去重），故同一份档案
    导出的样例逐字节相同。
    """
    kinds: dict[str, dict[str, list[str]]] = {}
    _collect_requirements(document.projections, kinds)
    payload: dict[str, object] = {
        "说明": (
            "本文件由导出端按档案自身的投影声明生成，回答「这份档案需要什么形状的事实输入」。"
            "它不是某个接入器的输入文件——输入由项目配置里的 source_ids 指向的事实源提供。"
        ),
        "profile_id": document.profile_id,
        "doc_type": document.doc_type,
        "领域集合": {
            kind: {
                "字段": spec["fields"],
                "缺失即进入裁决队列": spec["required"],
                "样例行": [dict.fromkeys(spec["fields"] + spec["required"], "示例值")],
            }
            for kind, spec in sorted(kinds.items())
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _collect_requirements(
    projections: Sequence[ProjectionSpec], found: dict[str, dict[str, list[str]]]
) -> None:
    """递归收集 ``projections``（含 ``children``）声明用到的领域集合与字段。"""
    for spec in projections:
        # ``for_each`` 的两种写法：``'<kind>'`` 与 ``'<kind>.<列表字段>'``。取前一段——
        # 后一段是**同一集合内**的嵌套字段，不构成另一个集合。
        kind, _, _ = (spec.for_each or "").partition(".")
        if kind:
            slot = found.setdefault(kind, {"fields": [], "required": []})
            for field_name in (*spec.fields, *spec.columns):
                if field_name not in slot["fields"]:
                    slot["fields"].append(field_name)
            for field_name in spec.required:
                if field_name not in slot["required"]:
                    slot["required"].append(field_name)
        _collect_requirements(spec.children or (), found)


def _bundle_payloads(repository: Repository, document: DocTypeProfile) -> dict[str, bytes]:
    """档案引用的模板包 → 包内条目（``templates/<ref>/…``）。

    逐包先过 ``verify_package``：库里那条登记指向一个读不回来的包时**拒绝导出**，
    而不是打一个"看着有模板、其实打不开"的包出去。
    """
    payloads: dict[str, bytes] = {}
    missing: list[str] = []
    for ref in document.bindings.template_packages:
        entry = resolve_template(repository, ref)
        if entry is None:
            missing.append(ref)
            continue
        package_dir = Path(entry.package_dir)
        problems = verify_package(package_dir)
        if problems:
            raise _package_error(
                f"库里的模板包读不回来，无法打进档案包：{ref}",
                remediation=[
                    "先修好该模板包（重新导入并登记），或从这份档案的 bindings 里去掉它",
                    f"排除依据：GET /templates/{entry.template_id}",
                ],
                where={"template_ref": ref, "problems": problems},
            )
        for path in iter_files(package_dir):
            relative = relative_posix(path, package_dir)
            payloads[f"{TEMPLATE_BUNDLE_DIRNAME}/{ref}/{relative}"] = path.read_bytes()
    if missing:
        raise EngineError(
            ErrorCode.PROFILE_TEMPLATE_MISSING,
            f"档案引用的模板包不在库里，导出件会不完整：{missing}",
            where={"profile_id": document.profile_id, "missing": missing},
            remediation=[
                "先登记这些模板包（POST /templates/imports → 登记），再导出",
                "模板包要随档案一起交付，收件方才能在另一环境复现同样产出（验收 45）",
            ],
        )
    return payloads


def _applicability(document: DocTypeProfile) -> str:
    """``applies`` 三条判据的可读版本（导出件附带）。

    标题里带上 ``profile_id`` / ``doc_type`` / ``title``：一份包被转手几次之后，
    "这是哪份档案"只能靠文件内容回答，不能靠文件名。
    """
    applies = document.applies
    lines = [
        f"# 适用判据：{document.title}",
        "",
        f"- 档案标识：`{document.profile_id}`",
        f"- 文档类型：`{document.doc_type}`",
        f"- 规则编号：{PROFILE_RULE_ID}",
        "",
        "三条判据**任一不成立即视为不适配**（需求 2.6.1）：",
        "",
        f"1. 版式来源：{applies.layout_source}",
        f"2. 章节骨架为何可枚举：{applies.structure_enumerable}",
        f"3. 内容来源为何可追溯：{applies.content_traceable}",
        "",
    ]
    return "\n".join(lines)
