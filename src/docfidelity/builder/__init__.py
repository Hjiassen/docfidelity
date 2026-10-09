"""参考构建包：把内核各层接成流水线的五个阶段体（DD-13.1）。

``pipeline`` 提供阶段机、断点与作业编排，但**不内置任何阶段实现**——"谁去解析模板、
谁去注入 OOXML、谁去判定门禁"由调用方通过 ``StageRegistry`` 注册。本包是那个"调用方"
的参考实现，也是随内核分发的 ``docfidelity.build_stages`` 入口点提供者：装上本包，
``server`` 不用改一行就能发起构建（需求 场景 E 第 1 步 / 验收 31）。

为什么它单独成包、且位于 ``pipeline`` 之上
------------------------------------------
"把五层接起来"这件事必然要 import ``ingest`` / ``compose`` / ``render`` /
``registry`` / ``qa``，而内核的分层契约把这条依赖限制得很死（``.importlinter`` 的
三条 seam 契约逐条钉住"其余模块不得认识 ingest / registry / agents"）。把接缝代码
散进既有模块会把那些契约掏空；单独成包则让"组合根在哪"这个问题有一个明确答案——
``.importlinter`` 里为它单列一条契约（可依赖内核各层，不得依赖 ``server``）。

两件事分开看
------------
* :mod:`.stages` 是五个阶段体本身（以及入口点目标 :func:`provide`）；
* :mod:`.inputs` 是它们的**共同方言**：``payload`` 的解析，以及工作区里
  "上游产出放在哪"的那份布局。五个阶段体都从它取件，故路径只有一个定义处。
"""

from __future__ import annotations

from .inputs import (
    FACTS_REL,
    FINDINGS_DIRNAME,
    IR_REL,
    RENDER_REL,
    REPORT_REL,
    TRACES_REL,
    BuildInputs,
    findings_rel,
)
from .stages import (
    INGEST_RULE_ID,
    REFRESH_SKIP_REASON,
    build_registry,
    compose_stage,
    ingest_stage,
    provide,
    qa_stage,
    refresh_stage,
    render_stage,
)

__all__ = [
    "FACTS_REL",
    "FINDINGS_DIRNAME",
    "INGEST_RULE_ID",
    "IR_REL",
    "RENDER_REL",
    "REFRESH_SKIP_REASON",
    "REPORT_REL",
    "TRACES_REL",
    "BuildInputs",
    "build_registry",
    "compose_stage",
    "findings_rel",
    "ingest_stage",
    "provide",
    "qa_stage",
    "refresh_stage",
    "render_stage",
]
