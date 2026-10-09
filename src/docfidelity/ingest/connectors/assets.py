"""图片素材接入（FR-M1-10 / DD-5.2）。

输入：图片目录。
抽取：按命名规范（``图 X-Y 名称.png``）索引截图与设计图，取出图号与标题，
并登记为工作区资产（``AssetRef``）。

DD-5.2 点名的陷阱是"命名不合规时给 Finding 而非静默跳过"。这条纪律在这里格外重要：
被静默跳过的图，在最终文档里表现为"这一节没有插图"——而"本来就没画"与"画了但没认出来"
在产物上完全一样。因此每个不合规文件都留一条告警。

命名模式是**配置**（``options.name_pattern``，命名捕获组见下面的默认值），
写死它就是把你家的文件命名习惯焊进引擎。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.fact import AssetRef, FactBundle, SourceConfig
from docfidelity.platform.fs import relative_posix
from docfidelity.platform.hashing import sha256_file

from ..emitter import FactEmitter
from ..protocol import ProbeResult
from ._support import option, option_str_list, root_path, source_files
from .images import image_size

#: 默认命名模式：``图 3-1 登录流程.png``。命名捕获组固定为 major / minor / title。
DEFAULT_NAME_PATTERN: str = r"^图\s*(?P<major>\d+)-(?P<minor>\d+)\s*(?P<title>.+)$"

#: 默认接的图片后缀。
DEFAULT_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".svg")

_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def media_type_of(suffix: str) -> str:
    """按后缀给 MIME 类型；未知后缀返回 ``application/octet-stream``。"""
    return _MEDIA_TYPES.get(suffix.lower(), "application/octet-stream")


class AssetsConnector:
    """图片目录接入器。"""

    name: ClassVar[str] = "assets"

    def _pattern(self, cfg: SourceConfig) -> re.Pattern[str]:
        raw = option(cfg, "name_pattern", DEFAULT_NAME_PATTERN)
        try:
            compiled = re.compile(raw)
        except re.error as exc:
            raise ConfigurationError(
                f"事实源 {cfg.source_id} 的 options.name_pattern 不是合法正则：{exc}",
                where={"source": cfg.source_id, "pattern": raw},
                remediation=["修正该正则；命名捕获组需要 major / minor / title"],
            ) from exc
        missing = [name for name in ("major", "minor", "title") if name not in compiled.groupindex]
        if missing:
            raise ConfigurationError(
                f"事实源 {cfg.source_id} 的 options.name_pattern 缺少命名分组：{missing}",
                where={"source": cfg.source_id, "pattern": raw},
                remediation=["在正则里补上 (?P<major>…) / (?P<minor>…) / (?P<title>…)"],
            )
        return compiled

    def _files(self, cfg: SourceConfig) -> list[Path]:
        suffixes = tuple(option_str_list(cfg, "suffixes", DEFAULT_SUFFIXES))
        return source_files(cfg, suffixes)

    def _entry(self, path: Path, relative: str, match: re.Match[str]) -> dict[str, Any]:
        major = match.group("major")
        minor = match.group("minor")
        size = image_size(path)
        width, height = size if size is not None else (None, None)
        return {
            "figure": f"{major}-{minor}",
            "figure_major": int(major),
            "figure_minor": int(minor),
            "title": match.group("title").strip(),
            "logical_name": relative,
            "media_type": media_type_of(path.suffix),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "width_px": width,
            "height_px": height,
        }

    def probe(self, cfg: SourceConfig) -> ProbeResult:
        pattern = self._pattern(cfg)
        files = self._files(cfg)
        matched = [path for path in files if pattern.search(path.stem)]
        warnings = tuple(
            f"命名不符合规范：{path.name}" for path in files if not pattern.search(path.stem)
        )
        return ProbeResult(
            ok=bool(files),
            detail=f"{len(files)} 个图片文件，其中 {len(matched)} 个符合命名规范",
            item_count=len(matched),
            warnings=warnings,
        )

    def load(self, cfg: SourceConfig) -> FactBundle:
        pattern = self._pattern(cfg)
        root = root_path(cfg)
        emitter = FactEmitter(cfg)
        for path in self._files(cfg):
            relative = relative_posix(path, root) if root.is_dir() else path.name
            match = pattern.search(path.stem)
            if match is None:
                emitter.warn(f"命名不符合规范，已跳过：{relative}", locator=relative)
                continue
            entry = self._entry(path, relative, match)
            if not entry["title"]:
                emitter.warn(f"命名里缺少标题：{relative}", locator=relative)
            if entry["width_px"] is None:
                emitter.warn(f"无法解析像素尺寸（图不会越界检查）：{relative}", locator=relative)
            emitter.add_asset(
                AssetRef(
                    logical_name=str(entry["logical_name"]),
                    relative_path=relative,
                    media_type=str(entry["media_type"]),
                    sha256=str(entry["sha256"]),
                    size_bytes=int(entry["size_bytes"]),
                    width_px=entry["width_px"],
                    height_px=entry["height_px"],
                )
            )
            emitter.emit("asset", entry, locator=relative, asset_names=[relative])
        return emitter.bundle()


__all__ = ["DEFAULT_NAME_PATTERN", "DEFAULT_SUFFIXES", "AssetsConnector", "media_type_of"]
