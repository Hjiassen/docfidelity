"""决策接缝（DD-12.3 / DD-12.4 落到编排层）—— ``agents`` 与 ``pipeline`` 之间唯一的耦合点。

为什么接缝在编排层
------------------
调用方向是"编排调 Agent"（``agents`` 不 import ``pipeline``，见 ``.importlinter`` 的
``kernel-layers``）。所以"谁来提问、用哪个适配层、留痕落到哪"必须在编排侧回答。
把这三件事散进五个阶段体，会得到三个具体后果：① 每个阶段各自 new 一个适配层，
模型连接数与阶段数成正比；② 留痕各写各的，报告与裁决队列的口径从此不一致；
③ "关掉模型"这件事要在五个地方各改一次，漏一处就是一次真实的模型调用。

本模块只做胶合，不做判定
------------------------
"谁该被问、问不出来怎么办"全部在 :mod:`docfidelity.agents.routing`（唯一实现）。
这里只负责把编排层已有的东西（作业 id、事件日志、项目配置）接到那条链路上，
再把结果收成一个便于查询的对象。故本模块里**没有**任何 ``if confidence < ...``
之类的分支——那类判定出现第二份实现时，症状是"报告说进了裁决队列、作业状态却显示成功"。

留痕为什么落在事件日志上
------------------------
``contracts.job.EventType`` 里的 ``decision`` 一直是个保留值（DD-11.6 列了它，
但直到本模块出现才有人写）。把决策落成事件而不是另开一张表，换来三件已经存在的事：
SSE 流里直接看得见（P09 的事件流据此显示"这一步是模型决定的"）、断线重连能按
``Last-Event-ID`` 补齐、进程重启后靠 :func:`~docfidelity.pipeline.read_events` 重建。
为决策另造一套留痕通道，就等于把这三条各实现第二遍。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from math import isfinite
from pathlib import Path
from typing import Any, Final

from docfidelity.agents import (
    BASELINE_FILENAME,
    DEFAULT_MAX_RETRIES,
    BaselineStatus,
    DecisionContext,
    DecisionOutcome,
    DecisionRecorder,
    GrammarCapability,
    MemoryRecorder,
    ModelAdapter,
    NullAdapter,
    OpenAIAgentsAdapter,
    RoutingTable,
    build_routing,
    decision_point,
    dispatch,
)
from docfidelity.agents import load_status as _load_baseline_status
from docfidelity.agents import probe_grammar as _probe_grammar
from docfidelity.agents.baseline import baseline_digests, default_cases, endpoint_digest
from docfidelity.agents.catalog import MODEL_ROUTED
from docfidelity.contracts.config import RoutingConfig
from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.job import Decision, EventType, Job, JobEvent, ModelCall

from .checkpoint import path_segment
from .events import EventLog

__all__ = [
    "BASELINE_FILENAME",
    "DECISION_EVENT_TYPE",
    "RECORD_DECISION",
    "RECORD_KIND_FIELD",
    "RECORD_MODEL_CALL",
    # 下列几个是**决策层的词汇**在编排层的出口：`ModelAdapter` / `DecisionRecorder`
    # 出现在本模块的签名里，`DecisionOutcome` 出现在 `StageContext.decide` 的返回值上。
    # 在这里转出一次（而不是让每个编排模块各 import 一次 `agents`），是为了让
    # "与决策层的耦合只有本模块"成为 `.importlinter` 里一条可执行契约，
    # 而不是一句写在文档里的自我约束。
    "BaselineStatus",
    "DecisionOutcome",
    "DecisionRecorder",
    "DecisionSession",
    "GrammarCapability",
    "JournalRecorder",
    "ModelAdapter",
    "build_session",
    "decisions_from_events",
    "load_baseline_status",
    "model_session_factory",
    "pending_decisions_from_events",
    "probe_grammar",
    "rule_only_session",
]

#: 决策事件的持久化类型。**本模块是它的唯一写入点**（DD-11.6 的 ``event: decision``）。
DECISION_EVENT_TYPE: Final[EventType] = "decision"

#: 事件载荷里区分"记录种类"的字段名。
RECORD_KIND_FIELD: Final = "record"

#: 事件载荷里的记录种类判别符。
#:
#: 为什么不给模型调用单开一个 ``EventType``：那是契约闭集，扩张它会让前端与回放
#: 逻辑各多一个待处理分支；而"一次决策"与"支撑它的一次模型调用"本来就是同一条链上
#: 的两段，共用一个类型、靠这个字段区分，界面按同一列渲染即可。
RECORD_DECISION: Final = "decision"
RECORD_MODEL_CALL: Final = "model_call"
RECORD_STAGE_START: Final = "stage_start"


class JournalRecorder:
    """把决策与模型调用落成**作业事件**的留痕（实现 ``DecisionRecorder``）。

    查询面（``decisions`` / ``pending_decisions`` / ``silent_adoptions`` …）**委托**给
    内部那份 :class:`~docfidelity.agents.MemoryRecorder`：这些口径各有唯一实现，
    本类只补"再写一条事件"。两处各写一份的后果是"裁决队列按事件算、报告按内存算"，
    同一份构建给出两个数字，而两个都像是真的。

    Parameters
    ----------
    events:
        落点。服务端传的是那份按 ``job_id`` 路由到 ``events.jsonl`` 的日志，
        故决策与阶段事件共享同一个单调序列。
    job_id:
        事件分桶键，也是留痕的归属。这里就校验路径合法性：等到写盘那一刻才报错时，
        "决策已经发生"这件事已无法回退。
    mirror:
        额外的持久化接缝（下一轮的落库记录器实现 ``DecisionRecorder`` 即可挂上）。
        镜像**不承担查询**——服务端需要在本进程内立刻回答"这一份有没有待裁决项"
        （它决定作业状态与编排聚合），那是数据库往返替代不了的。
    """

    def __init__(
        self,
        events: EventLog,
        *,
        job_id: str,
        inner: MemoryRecorder | None = None,
        mirror: DecisionRecorder | None = None,
    ) -> None:
        self._events = events
        self._job_id = path_segment(job_id, field="job_id")
        self._inner = (
            inner
            if inner is not None
            else _recorder_from_events(events.events(job_id), job_id=job_id)
        )
        self._mirror = mirror
        self._stage: str | None = None

    def begin_stage(self, stage: str) -> None:
        """实际重跑时作废该阶段旧待办，保留完整审计历史。"""
        self._stage = stage
        self._events.emit(
            self._job_id,
            DECISION_EVENT_TYPE,
            {RECORD_KIND_FIELD: RECORD_STAGE_START, "stage": stage},
        )

    @property
    def job_id(self) -> str:
        return self._job_id

    # -------------------------------------------------------------- 写入
    def record_decision(self, decision: Decision) -> None:
        """记一条决策：内存 → 镜像 → 事件。顺序见 :meth:`_emit`。"""
        self._inner.record_decision(decision)
        if self._mirror is not None:
            self._mirror.record_decision(decision)
        self._emit(RECORD_DECISION, decision)

    def record_model_call(self, call: ModelCall) -> None:
        """记一次模型调用（含失败与降级的那几次）。"""
        self._inner.record_model_call(call)
        if self._mirror is not None:
            self._mirror.record_model_call(call)
        self._emit(RECORD_MODEL_CALL, call)

    def _emit(self, record: str, payload: Decision | ModelCall) -> None:
        """发一条事件（DD-13.4：**先落库再推送**）。

        事件排在三步的最后，前面的内存与镜像都已完成——:meth:`EventLog.emit` 自己也
        先把事件写进 JSONL 再返回给调用方去推送，故"推送时下游一定能查到"这条对
        内存、镜像、事件文件三者同时成立。
        """
        self._events.emit(
            self._job_id,
            DECISION_EVENT_TYPE,
            {
                RECORD_KIND_FIELD: record,
                **({"stage": self._stage} if self._stage is not None else {}),
                **payload.model_dump(mode="json"),
            },
        )

    # -------------------------------------------------------------- 查询
    def decisions(self) -> tuple[Decision, ...]:
        return tuple(self._inner.decisions)

    def model_calls(self) -> tuple[ModelCall, ...]:
        return tuple(self._inner.model_calls)

    def pending_decisions(self) -> tuple[Decision, ...]:
        """进裁决队列的项（低置信 / 兜底 / 需人工）。NFR-26 的下游就是它。"""
        active = _active_decision_ids(self._events.events(self._job_id))
        return tuple(item for item in self._inner.pending_decisions() if item.decision_id in active)

    def silent_adoptions(self) -> tuple[Decision, ...]:
        """低置信却被自动采用的项，**应恒为空**（验收 39）。"""
        return self._inner.silent_adoptions()

    def token_totals(self) -> tuple[int, int]:
        """``(输入, 输出)`` token 合计；确定性模式下必须为 ``(0, 0)``（NFR-18）。"""
        return self._inner.token_totals()

    def describe(self) -> dict[str, Any]:
        description = self._inner.describe()
        description["pending"] = len(self.pending_decisions())
        return description


class DecisionSession:
    """一个作业的决策会话：路由表 + 适配层 + 留痕，绑在一起。

    按**作业**而不是按阶段建会话，有两个具体理由：留痕的 ``job_id`` 只能有一个答案；
    决策序号（:attr:`DecisionContext.sequence`）要是跨阶段的——否则"这份文档的第三次
    落位决策"与"另一个阶段的第一次"会得到同一个标识，留痕上分不出谁是谁。

    本类刻意不做成 ``dataclass``：它持有一个**可变**的
    :class:`~docfidelity.agents.DecisionContext`（序号在其中自增），
    而 dataclass 的 ``eq`` / ``repr`` 会把那个内部状态当成会话的身份。
    """

    def __init__(
        self,
        *,
        job_id: str,
        table: RoutingTable,
        recorder: JournalRecorder,
        adapter: ModelAdapter,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.job_id = job_id
        self.table = table
        self.recorder = recorder
        self.adapter = adapter
        self._context = DecisionContext(
            table=table,
            recorder=recorder,
            adapter=adapter,
            job_id=job_id,
            max_retries=max_retries,
            sequence=len(recorder.decisions()),
        )

    # -------------------------------------------------------------- 决策
    def begin_stage(self, stage: str) -> None:
        """阶段真正执行前更新有效决策作用域；复用断点时不调用。"""
        self.recorder.begin_stage(stage)

    def decide(
        self,
        decision_point_id: str,
        inputs: Mapping[str, Any] | None = None,
        *,
        target: str = "",
        candidates: Sequence[str] = (),
        context: Mapping[str, Any] | None = None,
    ) -> DecisionOutcome:
        """做一次决策（``agents.dispatch`` 在本层的样子）。

        先查目录再转发：未登记的决策点应当报 ``E_CONFIG_INVALID``（"这个点不存在，
        先归类登记"），而不是路由表那句"缺少该点的解析结果"——后者读起来像引擎缺陷，
        会把排查方向带到 :func:`build_session` 上去。
        """
        decision_point(decision_point_id)
        return dispatch(
            decision_point_id,
            inputs if inputs is not None else {},
            self._context,
            target=target,
            candidates=candidates,
            context=context,
        )

    # -------------------------------------------------------------- 只读面
    def decisions(self) -> tuple[Decision, ...]:
        return self.recorder.decisions()

    def model_calls(self) -> tuple[ModelCall, ...]:
        return self.recorder.model_calls()

    def pending_decisions(self) -> tuple[Decision, ...]:
        return self.recorder.pending_decisions()

    @property
    def has_pending_decision(self) -> bool:
        """是否存在待裁决项。作业状态与编排聚合都据此判断（NFR-26）。"""
        return bool(self.recorder.pending_decisions())

    def silent_adoptions(self) -> tuple[Decision, ...]:
        return self.recorder.silent_adoptions()

    def routing(self) -> list[dict[str, Any]]:
        """逐决策点的声明归属与实际路径（验收 41「决策点归属可查」的数据面）。"""
        return self.table.describe()

    def adapter_description(self) -> dict[str, Any]:
        """适配层自述（kind / available / 受限解码探测结果）。密钥只进不出（NFR-21）。"""
        return self.adapter.describe()

    def summary(self) -> dict[str, Any]:
        """可序列化汇总：留痕口径 + 适配层状态，报告与界面共用同一份。"""
        record = self.recorder.describe()
        record["adapter"] = self.adapter.describe()
        return record


def build_session(
    config: RoutingConfig | None = None,
    *,
    job_id: str,
    events: EventLog | None = None,
    adapter: ModelAdapter | None = None,
    model_enabled: bool | None = None,
    baseline: Mapping[str, float] | None = None,
    mirror: DecisionRecorder | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> DecisionSession:
    """建一个作业级决策会话。

    ``adapter`` 缺省是 :class:`~docfidelity.agents.NullAdapter`（纯确定性）。这不是
    "忘了配模型"的占位，而是一个**正式实现**：关闭模型时全链路走规则、产出仍完整
    （验收 39）。把确定性做成缺省值的方向是刻意的——"接模型"是显式动作，忘了接的
    后果是"做得少"，而不是"静默按模型的猜测改了文档"。

    ``model_enabled`` 缺省随 ``config.model_enabled``。调用方若挂的是空适配层，
    应当**显式**传 ``False``：只关总开关而留着一个可用适配层（或反之）会让路由表的
    ``reason`` 与 ``describe()`` 各说一套，排查时先怀疑配置读错了。
    """
    return DecisionSession(
        job_id=job_id,
        table=build_routing(config, model_enabled=model_enabled, baseline=baseline),
        recorder=JournalRecorder(
            events if events is not None else EventLog(),
            job_id=job_id,
            mirror=mirror,
        ),
        adapter=adapter if adapter is not None else NullAdapter(),
        max_retries=max_retries,
    )


def model_session_factory(
    *,
    model: str,
    base_url: str,
    api_key: str | None = None,
    baseline: Mapping[str, float] | None = None,
    baseline_path: str | Path | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Callable[[Job, RoutingConfig | None, EventLog], DecisionSession]:
    """把"接一个真模型（OpenAI 兼容端点）"收成一个可注入的作业级会话工厂。

    为什么在这里而不是在服务端的组合根
    ----------------------------------
    ``server`` 不得 import ``agents``（``.importlinter`` 的 ``server-has-no-ooxml``），
    而适配层是 ``agents`` 的东西。本模块既然是"与决策层的耦合只有这里"的那条接缝
    （DD-12.3），**"怎么把一个端点折成适配层"就也在这里回答一次**；服务端的组合根
    只做一件事：把 ``settings.model_*`` 读出来传进来（`server.api.deps.build_state`）。

    为什么一个适配层给所有作业共用
    ------------------------------
    SDK 客户端因此只建一次，模型连接数不随作业数增长——若每个作业各 new 一个适配层，
    连接数就与作业数成正比，那正是"接缝放在编排层"要避免的第一个后果（见模块文档）。
    适配层本身不持作业状态（`OpenAIAgentsAdapter` 只缓存 SDK 与模型对象），共用是安全的。

    工厂存在表示服务已配置模型，但仍遵守项目路由的关闭开关。项目关闭时直接走规则，
    不做模型准入；服务未接模型时使用 ``build_session`` 的确定性会话。

    ``baseline`` 与 ``baseline_path`` 是同一件事的两种给法（NFR-23 的能力门槛）：
    前者是现成的一张"逐点通过率"表（测试与嵌入方用），后者是**评测报告文件路径**——
    服务端给的就是后者，因为它只认识存储根、不认识 ``agents`` 的数据结构。

    为什么按路径给时是**每次作业现读**而不是构造工厂时读一次
    --------------------------------------------------------
    评测是**运维动作**：跑完 `scripts/run_baseline.py` 之后，下一次构建就该按新数据
    路由，而不是"重启服务才生效"。构造期读一次会让"我刚跑完评测，界面却没变"成为
    一个需要靠重启才能解释的现象；而现读的代价只是一次小 JSON 读取（每作业一次）。
    生产准入要求报告匹配当前模型、内置样本集与路由姿态。报告缺失、失效或未覆盖的点
    回退规则并标待确认，不让构建失败。显式基线映射由嵌入方负责验证身份，未覆盖点同样回退。
    """
    adapter = OpenAIAgentsAdapter(model=model, base_url=base_url, api_key=api_key)

    def factory(job: Job, routing: RoutingConfig | None, events: EventLog) -> DecisionSession:
        if routing is not None and not routing.model_enabled:
            return build_session(
                routing,
                job_id=job.job_id,
                events=events,
                adapter=adapter,
                model_enabled=False,
                max_retries=max_retries,
            )
        rates, rejected = _resolve_baseline(
            baseline,
            baseline_path,
            model=model,
            routing=routing,
            endpoint_fingerprint=endpoint_digest(base_url),
        )
        session = build_session(
            routing,
            job_id=job.job_id,
            events=events,
            adapter=adapter,
            model_enabled=True,
            baseline=rates,
            max_retries=max_retries,
        )
        for point_id, reason in rejected.items():
            route = session.table.route(point_id)
            route.baseline_pending = True
            route.reason = reason
        return session

    return factory


def _resolve_baseline(
    fixed: Mapping[str, float] | None,
    path: str | Path | None,
    *,
    model: str,
    routing: RoutingConfig | None,
    endpoint_fingerprint: str,
) -> tuple[dict[str, float], dict[str, str]]:
    """验证生产基线身份，并把未验证的模型点交回规则与裁决队列。"""
    routes = build_routing(routing, model_enabled=True).describe()
    problem = ""
    uncovered: set[str] = set()
    if fixed is not None:
        provided = fixed
    else:
        status = load_baseline_status(path) if path is not None else BaselineStatus()
        provided = status.by_point
        uncovered = set(status.uncovered)
        if not status.loaded:
            problem = status.problem or "未提供基线评测报告"
        else:
            try:
                cases_digest, routing_digest = baseline_digests(default_cases(), routes)
            except EngineError as exc:
                problem = f"无法验证当前评测样本：{exc.message}"
            else:
                if status.model != model:
                    problem = "基线报告的模型与当前模型不一致"
                elif status.endpoint_digest != endpoint_fingerprint:
                    problem = "基线报告的端点指纹缺失或与当前部署不一致"
                elif status.cases_digest != cases_digest:
                    problem = "基线报告的样本指纹与当前样本不一致"
                elif status.routing_digest != routing_digest:
                    problem = "基线报告的路由姿态指纹与当前配置不一致"

    rates: dict[str, float] = {}
    rejected: dict[str, str] = {}
    for record in routes:
        if record["declared"] not in MODEL_ROUTED:
            continue
        point_id = str(record["id"])
        rate = provided.get(point_id)
        reason = problem
        if not reason and (rate is None or point_id in uncovered):
            reason = "基线报告未覆盖该决策点"
        if not reason and rate is not None and (not isfinite(rate) or not 0 <= rate <= 1):
            reason = "基线通过率不合法"
        rates[point_id] = 0.0 if reason else float(rate if rate is not None else 0.0)
        if not reason and rates[point_id] < record["baseline_threshold"]:
            reason = f"基线通过率 {rates[point_id]:.2f} 低于阈值 {record['baseline_threshold']:.2f}"
        if reason:
            rejected[point_id] = f"{reason}，按 NFR-23 回退规则并标记待确认"
    return rates, rejected


def load_baseline_status(path: str | Path) -> BaselineStatus:
    """读基线评测报告的加载状态。**永不抛**（三态见 `agents.baseline.load_status`）。

    服务端（设置页）与构建接线共用这一个入口：两处各判一次"报告在不在、读得动吗"，
    得到的会是两句不一样的措辞，而界面与构建对同一份资产的判断不一致时，
    最像的解释是"评测没生效"。
    """
    return _load_baseline_status(path)


def probe_grammar() -> GrammarCapability:
    """探测本机的受限解码后端（DD-12.4 / 风险清单第 5 条）。

    探测在**内核**侧完成（那里才知道哪些后端算数，`GRAMMAR_BACKENDS`），
    服务端只负责把它显示出来——设置页要回答的是"这台机器上会用原生受限解码吗"，
    而那个答案不该由 Web 层去 import 一个可选依赖来猜。
    """
    return _probe_grammar()


def rule_only_session(job_id: str, *, events: EventLog | None = None) -> DecisionSession:
    """确定性会话：总开关关闭 + 空适配层，全链路走规则默认值。

    两件事**同时**成立才自洽。这是"没接模型时的默认会话"，也是单独调
    :func:`~docfidelity.pipeline.run_stage` 时编排替你绑上的那个——一次单阶段重跑
    不该因为"这个进程没配模型"而在某次决策上拿到不确定的结果。
    """
    return build_session(
        None,
        job_id=job_id,
        events=events,
        adapter=NullAdapter(),
        model_enabled=False,
    )


def _recorder_from_events(events: Sequence[JobEvent], *, job_id: str) -> MemoryRecorder:
    """把事件回流成留痕对象。

    为什么不"就地过滤一遍":待裁决的判据（``status == "pending"``）与"应恒为空的静默采用"
    各有唯一实现，都在 :mod:`docfidelity.agents.recorder`。在这里按载荷字段再判一次，
    就是同一份口径的第二处实现——而它的症状很典型："裁决队列按事件算、报告按内存算"，
    同一份构建两个数字，且两个都像是真的。
    """
    recorder = MemoryRecorder(job_id=job_id)
    for event in events:
        if event.type != DECISION_EVENT_TYPE:
            continue
        payload = {
            key: value
            for key, value in event.payload.items()
            if key not in {RECORD_KIND_FIELD, "stage"}
        }
        kind = event.payload.get(RECORD_KIND_FIELD)
        if kind == RECORD_DECISION:
            recorder.record_decision(Decision.load(payload))
        elif kind == RECORD_MODEL_CALL:
            recorder.record_model_call(ModelCall.load(payload))
    return recorder


def _active_decision_ids(events: Sequence[JobEvent]) -> set[str]:
    """重放阶段作用域；旧事件可推断阶段，无法推断的记录保守保留。"""
    scopes: dict[str | None, set[str]] = {}
    inferred: str | None = None
    for event in events:
        if event.type == "stage" and event.payload.get("status") == "running":
            inferred = event.payload.get("stage")
        if event.type != DECISION_EVENT_TYPE:
            continue
        kind = event.payload.get(RECORD_KIND_FIELD)
        scope = event.payload.get("stage", inferred)
        if kind == RECORD_STAGE_START:
            scopes[scope] = set()
            inferred = scope
        elif kind == RECORD_DECISION:
            scopes.setdefault(scope, set()).add(event.payload["decision_id"])
    return set().union(*scopes.values())


def decisions_from_events(events: Sequence[JobEvent], *, job_id: str = "") -> tuple[Decision, ...]:
    """从作业事件里还原全部决策留痕（按发生次序）。"""
    return tuple(_recorder_from_events(events, job_id=job_id).decisions)


def pending_decisions_from_events(
    events: Sequence[JobEvent], *, job_id: str = ""
) -> tuple[Decision, ...]:
    """从作业事件里还原**待裁决项**——裁决待办中心的数据来源。

    读事件而不是读某次运行的内存对象：内存那份只覆盖**本进程**见过的决策，
    而"这份作业还有哪些项在等人裁决"必须跨重启成立。事件是决策的持久形态
    （``JournalRecorder`` 的写入端），读它才是权威。
    """
    active = _active_decision_ids(events)
    return tuple(
        item
        for item in _recorder_from_events(events, job_id=job_id).pending_decisions()
        if item.decision_id in active
    )
