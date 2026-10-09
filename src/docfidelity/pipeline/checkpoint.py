"""阶段断点：续跑的正确性全押在这里（DD-11.3 / 设计文档第 20 章风险 #6）。

一个阶段跑完，就把「产出」连同「输入摘要 + 阶段实现版本」一起落盘。下次再跑同一
作业时，只要**版本一致**且**输入未变**，该阶段直接跳过、沿用产出（DD-11.3 的
``run_stage``）。这条规则有两个反面，两边都会给出"看着是绿的、其实是错的"结果：

* 只看"断点存在"就复用 —— 阶段实现换了口径，旧产出却被沿用，产物停留在旧实现上。
  所以 :data:`~docfidelity.contracts.job.STAGE_IMPL_VERSION` 必须参与判定。
* 只看"阶段版本一致"就复用 —— 上游输入变了（换模板版本、改配置），该阶段照样跳过，
  产物悄无声息地停在旧输入上。所以 ``inputs_hash`` 必须参与判定。

两者缺一不可，而判定**只有这一处实现**：:func:`decide_reuse`。调用方（``stages``）
不得自己写"有没有断点"的判断——那种写法迟早会漏掉其中一条。

为什么落盘格式是 JSON 而不是 pickle
----------------------------------
断点会被**跨版本**读取（续跑正是这个场景），pickle 的兼容性由类定义决定，
一次重构就再也读不出来；JSON 读不出来时还能被人看一眼。读不出来的断点**不删、
也不复用**：抛 :class:`CorruptCheckpoint`（``fatal=False``），由调用方记一条判定并
重跑该阶段——静默删除会把"磁盘坏了"伪装成"本来就该重跑"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from docfidelity.contracts.errors import ConfigurationError, EngineError, ErrorCode
from docfidelity.contracts.job import (
    STAGE_IMPL_VERSION,
    STAGE_NAMES,
    StageCheckpoint,
)
from docfidelity.platform.fs import (
    atomic_write_text,
    ensure_dir,
    read_text,
    safe_join,
)

__all__ = [
    "CHECKPOINT_SUFFIX",
    "WORK_DIRNAME",
    "CheckpointStore",
    "CorruptCheckpoint",
    "FileCheckpointStore",
    "MemoryCheckpointStore",
    "ReuseDecision",
    "ReuseReason",
    "decide_reuse",
    "impl_version_of",
    "path_segment",
]

#: 断点文件名后缀。文件名为 ``<stage>.checkpoint.json``。
CHECKPOINT_SUFFIX: Final = ".checkpoint.json"

#: 工作区内的中间态目录名（DD-11.7）。
WORK_DIRNAME: Final = "work"

#: 断点无法复用时的判定依据。取值是闭集，便于调用方按原因给出不同提示。
ReuseReason = Literal["missing", "version-mismatch", "inputs-changed", "reusable"]

_FORBIDDEN_SEGMENTS: Final[frozenset[str]] = frozenset({"", ".", ".."})

#: 任何主流平台上都不能出现在**单个**路径片段里的字符。
#:
#: Windows 禁 ``< > : " / \ | ? *``；POSIX 只禁 ``/`` 与 NUL。取并集：本引擎的产物
#: 要被 Windows 上的 Word 打开，故 Windows 的限制就是硬约束。
#:
#: ``:`` 是这一条被补上的原因——服务端曾用 ``<编排id>:<文档键>`` 作为子作业 id，
#: 在 POSIX 上一切正常，到 Windows 上就成了 ``NotADirectoryError``，
#: 而且报错点在**建目录**处，离"id 怎么拼的"很远。
_UNSAFE_SEGMENT_CHARS: Final = frozenset('<>:"/\\|?*')


def _is_unsafe_segment(value: str) -> str | None:
    """返回不合法的原因，合法则返回 ``None``。"""
    if value in _FORBIDDEN_SEGMENTS:
        return "空 / 当前目录 / 父目录记号"
    if bad := sorted(_UNSAFE_SEGMENT_CHARS & set(value)):
        return f"含路径片段非法字符 {bad}"
    if any(ord(char) < 32 for char in value):
        return "含控制字符"
    if value != value.strip() or value.endswith("."):
        return "首尾空白或结尾的点（Windows 会静默去掉，两个 id 会映射到同一个目录）"
    return None


def impl_version_of(stage: str) -> int:
    """阶段实现版本；未登记的阶段**立刻报错**。

    宁可在这里失败，也不要在续跑判定里用一个"默认版本 0"——那会让所有阶段
    都判定为"版本不匹配"从而永不续跑（看起来正常，只是慢），或者相反。
    """
    version = STAGE_IMPL_VERSION.get(stage)
    if version is None:
        raise ConfigurationError(
            f"未登记的阶段实现版本：{stage!r}",
            where={"stage": stage, "registered": sorted(STAGE_IMPL_VERSION)},
            remediation=["在 contracts.job.STAGE_IMPL_VERSION 中登记该阶段，并记录到 CHANGELOG"],
        )
    return version


@dataclass(frozen=True)
class ReuseDecision:
    """某阶段的断点能否复用。

    ``checkpoint`` 在 ``reason`` 为 ``missing`` 时为 ``None``；其余情况指向被判定
    的那个断点（便于调用方展示"旧版本几、旧输入摘要几"）。
    """

    reason: ReuseReason
    checkpoint: StageCheckpoint | None = None

    @property
    def reusable(self) -> bool:
        return self.reason == "reusable"

    @property
    def must_run(self) -> bool:
        return not self.reusable

    def explain(self) -> str:
        """人读一句话，直接进日志与界面。"""
        table = {
            "missing": "没有可用断点",
            "version-mismatch": "阶段实现已变更，历史断点失效",
            "inputs-changed": "本阶段输入已变化",
            "reusable": "断点有效，沿用产出",
        }
        return table[self.reason]


def decide_reuse(
    checkpoint: StageCheckpoint | None, *, stage: str, inputs_hash: str
) -> ReuseDecision:
    """判定断点能否复用。**这是唯一实现**，调用方不得另写判断。

    判定顺序是刻意的：先"有没有"，再"实现版本",最后"输入"——因为三者的处置
    都相同（重跑），但**原因**不同，而原因决定界面给用户看什么（"升级导致的"
    与"你改了配置"是两条完全不同的操作指引）。
    """
    if checkpoint is None or checkpoint.stage != stage:
        # 断点存到了别处（stage 字段对不上）与"没有断点"同义：都不足以支撑复用。
        return ReuseDecision("missing")
    if checkpoint.version != impl_version_of(stage):
        return ReuseDecision("version-mismatch", checkpoint)
    if checkpoint.inputs_hash != inputs_hash:
        return ReuseDecision("inputs-changed", checkpoint)
    return ReuseDecision("reusable", checkpoint)


class CorruptCheckpoint(EngineError):
    """断点文件存在但读不出来。

    ``fatal=False``：它**不是**作业失败，只说明这个阶段必须重跑。调用方据此
    记一条判定并继续，而不是中断整条流水线。
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(
            ErrorCode.CHECKPOINT_MISSING,
            f"阶段断点无法解析，将重跑该阶段：{path}",
            where={"path": str(path), "detail": detail},
            remediation=["确认磁盘可读；如需彻底重来，删除该断点文件"],
            fatal=False,
        )


class CheckpointStore(Protocol):
    """断点仓储。生产实现落盘，测试用内存实现。"""

    def load(self, job_id: str, stage: str) -> StageCheckpoint | None:
        """取断点；不存在返回 ``None``，读不出来抛 :class:`CorruptCheckpoint`。"""
        ...

    def save(self, job_id: str, checkpoint: StageCheckpoint) -> None:
        """写入断点（覆盖同阶段的旧断点）。"""
        ...

    def stages(self, job_id: str) -> dict[str, StageCheckpoint]:
        """该作业已有的全部断点，按阶段名索引。"""
        ...

    def discard(self, job_id: str, stage: str | None = None) -> int:
        """丢弃断点，返回真正删掉的个数；``stage=None`` 表示该作业全部阶段。

        为什么协议里必须有这一条（P0-13 收尾时发现缺）：DD-11.2 的 ``restart``
        语义是"从头重跑"，而续跑的判定完全由断点决定——**删不掉断点就等于跑不了
        ``restart``**：``run_stage`` 会依次命中旧断点、全部复用，界面显示"重跑成功"
        而阶段一次都没执行。同理，``retry_stage``（重跑某个**已成功**的阶段）也依赖它。

        用"删断点"而不是"改 inputs_hash 骗过复用判定"：后者会把一个假摘要写进断点，
        使 ``inputs_hash`` 再也回答不了"这次跑的到底是不是这份输入"。
        """
        ...


class MemoryCheckpointStore:
    """内存断点仓储（单进程测试与短生命周期作业）。"""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], StageCheckpoint] = {}

    def load(self, job_id: str, stage: str) -> StageCheckpoint | None:
        return self._items.get((job_id, stage))

    def save(self, job_id: str, checkpoint: StageCheckpoint) -> None:
        self._items[(job_id, checkpoint.stage)] = checkpoint

    def stages(self, job_id: str) -> dict[str, StageCheckpoint]:
        return {
            stage: checkpoint
            for (owner, stage), checkpoint in self._items.items()
            if owner == job_id
        }

    def discard(self, job_id: str, stage: str | None = None) -> int:
        targets = [stage] if stage is not None else list(STAGE_NAMES)
        removed = 0
        for name in targets:
            if self._items.pop((job_id, name), None) is not None:
                removed += 1
        return removed


def path_segment(value: str, *, field: str) -> str:
    """把作业 id / 阶段名裁成**单个**路径片段，越界即报错。

    作业 id 来自服务端、阶段名来自契约，两者都不该含路径分隔符。这里不做
    "把 ``/`` 替换成 ``_``"的修补——那会把两个不同的 id 映射到同一个目录，
    续跑时互相覆盖（P0-06 的 ``unit_id`` 撞车就是这个模式）。

    判据是**任何平台**都不能用的字符集，而不只是分隔符（见
    :data:`_UNSAFE_SEGMENT_CHARS`）：一个在 POSIX 上合法的 id，不该因为部署到
    Windows 就变成一次 ``NotADirectoryError``。
    """
    reason = _is_unsafe_segment(value)
    if reason is not None:
        raise ConfigurationError(
            f"{field} 不能作为路径片段（{reason}）：{value!r}",
            where={field: value},
            remediation=["使用不含路径分隔符的标识，或改用 MemoryCheckpointStore"],
        )
    return value


class FileCheckpointStore:
    """文件断点仓储：``<root>/jobs/<job_id>/work/<stage>.checkpoint.json``（DD-11.7）。"""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def job_dir(self, job_id: str) -> Path:
        """作业目录（``jobs/<job_id>``）；同样做片段校验。"""
        return safe_join(self._root, "jobs", path_segment(job_id, field="job_id"))

    def path_for(self, job_id: str, stage: str) -> Path:
        """断点文件路径。"""
        return safe_join(
            self.job_dir(job_id),
            WORK_DIRNAME,
            path_segment(stage, field="stage") + CHECKPOINT_SUFFIX,
        )

    def load(self, job_id: str, stage: str) -> StageCheckpoint | None:
        path = self.path_for(job_id, stage)
        if not path.is_file():
            return None
        raw = read_text(path)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptCheckpoint(path, f"JSON 解析失败：{exc.msg}") from exc
        if not isinstance(payload, dict):
            raise CorruptCheckpoint(path, f"顶层应为对象，实为 {type(payload).__name__}")
        try:
            return StageCheckpoint.model_validate(payload)
        except ValueError as exc:  # pydantic ValidationError 亦是 ValueError
            raise CorruptCheckpoint(path, f"契约校验失败：{exc}") from exc

    def save(self, job_id: str, checkpoint: StageCheckpoint) -> None:
        path = self.path_for(job_id, checkpoint.stage)
        ensure_dir(path.parent)
        payload = checkpoint.model_dump(mode="json")
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    def stages(self, job_id: str) -> dict[str, StageCheckpoint]:
        found: dict[str, StageCheckpoint] = {}
        for stage in STAGE_NAMES:
            checkpoint = self.load(job_id, stage)
            if checkpoint is not None:
                found[stage] = checkpoint
        return found

    def discard(self, job_id: str, stage: str | None = None) -> int:
        targets = [stage] if stage is not None else list(STAGE_NAMES)
        removed = 0
        for name in targets:
            path = self.path_for(job_id, name)
            if path.is_file():
                # ``missing_ok`` 之外再判一次 ``is_file``：并发下两个 worker 可能同时
                # 丢弃同一份断点，先判后删才不会把后者变成异常（丢弃是幂等操作）。
                path.unlink(missing_ok=True)
                removed += 1
        return removed
