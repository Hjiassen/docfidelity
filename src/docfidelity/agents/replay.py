"""模型决策的离线录制与回放（DD-17.3 / NFR-17 / NFR-18）。

它解决的是什么
--------------
"提示词或工具描述改一个字，就必须重跑一遍模型路径并附上结果"（NFR-17）——这句话在
本模块之前只是一句话：``instructions.instructions_digest`` 早就算得出摘要，但**没有
任何东西把它存下来、也没有任何东西拿它比对**，于是"改完忘了跑"永远是静默的。

本模块补的就是这一环：录制件里带上当时的 ``instructions`` 摘要，回放时先比摘要——
不一致即判定**过期**，要求重跑。摘要变了而没人重跑，"模型路径的回归结果"就成了一句
无法核对的声明。

回放能力不是测试专用品（``sdk_adapter`` 的模块文档也这么说）
----------------------------------------------------------
外部使用者不愿为验证功能承担模型 API 费用，故"把一次真实调用的输出录下来、之后离线
重放"是开源版的必要退路。故格式与读写都在内核，测试只负责驱动。

录制件的形状
------------
``tests/replay/<decision_point>/<hash>.json``，``hash = sha256(run_id|decision_point)``
的前 12 位——**以 ``run_id + decision_point`` 为键**（DD-17.3 的字面）。同一个决策点
只保留一份录制，故脚本注入（``ScriptedAdapter.script`` 是"决策点 → 输出"的映射）
无歧义；要覆盖多个样本就换 ``run_id``。

两类用例
--------
* ``decision``：带完整的 ``request`` 与 ``outcome``，回放时重走一遍路由；
* ``guard``：带一段指令文本与期望命中的意图，验输入护栏。

分开的理由是它们断言的东西不同：前者验"模型结论被正确采纳"，后者验"越权指令被拦下"。
DD-17.1 那句"录制响应回放 + guardrail 命中"是两件事，合成一条用例会让失败信息说不清
是哪一半坏了。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Literal

from .instructions import instructions_digest
from .routing import AdapterOutcome

__all__ = [
    "REPLAY_FORMAT_VERSION",
    "ReplayCase",
    "case_hash",
    "case_path",
    "current_instructions",
    "load_cases",
    "save_case",
    "script_of",
    "stale_cases",
]

#: 录制件的格式版本。与 ``instructions.INSTRUCTIONS_VERSION`` 是**两件事**：后者管
#: "模型看到的输入变了没有"，前者管"这个文件的形状变了没有"。混在一起会让"改一条
#: 录制"看起来像"改了提示词"，那时回放会把所有用例都判成过期。
REPLAY_FORMAT_VERSION: Final = "1.0.0"

#: 文件名里保留的摘要位数。12 位十六进制 = 48 bit，碰撞概率对"每决策点一份"这个量级
#: 可以忽略；用全长摘要会让目录难读，而这份文件是要被人打开看的。
HASH_CHARS: Final = 12

CaseKind = Literal["decision", "guard"]


@dataclass(frozen=True)
class ReplayCase:
    """一条录制用例。``decision`` 与 ``guard`` 各自只填自己那几个字段。"""

    kind: CaseKind
    run_id: str
    decision_point: str
    #: 录制当时的 ``instructions_digest``。回放时比对，不等即过期（NFR-17）。
    instructions: str
    request: Mapping[str, Any] | None = None
    outcome: Mapping[str, Any] | None = None
    instruction: str | None = None
    intent: str | None = None
    format_version: str = REPLAY_FORMAT_VERSION

    def as_payload(self) -> dict[str, Any]:
        """落盘形状。**字段全写**（含 ``null``）：少写一个键会让人分不清"没有这项"
        与"这一版没有这个键"，而录制件是要长期留在仓库里的。"""
        return {
            "format_version": self.format_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "decision_point": self.decision_point,
            "instructions": self.instructions,
            "request": None if self.request is None else dict(self.request),
            "outcome": None if self.outcome is None else dict(self.outcome),
            "instruction": self.instruction,
            "intent": self.intent,
        }

    @classmethod
    def of(cls, payload: Mapping[str, Any]) -> ReplayCase:
        version = str(payload.get("format_version") or "")
        if version != REPLAY_FORMAT_VERSION:
            raise ValueError(
                f"录制件的格式版本是 {version!r}，本代码只认 {REPLAY_FORMAT_VERSION!r}："
                f"格式变了就要重新录制（{payload.get('decision_point')}）"
            )
        return cls(
            kind=_kind_of(payload.get("kind")),
            run_id=str(payload.get("run_id") or ""),
            decision_point=str(payload.get("decision_point") or ""),
            instructions=str(payload.get("instructions") or ""),
            request=payload.get("request"),
            outcome=payload.get("outcome"),
            instruction=payload.get("instruction"),
            intent=payload.get("intent"),
        )


def _kind_of(raw: Any) -> CaseKind:
    if raw == "decision":
        return "decision"
    if raw == "guard":
        return "guard"
    raise ValueError(f"录制件的 kind 只能是 decision / guard，收到 {raw!r}")


def case_hash(run_id: str, decision_point: str) -> str:
    """``run_id + decision_point`` 的短摘要，用作文件名（DD-17.3）。"""
    material = f"{run_id}|{decision_point}".encode()
    return sha256(material).hexdigest()[:HASH_CHARS]


def case_path(root: str | Path, case: ReplayCase) -> Path:
    return Path(root) / case.decision_point / f"{case_hash(case.run_id, case.decision_point)}.json"


def save_case(root: str | Path, case: ReplayCase) -> Path:
    """写一条录制件（缩进 2 + 末尾换行：它要能被 diff 与评审）。"""
    target = case_path(root, case)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(case.as_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_cases(root: str | Path) -> tuple[ReplayCase, ...]:
    """读全部录制件，按 ``(decision_point, run_id)`` 排序（确定性遍历）。

    空目录**报错**而不是返回空元组：那意味着"回放通过"是一句空话。
    """
    base = Path(root)
    if not base.is_dir():
        raise FileNotFoundError(f"录制目录不存在：{base}")
    cases: list[ReplayCase] = []
    for path in sorted(base.glob("*/*.json")):
        cases.append(ReplayCase.of(json.loads(path.read_text(encoding="utf-8"))))
    if not cases:
        raise FileNotFoundError(f"录制目录里一条用例都没有：{base}")
    return tuple(sorted(cases, key=lambda case: (case.decision_point, case.run_id)))


def current_instructions(tool_summaries: Mapping[str, str] | None = None) -> str:
    """当前的提示词 + 工具描述摘要。

    ``tool_summaries`` 缺省时**真的去取工具面**（``agents.tools.descriptions()``）：
    留一个"忘了传就只算提示词"的空档，等于让"工具描述改了"这条回归面静默失效——
    而它正是 NFR-17 点名的另一半。
    """
    if tool_summaries is None:
        from .tools import descriptions

        tool_summaries = descriptions()
    return instructions_digest(tool_summaries)


def stale_cases(
    cases: Iterable[ReplayCase], *, tool_summaries: Mapping[str, str] | None = None
) -> list[str]:
    """与当前提示词/工具描述不一致的用例（人读描述行，空列表 = 全都新鲜）。

    **调用方必须把非空当成失败**：这正是 NFR-17 的"改提示词必须重跑回放"落地的地方。
    """
    current = current_instructions(tool_summaries)
    return [
        f"{case.decision_point}（run_id={case.run_id}）：录制于 {case.instructions[:12]}，"
        f"当前是 {current[:12]}"
        for case in cases
        if case.instructions != current
    ]


def script_of(cases: Sequence[ReplayCase]) -> dict[str, AdapterOutcome]:
    """把 ``decision`` 类用例装成 ``ScriptedAdapter`` 的脚本。

    同一决策点出现两次即报错：``script`` 是"决策点 → 输出"的映射，后来的会静默盖掉
    先前的，而那正是"以为在回放 A 样本、实际回放的是 B"的来源。
    """
    script: dict[str, AdapterOutcome] = {}
    for case in cases:
        if case.kind != "decision":
            continue
        if case.outcome is None:
            raise ValueError(f"{case.decision_point} 的 decision 用例没有 outcome")
        if case.decision_point in script:
            raise ValueError(
                f"决策点 {case.decision_point} 有不止一条录制：换 run_id 区分样本，"
                "或删掉多余的——script 是按决策点索引的，后写的会静默盖掉先写的"
            )
        script[case.decision_point] = AdapterOutcome.model_validate(dict(case.outcome))
    return script
