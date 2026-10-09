"""模板库的读取与治理（DD-13.2 的 ``templates/{tid}`` 一族）。

为什么与入库流水线分家
----------------------
:mod:`server.templates` 管的是**一条流水线**（收件 → 解析 → 确认 → 登记，P05 第 1~5 步），
它的状态是"这次导入走到了哪一步"；本模块管的是**一座库**（列出、看详情、比版本、
废弃、导出），它的状态是"库里现在有什么"。两者的寿命不同：一次导入会结束，
库会一直在长。

分家的直接动因是 NFR-09 的单文件行数上限——合在一起已经 750 行以上，再加一处
改动就会越线。但**分界线取自职责而不是行数**：真按行数切，会切出"前半段函数在
A、后半段在 B"这种必须在两个文件间跳着读的东西。

只读 + 治理，不改内容
--------------------
本模块**不写包内容**：详情与导出都是读，废弃只改登记元数据（``deprecated``），
包目录一个字节都不动。理由与档案侧一致——模板版本是配置与历史产物的引用键
（``DocumentEntry.template_ref``），改包内容会让"这份文档是按哪个模板产出的"
失去可复现性。要换内容就重新导入并登记一个新版本号。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import TemplatePackage
from docfidelity.pipeline import (
    ProfileReference,
    RegisteredProfile,
    VersionDiff,
    diff_packages,
    export_package,
    fingerprint_problems,
    path_segment,
    read_package,
    referencing_profiles,
    verify_package,
)

from .profiles import read_profile_document
from .storage import RegisteredTemplate

if TYPE_CHECKING:
    from .settings import ServerSettings
    from .storage import Repository

__all__ = ["TemplateLibraryMixin"]


class TemplateLibraryMixin:
    """模板库（``Services`` 的第六个 mixin，见 ``server.services``）。

    只**用** ``self.repository`` / ``self.settings`` / ``self.now``。
    """

    if TYPE_CHECKING:
        repository: Repository
        settings: ServerSettings
        now: Callable[[], datetime]

    # ---------------------------------------------------------------- 列表
    def list_templates(
        self, *, template_id: str | None = None, include_deprecated: bool = True
    ) -> list[RegisteredTemplate]:
        entries = self.repository.list_templates(template_id=template_id)
        if include_deprecated:
            return entries
        return [entry for entry in entries if not entry.deprecated]

    def get_template(self, template_id: str, version: str | None = None) -> RegisteredTemplate:
        """取某个版本；``version`` 为空取**最新一版**（按 ``registered_at``）。

        与档案侧（``server.profiles``）同口径：界面上"打开这个模板包"想看的是当前
        有效的那一版，而"最新"由仓储的排序给出（两种实现同一次序，见 ``protocols``）。
        """
        entries = self.repository.list_templates(
            template_id=path_segment(template_id, field="template_id")
        )
        if not entries:
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到模板包：{template_id}",
                where={"template_id": template_id},
                remediation=["按 GET /api/v1/templates 列出已登记的模板包"],
            )
        if version is None:
            return entries[-1]
        wanted = path_segment(version, field="version")
        for entry in entries:
            if entry.version == wanted:
                return entry
        raise EngineError(
            ErrorCode.NOT_FOUND,
            f"找不到模板版本：{template_id}@{version}",
            where={"template_id": template_id, "version": version},
            remediation=["列出该模板的全部版本后重试"],
        )

    # ---------------------------------------------------------------- 详情
    def template_detail(
        self, template_id: str, version: str | None = None
    ) -> tuple[RegisteredTemplate, TemplatePackage | None, list[str], list[str]]:
        """一个模板版本的详情，返回 ``(登记, 包, 完整性, 指纹问题)``。

        包为 ``None`` 表示这次登记**只有原件**（解析没成功）；此时两个问题清单都是空
        列表——不是"没问题"，而是"没有可查的东西"，界面必须按"未解析"渲染而不是
        渲染成一张全绿的检查表（硬规则 11 的同一条理由）。

        两条检查各查一件事，都要显示：``verify_package`` 查"包内容与清单是否一致"
        （被外部改过没有），``fingerprint_problems`` 查"指纹与包内容是否一致"
        （改完指纹又动过内容没有）。
        """
        entry = self.get_template(template_id, version)
        if not entry.parsed:
            return entry, None, [], []
        package_dir = Path(entry.package_dir)
        package = read_package(package_dir)
        return (
            entry,
            package,
            verify_package(package_dir),
            fingerprint_problems(package_dir),
        )

    def template_diff(self, template_id: str, before: str, after: str) -> VersionDiff:
        """两个版本之间的字段级差异（P04 的"版本对比"）。"""
        first = self.get_template(template_id, before)
        second = self.get_template(template_id, after)
        for entry in (first, second):
            if not entry.parsed:
                raise EngineError(
                    ErrorCode.NO_BASELINE,
                    f"无法对比：{entry.ref} 未解析出契约",
                    where={"template_ref": entry.ref, "parsed": False},
                    remediation=[
                        "该版本只登记了原件；重新导入并完成解析后再对比",
                        "或改选两个已解析的版本",
                    ],
                )
        return diff_packages(Path(first.package_dir), Path(second.package_dir))

    def template_references(
        self, template_id: str, version: str | None = None
    ) -> list[ProfileReference]:
        """哪些档案引用了这个模板包（P04 概要页签的「引用该包的作业与文档」）。

        两个粒度都要，用途不同：

        * ``version`` 给了 → 精确一版。"**这一版**还有人在用吗"是废弃判据，
          答错会得到"废弃完立刻被预检拦住"；
        * ``version`` 为空 → 该编号的**任意版本**。编号层面改名 / 下架时要通知谁。

        判定在 :func:`docfidelity.pipeline.profiles.referencing_profiles`——与档案登记时
        校验 ``bindings.template_packages`` 的是同一份实现（"引用的书写形式长什么样"
        只能有一个答案）。本方法只负责把已登记档案读齐（含已废弃版本：废弃的档案
        仍在引用这个包，漏掉它们会让判据凭空放宽）。

        先 :meth:`get_template` 一次，是为了**编号写错**与"没人引用"给出不同的回答：
        前者 404，后者是一份空清单。两者在界面上长得像，但下一步动作完全不同。
        """
        self.get_template(template_id, version)
        registered = [
            RegisteredProfile(
                version=item.version,
                profile=read_profile_document(item),
                deprecated=item.deprecated,
            )
            for item in self.repository.list_profiles()
        ]
        return referencing_profiles(registered, template_id=template_id, version=version)

    # ---------------------------------------------------------------- 治理
    def deprecate_template(
        self,
        template_id: str,
        *,
        version: str,
        reason: str,
        operator: str | None = None,
    ) -> RegisteredTemplate:
        """标记一个模板版本为废弃（**不删除**）。

        只标不删的理由与档案一致：模板版本是配置与历史产物的引用键
        （``DocumentEntry.template_ref``）。包目录**原样保留**，故已经产出的文档照旧
        可复现；废弃的效果只是"列默认不再展示、发起新构建时不再可选"。

        ``reason`` 必填且不得空白——一次没有理由的废弃在三个月后与"误操作"无法区分。
        """
        if not (reason or "").strip():
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                "废弃模板版本必须给出理由",
                where={"template_id": template_id, "version": version},
                remediation=["在请求体里给出 reason：为什么这个版本不再可用"],
            )
        entry = self.get_template(template_id, version)
        if entry.deprecated:
            # 幂等：重复废弃不改写最早那次留痕（理由与时间）。
            return entry
        return self.repository.save_template(
            replace(
                entry,
                deprecated=True,
                deprecated_reason=reason.strip(),
                # 记下是谁办的：这是**留痕**，不参与任何判定（同 ``Job.initiated_by``）。
                deprecated_by=operator or entry.deprecated_by,
            )
        )

    def export_template(self, template_id: str, version: str | None = None) -> tuple[str, bytes]:
        """导出模板包（zip），返回 ``(建议文件名, 字节)``。"""
        entry = self.get_template(template_id, version)
        if not entry.parsed:
            raise EngineError(
                ErrorCode.PACKAGE_INCOMPLETE,
                f"{entry.ref} 只登记了原件，没有可导出的模板包",
                where={"template_ref": entry.ref, "parsed": False},
                remediation=["重新导入并完成解析后再导出", "暂存目录里的原件仍在（imports/）"],
            )
        return f"{entry.template_id}@{entry.version}.zip", export_package(Path(entry.package_dir))
