"""构建前置检查（P08 的"为什么不能发起"）与发起构建的留痕。

判据是「**同一份判据**服务两个消费者」：

1. ``POST /projects/{pid}/config/validate`` 据此渲染"只差哪一项"（永远 200，
   结论写在 ``ok`` 里）；
2. ``POST /projects/{pid}/jobs`` 据此拒绝（不合即 ``E_PRECHECK_FAILED``）。

两处各写一份的症状很具体——界面说"可以发起"而提交被拒，或者反过来：按钮被置灰
而服务端其实会放行。故本文件的关键用例不是"检查项都能查出来"（那是一般的正确性），
而是**两条路的结论逐字一致**：把检查端的 ``as_error()`` 与提交端真抛出来的错误
放在一起比 ``code`` 与 ``where``。

第二条纪律是「做不了的检查要显式跳过」：模板库与事实源登记都还没落地，此刻
"模板包存在吗"**答不出来**。答不出来就说答不出来（进 ``skipped``），不能算成
"检查通过"——那会让使用者以为系统确认过，直到构建跑到 ingest 才失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import StageRegistry
from server.precheck import UNAVAILABLE_CHECKS, UNAVAILABLE_WITHOUT_REFS, ReferenceIndex, collect
from tests.fixtures.server_fixture import (
    DEMO_DOC_KEYS,
    ArtifactStage,
    Harness,
    build_harness,
    document_entry,
    project_config,
    seed_demo_assets,
    seed_domain_models,
)

pytestmark = pytest.mark.api


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return build_harness(tmp_path)


def expect_error(code: str, fn: object) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()  # type: ignore[operator]
    assert caught.value.code == code, caught.value.message
    return caught.value


def project_with(harness: Harness, **entry_overrides: object) -> str:
    """建项目 + 写入一份（可带缺陷的）配置。

    先把演示资产登记进库：本文件要验的是**引用是否已声明**与**引用是否已登记**两条
    判据各自的结论，而"库里有什么"必须是个已知量。不登记的话，每一条用了
    ``template_ref=""`` 的用例都会额外多出一条 ``unregistered_references``
    （档案那一条），断言也就变成了在验另一件事。
    """
    seed_demo_assets(harness)
    project = harness.services.create_project(name="前置检查")
    config: ProjectConfig = project_config(project.project_id, **entry_overrides)
    harness.services.put_config(project.project_id, config, author="t")
    return project.project_id


# ------------------------------------------------------------------ 通过
def test_a_complete_config_can_be_started(harness: Harness) -> None:
    """空 ``issues`` 就是"可以发起"，没有第三种状态（``ok`` 是它的派生）。"""
    project_id = harness.new_project()
    report = harness.services.precheck(project_id)

    assert report.ok
    assert report.issues == ()
    assert report.doc_keys == DEMO_DOC_KEYS
    assert report.checked_documents == len(DEMO_DOC_KEYS)
    assert report.missing_stages == ()


@pytest.mark.parametrize(
    ("profile_ref", "deprecated", "expected_ok"),
    [
        ("demo-profile@1.0", False, True),
        ("demo-profile@missing", False, False),
        ("demo-profile", True, False),
        ("demo-profile@1.0", True, True),
    ],
)
def test_precheck_uses_the_build_profile_resolution_policy(
    harness: Harness, profile_ref: str, deprecated: bool, expected_ok: bool
) -> None:
    project_id = project_with(harness, profile_id=profile_ref)
    if deprecated:
        harness.services.deprecate_profile("demo-profile", "1.0", reason="停用旧版")

    report = harness.services.precheck(project_id)
    assert report.ok is expected_ok
    if expected_ok:
        job = harness.services.submit_build(project_id)
        assert job.project_id == project_id
    else:
        assert report.issues[0].where["unregistered"] == [
            {"doc_key": "overview", "field": "profile_id", "ref": profile_ref},
            {"doc_key": "detail", "field": "profile_id", "ref": profile_ref},
        ]
        expect_error(ErrorCode.PRECHECK_FAILED, lambda: harness.services.submit_build(project_id))
        assert harness.services.list_jobs() == []


def test_precheck_never_raises_so_it_can_also_be_read(harness: Harness) -> None:
    """检查**只收集不抛错**：抛错的实现回答不了"只差哪一项"（见模块文档）。"""
    project_id = project_with(harness, template_ref="")
    report = harness.services.precheck(project_id)  # 不抛
    assert report.ok is False
    assert report.as_error().code == ErrorCode.PRECHECK_FAILED


# ------------------------------------------------------------------ 缺陷
def test_a_blank_reference_counts_as_missing(harness: Harness) -> None:
    """判空口径是 ``if not x``：空串与"没填"是同一种缺陷。

    契约里 ``template_ref`` / ``profile_id`` 都是**必填 str**，界面上"没选"落下来
    就是一个空串。写成 ``== ""`` 或 ``is None`` 都会漏掉另一半，而漏掉的表现是
    "提交被拒，但检查说没问题"。
    """
    project_id = project_with(harness, template_ref="", profile_id="")
    report = harness.services.precheck(project_id)

    assert report.issues[0].kind == "missing_references"
    assert report.issues[0].where["problems"] == [
        {"doc_key": "overview", "field": "template_ref"},
        {"doc_key": "overview", "field": "profile_id"},
        {"doc_key": "detail", "field": "template_ref"},
        {"doc_key": "detail", "field": "profile_id"},
    ]


def test_selection_narrows_the_check_to_the_chosen_documents(harness: Harness) -> None:
    """只查选中的文档：检查一份没选的文档会把"能发起"误判成"不能发起"。"""
    project_id = project_with(harness, template_ref="")
    report = harness.services.precheck(project_id, doc_keys=["overview"])

    assert report.doc_keys == ("overview",)
    assert report.checked_documents == 1
    assert report.issues[0].where["problems"] == [{"doc_key": "overview", "field": "template_ref"}]


def test_an_unknown_doc_key_is_reported_by_name(harness: Harness) -> None:
    """``unknown_doc_keys`` 与 ``missing_references`` 是不同的事：一个是键写错了，
    一个是键对而引用没配。混成一条会让"改拼写"和"去选模板"变成同一条提示。"""
    project_id = harness.new_project()
    report = harness.services.precheck(project_id, doc_keys=["overview", "没有这份"])

    assert [issue.kind for issue in report.issues] == ["unknown_doc_keys"]
    assert report.issues[0].where["missing"] == ["没有这份"]
    # 选中且存在的那一份照常计入（"查了哪些"要如实给出来）
    assert report.doc_keys == ("overview",)


def test_missing_document_and_unknown_key_both_surface(harness: Harness) -> None:
    """先报"哪些键不存在"，再报"存在的那份缺什么"——顺序即 ``as_error`` 的主提示。"""
    project_id = project_with(harness, template_ref="")
    report = harness.services.precheck(project_id, doc_keys=["overview", "没有这份"])

    assert [issue.kind for issue in report.issues] == [
        "unknown_doc_keys",
        "missing_references",
    ]
    error = report.as_error()
    assert error.message.startswith("配置里没有启用的文档")
    assert "另有 1 项" in error.message


def test_a_project_with_no_enabled_document_is_reported_as_such(
    harness: Harness,
) -> None:
    """``no_documents`` 单独一类：它和"缺引用"的可操作动作完全不同
    （去配置里启用文档 vs 去选模板包）。"""
    project_id = project_with(harness, enabled=False)
    report = harness.services.precheck(project_id)

    assert [issue.kind for issue in report.issues] == ["no_documents"]
    assert report.checked_documents == 0
    assert report.doc_keys == ()


def test_missing_stages_are_reported_before_the_build_starts(tmp_path: Path) -> None:
    """缺阶段要在**发起前**拦下：否则作业以 ``failed`` 收场，而界面会把它读成
    "这份文档没构建成功"——真正的原因是这台服务器没装阶段。"""
    harness = build_harness(
        tmp_path, stages=StageRegistry({"ingest": ArtifactStage(name="ingest")})
    )
    project_id = harness.new_project()
    report = harness.services.precheck(project_id)

    assert [issue.kind for issue in report.issues] == ["missing_stages"]
    assert report.missing_stages == ("compose", "render", "refresh", "qa")
    assert report.registered_stages == ("ingest",)
    assert report.issues[0].where["missing_stages"] == [
        "compose",
        "render",
        "refresh",
        "qa",
    ]


# ------------------------------------------------- 两条路共用一份判据
def test_the_check_and_the_submit_report_the_same_error(harness: Harness) -> None:
    """**本文件的核心断言**：检查端 ``as_error()`` 与提交端真抛的错逐字一致。

    只断言"两边都失败了"是不够的——``code`` 或 ``where`` 漂了的话，界面按检查端
    渲染出的提示就会与日志里的失败对不上，而使用者手里只有界面。
    """
    project_id = project_with(harness, template_ref="")
    checked = harness.services.precheck(project_id).as_error()
    submitted = expect_error(
        ErrorCode.PRECHECK_FAILED, lambda: harness.services.submit_build(project_id)
    )

    assert submitted.code == checked.code
    assert submitted.message == checked.message
    assert dict(submitted.where) == dict(checked.where)
    assert submitted.where["problems"] == [
        {"doc_key": "overview", "field": "template_ref"},
        {"doc_key": "detail", "field": "template_ref"},
    ]


def test_submitting_with_an_unknown_key_reports_it_the_same_way(harness: Harness) -> None:
    """``doc_keys`` 这条参数也走同一份判据（提交端不能另有一套校验）。"""
    project_id = harness.new_project()
    checked = harness.services.precheck(project_id, doc_keys=["没有这份"]).as_error()
    submitted = expect_error(
        ErrorCode.PRECHECK_FAILED,
        lambda: harness.services.submit_build(project_id, doc_keys=["没有这份"]),
    )
    assert dict(submitted.where) == dict(checked.where)


def test_a_failed_precheck_leaves_no_job_behind(harness: Harness) -> None:
    project_id = project_with(harness, template_ref="")
    expect_error(ErrorCode.PRECHECK_FAILED, lambda: harness.services.submit_build(project_id))
    assert harness.services.list_jobs() == []


# --------------------------------------------------------- 引用的东西在不在库
def test_a_reference_that_is_not_in_the_library_is_reported_by_name(harness: Harness) -> None:
    """**填了、库里没有**与**没填**是两条判据，报出来的字段也就不同。

    ``missing_references`` 只说"这一项没填"，``unregistered_references`` 必须带上
    ``ref``——使用者拿着一个 ``template_ref`` 才知道要去库里补哪一版。少给 ``ref``
    的后果是界面只能说"你有个模板不在库里"，而他手上可能有五六份文档。
    """
    project_id = project_with(harness, template_ref="demo-package@9")  # 版本不存在
    report = harness.services.precheck(project_id)

    assert [issue.kind for issue in report.issues] == ["unregistered_references"]
    assert report.issues[0].where["unregistered"] == [
        {"doc_key": "overview", "field": "template_ref", "ref": "demo-package@9"},
        {"doc_key": "detail", "field": "template_ref", "ref": "demo-package@9"},
    ]
    # 补救动作只列**出现过**的字段：这一屏不该冒出"去登记档案"
    assert all("档案" not in item for item in report.issues[0].remediation)


def test_both_reference_problems_keep_their_own_payload(harness: Harness) -> None:
    """两条问题同时在时，``as_error`` 的 ``where`` 里两段都要在。

    这是 ``unregistered`` 另开一个键、而不是复用 ``problems`` 的**原因**：
    ``as_error`` 合并各问题的 ``where`` 走的是 ``dict.update``，同键会让先出现的那一份
    被后一份**盖掉**（而报出来的还是"另有 1 项"，看上去什么都没丢）。
    """
    project_id = project_with(harness, template_ref="demo-package@9", profile_id="")
    error = harness.services.precheck(project_id).as_error()

    assert error.where["problems"] == [
        {"doc_key": "overview", "field": "profile_id"},
        {"doc_key": "detail", "field": "profile_id"},
    ]
    assert error.where["unregistered"] == [
        {"doc_key": "overview", "field": "template_ref", "ref": "demo-package@9"},
        {"doc_key": "detail", "field": "template_ref", "ref": "demo-package@9"},
    ]


def test_a_real_import_makes_the_same_config_startable(
    harness: Harness, template_bytes: bytes
) -> None:
    """**端到端**：真走模板入库流水线 + 档案登记之后，检查必须由"拒绝"变成"通过"。

    为什么非要有这一条
    ------------------
    ``seed_demo_assets`` 为了让别的用例跑得快，是**直接往仓储里写登记**的。那个捷径
    有个风险：检查读的键（``RegisteredTemplate.ref`` / ``RegisteredProfile.profile_id``）
    与真实导入写下的键若漂了，全套用例照样全绿——因为两边用的都是夹具伪造的那份。
    这条用例把真实链路接回来：导入 → 登记 → 检查。两边一旦对不上，这里立刻红。
    """
    from docfidelity.contracts.profile import Bindings
    from server.services import DEMO_PROFILE_ID, DEMO_TEMPLATE_REF
    from tests.fixtures.domain_fixture import api_profile

    fresh = build_harness(harness.root / "real-import")
    source = fresh.root / "uploaded.docx"
    source.write_bytes(template_bytes)
    record = fresh.services.import_template(
        filename=source.name,
        payload=source.read_bytes(),
        template_id="demo-package",
        version="1",
    )
    assert record.package_dir is not None, "解析失败的话下面那条断言就验不到真东西了"
    fresh.services.register_template(record.import_id, template_id="demo-package", version="1")
    # 领域类型也要走真链路进库：档案的 for_each 在**登记期**就按 FR-M2-23 校验，
    # 空库会被 E_DOMAIN_KIND_UNKNOWN 拦住——那条失败与"引用是否已登记"无关。
    seed_domain_models(fresh)
    fresh.services.register_profile(
        # 身份与绑定改成演示配置里写的值，否则登记时会被 E_PROFILE_TEMPLATE_MISSING 拒掉
        # ——而那正是这条检查该有的样子（`serve_dev.py` 的 `_seed_demo_assets` 同处理）。
        document=api_profile().model_copy(
            update={
                "profile_id": DEMO_PROFILE_ID,
                "doc_type": DEMO_PROFILE_ID,
                "bindings": Bindings(template_packages=[DEMO_TEMPLATE_REF]),
            }
        ),
        known_template_refs=[DEMO_TEMPLATE_REF],
        domain_models=fresh.services.domain_declarations(),
    )

    project_id = fresh.services.create_project(name="真实入库").project_id
    fresh.services.demo_init(project_id)

    report = fresh.services.precheck(project_id)
    assert report.ok, f"真实登记之后这份配置应当可以发起：{[i.kind for i in report.issues]}"


# ------------------------------------------------------- 做不了的检查
def test_checks_that_cannot_run_are_listed_rather_than_passed(harness: Harness) -> None:
    """**答不出来要说答不出来**：现在只剩"事实源非空"这一条。

    "模板包存在吗"从前答不出来（登记未落地）故进过 ``skipped``；登记落地之后它改成了
    真查（见上面那组用例），于是它必须从 ``skipped`` 里**消失**——留在那里会让使用者
    以为系统还没确认过，而实际上已经查了。

    剩下这条是真的答不出来：它不是查引用，是"这份来源到底产出了几条事实"，要真跑一次
    接入（``ingest`` 的 I/O）才知道。把它算成"检查通过"是最坏的一种：使用者以为系统
    已经确认过，直到构建跑到 ingest 才失败（与门禁"缺能力跳过必须进 summary"同一口径）。
    """
    project_id = harness.new_project()
    report = harness.services.precheck(project_id)

    assert report.skipped == UNAVAILABLE_CHECKS
    assert report.skipped, "跳过项不能是空的：那等于宣称所有检查都做了"
    assert any("事实源" in item for item in report.skipped)
    assert not any("模板包" in item for item in report.skipped), "模板包检查已落地，不该再跳过"
    # 跳过**不影响**结论：它是"没查"，不是"查出来有问题"
    assert report.ok


def test_not_being_able_to_check_is_different_from_checking_nothing() -> None:
    """``refs=None``（这次没能力查）与 ``refs=空集``（查了，一个都没登记）必须分得开。

    混同之后的表现很具体：**预检说可以发起**，构建跑到取模板包时才报缺——
    而"没能力查"本该被如实标成跳过（硬规则 11）。
    """
    entries = [document_entry("overview")]

    without = collect(
        project_id="p",
        entries=entries,
        selected=None,
        registered_stages=("ingest",),
        missing_stages=(),
        refs=None,
    )
    assert tuple(without.skipped) == UNAVAILABLE_CHECKS + UNAVAILABLE_WITHOUT_REFS
    assert without.ok, "没能力查不该被算成查出问题"

    empty = collect(
        project_id="p",
        entries=entries,
        selected=None,
        registered_stages=("ingest",),
        missing_stages=(),
        refs=ReferenceIndex(),
    )
    assert empty.skipped == UNAVAILABLE_CHECKS
    assert not empty.ok, "空集是一个结论（一个都没登记），不是'没查'"
    assert empty.issues[0].kind == "unregistered_references"


def test_a_project_without_a_config_is_a_config_problem_not_a_not_found(
    harness: Harness,
) -> None:
    """项目在、配置不在 → ``E_CONFIG_INVALID``；项目不在 → ``E_NOT_FOUND``。
    两者的可操作动作不同（去初始化配置 vs 改标识），不能合并成一个码。"""
    project = harness.services.create_project(name="空配置")
    expect_error(ErrorCode.CONFIG_INVALID, lambda: harness.services.precheck(project.project_id))
    expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.precheck("p_ghost"))


# --------------------------------------------------------- 发起人留痕
def test_the_author_lands_on_every_job_but_not_on_the_inputs_hash(
    harness: Harness,
) -> None:
    """``author`` 只落成留痕（``Job.initiated_by``），**不进** ``inputs_hash``。

    进了摘要就有两个后果：一是"谁点的按钮"变了让断点失效（同一份输入因此不可复现），
    二是同一份配置由甲发起、乙发起会算出两个不同的摘要——而它们在内容上完全一样。
    故这里同时断言"留痕在"与"摘要不变"。
    """
    project_id = harness.new_project()
    anonymous = harness.services.submit_build(project_id, doc_keys=["overview"])
    signed = harness.services.submit_build(project_id, doc_keys=["overview"], author="森")

    assert anonymous.initiated_by is None
    assert signed.initiated_by == "森"
    assert signed.inputs_hash == anonymous.inputs_hash, "署名不该改变输入摘要"

    child = harness.services.job_children(signed.job_id)[0]
    assert child.initiated_by == "森", "子作业也要带发起人（详情页按子作业渲染）"


def test_the_request_body_does_not_require_an_author() -> None:
    """``author`` 可省：强制署名会让"我就自己跑一遍"变成一次填表。"""
    from server.api.schemas import SubmitBuild

    assert SubmitBuild().author is None
    assert SubmitBuild(author="森").author == "森"
