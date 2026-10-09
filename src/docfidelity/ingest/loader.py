"""接入器执行、事实包规范化与失败分级（DD-5.1 / DD-7.1 的接入侧落地）。

排序与摘要在本模块各只有一处实现
--------------------------------
接入器返回的事实集合先经 :func:`build_bundle` 规范化：顺序由**内容**决定，
摘要由规范化后的内容算出。若让每个接入器自己算摘要，同一份输入会因为遍历顺序
不同算出两个摘要——"输入没变"于是被判成"输入变了"，下游每次都重跑。

失败分级
--------
- 接入器抛 ``EngineError``：原样上抛（它的 code / remediation 比这里更具体）。
- 接入器抛其它异常：包成 ``E_SOURCE_LOAD_FAILED`` 抛出，**不吞**。
  吞掉的后果是"来源读不到"与"来源本来就没有内容"在产物上完全一样，
  而前者会让整章内容凭空消失且报告全绿。
- ``BaseException``（``KeyboardInterrupt`` / ``SystemExit``）本就不在捕获范围内。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from docfidelity.contracts.errors import EngineError, EngineErrorInfo, ErrorCode
from docfidelity.contracts.fact import (
    AssetRef,
    Fact,
    FactBundle,
    SourceConfig,
    SourceType,
)
from docfidelity.platform.hashing import digest_payload

from .protocol import FactSourceConnector, ProbeResult
from .safety import SourceGuard


def fact_sort_key(fact: Fact) -> tuple[str, tuple[str, ...], str, str]:
    """事实的规范序：kind → 键 → 定位 → payload 摘要。

    末尾挂 payload 摘要是为了得到**全序**。同一 (kind, 键, 定位) 上出现两条不同事实
    （同一个位置被两种读法读出不同值）时，前三个分量相等；若排序键到此为止，
    ``sorted`` 的顺序就退回入参顺序，摘要随之不稳定。
    """
    return (fact.kind, tuple(fact.key), fact.locator, digest_payload(fact.payload))


def fact_digest(facts: Sequence[Fact], assets: Sequence[AssetRef]) -> str:
    """事实包摘要。只覆盖事实与资产，**不含** ``loaded_at`` 与入参顺序。"""
    return digest_payload(
        {
            "facts": [fact.model_dump(mode="json") for fact in facts],
            "assets": [asset.model_dump(mode="json") for asset in assets],
        }
    )


def build_bundle(
    *,
    source_id: str,
    source_type: SourceType,
    facts: Iterable[Fact],
    assets: Iterable[AssetRef] = (),
    warnings: Iterable[EngineErrorInfo] = (),
) -> FactBundle:
    """**唯一**的事实包构造入口：统一排序 + 统一算摘要。"""
    ordered_facts = sorted(facts, key=fact_sort_key)
    ordered_assets = sorted(assets, key=lambda asset: asset.logical_name)
    return FactBundle(
        source_id=source_id,
        source_type=source_type,
        facts=list(ordered_facts),
        assets=list(ordered_assets),
        digest=fact_digest(ordered_facts, ordered_assets),
        warnings=list(warnings),
    )


def skip_warning(message: str, *, where: Mapping[str, object] | None = None) -> EngineErrorInfo:
    """构造一条**非致命**告警（进事实包，不阻断）。

    复用 ``E_UPLOAD_TYPE`` 的语义（"这一类东西按策略不接入"）但把 ``fatal`` 置为
    ``False``——它描述的是"跳过了什么"，不是"失败了"。
    """
    return EngineErrorInfo(
        code=ErrorCode.UPLOAD_TYPE,
        message=message,
        where={key: str(value) for key, value in (where or {}).items()},
        fatal=False,
    )


def _canonical(bundle: FactBundle, extra: Iterable[EngineErrorInfo]) -> FactBundle:
    return build_bundle(
        source_id=bundle.source_id,
        source_type=bundle.source_type,
        facts=bundle.facts,
        assets=bundle.assets,
        warnings=[*bundle.warnings, *extra],
    )


def _require_identity(bundle: FactBundle, cfg: SourceConfig) -> None:
    """接入器必须原样回填 ``source_id`` / ``source_type``。

    回填错的后果不是报错，而是**事实被挂到别的来源名下**：冲突裁决会按错误的优先级
    判，产物上完全看不出来。
    """
    if bundle.source_id != cfg.source_id or bundle.source_type != cfg.source_type:
        raise EngineError(
            ErrorCode.INTERNAL,
            f"接入器回填的来源标识与配置不符：{bundle.source_id}/{bundle.source_type} "
            f"!= {cfg.source_id}/{cfg.source_type}",
            where={
                "expected_source_id": cfg.source_id,
                "expected_source_type": cfg.source_type,
                "got_source_id": bundle.source_id,
                "got_source_type": bundle.source_type,
            },
            remediation=["接入器必须原样回填 SourceConfig 的 source_id 与 source_type"],
        )


def run_connector(
    connector: FactSourceConnector,
    cfg: SourceConfig,
    *,
    guard: SourceGuard | None = None,
) -> FactBundle:
    """执行接入器，返回规范化后的事实包。

    Parameters
    ----------
    guard:
        给出时，先按工作区与容量策略把关（敏感文件 / 可执行 / 大小 / 压缩包条目）。
        把关产生的告警会被带进事实包。**把关同时把 ``root`` 解析到工作区内**：
        接入器只吃成形路径，于是"``root`` 相对工作区根"这条契约只有
        :meth:`SourceGuard.resolve` 一个实现点（见 :func:`probe_connector` 的说明）。
    """
    extra: list[EngineErrorInfo] = []
    if guard is not None:
        target, skipped = guard.check(cfg)
        extra = [skip_warning(text, where={"source": cfg.source_id}) for text in skipped]
        if target is not None:
            cfg = cfg.model_copy(update={"root": str(target)})

    try:
        bundle = connector.load(cfg)
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError(
            ErrorCode.SOURCE_LOAD_FAILED,
            f"事实源接入失败：{cfg.source_id}",
            where={"source": cfg.source_id, "source_type": cfg.source_type},
            remediation=["检查来源路径与格式；该来源已跳过，其余来源继续"],
        ) from exc

    _require_identity(bundle, cfg)
    return _canonical(bundle, extra)


def probe_connector(
    connector: FactSourceConnector,
    cfg: SourceConfig,
    *,
    guard: SourceGuard | None = None,
) -> ProbeResult:
    """「试接入」：**永不抛错**，失败也返回结果。

    探针服务于界面上的一个按钮，抛异常会让按钮变成 500。

    ``guard`` 与 :func:`run_connector` 同义，且**必须与它用同一个解析点**：
    接入器内部是按 :class:`pathlib.Path` 直接读文件的（它不知道工作区在哪），
    所以"谁把相对 ``root`` 变成绝对路径"这件事若有第二个答案，"试接入说通了、
    真接入却说找不到"就会成为常态——而这两句话来自同一个 ``root``。

    把关本身也在"永不抛"的范围里：``root`` 越界是个**结果**（``ok=False``，附一句
    说明），不是异常。想要逐条的问题清单请用配置期校验（``pipeline.sources.check_config``）。
    """
    try:
        skipped: list[str] = []
        if guard is not None:
            target, skipped = guard.check(cfg)
            if target is not None:
                cfg = cfg.model_copy(update={"root": str(target)})
        result = connector.probe(cfg)
    except Exception as exc:  # 探针的职责就是把失败变成结果
        return ProbeResult(ok=False, detail=f"探测失败：{exc}")
    if not skipped:
        return result
    return replace(result, warnings=(*result.warnings, *skipped))


__all__ = [
    "build_bundle",
    "fact_digest",
    "fact_sort_key",
    "probe_connector",
    "run_connector",
    "skip_warning",
]
