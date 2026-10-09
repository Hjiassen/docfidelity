"""一键体验：容器形态真的能跑完一次构建（DD-18.1 / 验收 31 / TASK-P0-20）。

本文件为什么必须存在
--------------------
``deploy/README.md`` 第 5 节写着「（``tests/integration/test_one_click.py`` 把这条串成了
可执行用例）」。那个名字被写进 README 时它还不存在——而本仓库对这种事有明确纪律
（``tests/unit/test_demo_assets.py`` 就是为"防虚假声明"而设的：文档里说的每一句都得有
一个当场会红的判据）。故这里把它补上。

三条判据各自对应一句会被读到的话
--------------------------------
1. **模板库不是空的**（``deploy/README.md`` 开头 / DD-18.1 的「首次启动若 ``packages/``
   为空 → 自动装载 ``demo-templates/``」）。判据落在**解析成功**上，不是"列表里有一条"：
   只登记原件的条目 ``parsed=False``，界面上看得见、构建时读不到包。
2. **重启不会把示例塞回来**，用户动过的条目也不被覆盖（``server/seed.py`` 的三条纪律）。
   判据是"库非空 ⇒ 跳过"，而不是"某个 id 存不存在"。
3. **容器形态的链路真跑通**：迁移 → 落库 → api 装配 → **随发行版分发的示例资产**
   （``examples/`` 的领域模型 / 档案 / 事实源）从接口进库 → 另一个进程角色
   （``python -m server.worker --once`` 那条代码路径）消费 → 产物 ``.docx`` 落在数据卷上，
   且内容来自示例输入。

这里**没有 Docker**（``docker: command not found``，见 README 第 5 节）
---------------------------------------------------------------------
本文件验的是容器里那些**命令**的效果，**不是** ``docker build`` 本身。这条边界写在
``deploy/README.md`` 里，也写在这里：把"命令跑过一遍"说成"镜像能构建"是同一类虚假声明，
而且是最容易在开源之后被第一个使用者撞见的那一类。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from docfidelity.builder import build_registry as reference_registry
from server.api.deps import build_state
from server.main import APP_STATE_ATTRIBUTE, create_app
from server.settings import ServerSettings
from server.storage import run_migrations
from server.worker import main as worker_main

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_TEMPLATES = REPO_ROOT / "demo-templates"
EXAMPLES = REPO_ROOT / "examples"

#: 首启装载的期望结果。写死而**不是**现场扫目录：扫出来再比就是"拿实现验实现"，
#: 多装一份少装一份都会照常通过。顺序即 ``server.seed.demo_sources`` 的路径序。
SEEDED_REFS = ("demo-operations@1", "demo-software-design@1")

#: 本次要跑通的那份文档（一模块一表型），它绑的正是被装载进来的示例模板。
DOC_KEY = "api-summary"
PROFILE_ID = "api-summary"
TEMPLATE_REF = "demo-software-design@1"
SOURCE_ID = "interface-surface"

#: 示例事实源进数据卷后的相对目录（与 ``examples/sources.json`` 的 ``source_root`` 对齐）。
SOURCE_DIRNAME = "data"


# ====================================================================== 装配
def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _settings(root: Path, *, seed: bool = True) -> ServerSettings:
    """``deploy/.env.example`` 那四个部署键的等价物。

    ``sqlite:///<绝对路径>`` 里 scheme 之后是**三个**斜杠（前两个是 ``//`` 网络位、
    第三个才是绝对路径的开始）。写成相对路径会落到进程的工作目录下，症状是
    "作业记录有时在有时不在"——因为它取决于从哪儿把进程起起来。
    """
    return ServerSettings(
        _env_file=None,
        storage_root=root,
        database_url=f"sqlite:///{(root / 'df.db').as_posix()}",
        seed_demo_templates=seed,
        demo_templates_dir=DEMO_TEMPLATES,
        start_worker=False,
    )


def _container_form(root: Path, *, seed: bool = True) -> FastAPI:
    """按容器入口脚本的顺序准备数据卷：建目录 → 迁移 → 起应用。

    迁移**必须**发生在装配之前（``deploy/entrypoint.sh`` 就是这么排的）。把它挪进应用
    工厂会把"库结构比代码旧"与"服务起不来"绑成一件事：前者是一次可修的启动失败，
    后者是一次全站事故，而两件事在日志上长得一模一样。
    """
    root.mkdir(parents=True, exist_ok=True)
    settings = _settings(root, seed=seed)
    assert settings.database_url is not None
    run_migrations(settings.database_url)
    return create_app(settings=settings, stages=reference_registry(), start_worker=False)


def _stage_source_files(root: Path) -> None:
    """把示例输入文件放进数据卷。

    来源 ``root`` 相对**存储根**解析（``server.jobs.payload`` 里冻的 ``source_root``）。
    ``examples/data/`` 是 Git 里的资产，不在卷上；不拷这一步，"一键体验"就只在仓库工作
    目录下成立，而那恰恰不是容器里的形态。
    """
    target = root / SOURCE_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLES / "data" / "openapi.json", target / "openapi.json")


def _source_declaration() -> dict[str, Any]:
    """``examples/sources.json`` 里那条接口来源，``root`` 前缀上它声明的 ``source_root``。

    从声明里派生，而不是在这里另写一份：``examples/sources.json`` 是"这两条来源指向
    哪些文件"的唯一定义，写第二份就会有第二个答案，而两者分叉时的症状是
    "界面上试接入通了、构建却说找不到"。
    """
    bundle = _json(EXAMPLES / "sources.json")
    for item in bundle["sources"]:
        if item["source_id"] == SOURCE_ID:
            return {**item, "root": f"{bundle['source_root']}/{item['root']}"}
    raise AssertionError(f"示例来源里没有 {SOURCE_ID}")


# ============================================================ 判据一：首启装载
def test_the_first_boot_fills_an_empty_library_with_the_shipped_samples(tmp_path: Path) -> None:
    """空库 → 两份示例模板**解析成功**地进库，并且接口立刻列得出来。"""
    root = tmp_path / "data"

    with TestClient(_container_form(root)) as client:
        health = client.get("/health").json()
        library = client.get("/api/v1/templates").json()
        parsed = {
            item["template_id"]: client.get(f"/api/v1/templates/{item['template_id']}").json()[
                "parsed"
            ]
            for item in library["items"]
        }

    assert health["demo_seed"] == {
        "skipped": False,
        "seeded": list(SEEDED_REFS),
        "errors": [],
    }
    assert sorted(item["ref"] for item in library["items"]) == list(SEEDED_REFS)
    # 「装上了」与「解析通了」是两件事：只登记原件的条目 ``parsed=False``，它在界面
    # 上与正常条目几乎没有差别，而构建时读包会报 ``E_PACKAGE_INCOMPLETE``——
    # 首启装载走真实入库流水线（``server.seed`` 的第 2 条纪律）就是为了避免这个形态。
    assert parsed == {"demo-operations": True, "demo-software-design": True}


# ======================================================== 判据二：不重复塞回来
def test_a_restart_skips_and_never_pushes_a_sample_back(tmp_path: Path) -> None:
    """库非空 ⇒ 跳过；用户动过的条目**原样保留**。

    「拿某个 id 在不在当判据」正是"我明明删了它、它又回来了"这类投诉的来源。这里用
    **废弃**代表"用户动过"：模板版本是配置与历史产物的引用键，只能标不能删
    （``TemplateLibraryMixin.deprecate_template``），而废弃状态恰恰是最容易被
    "再装载一次"抹掉的东西——抹掉之后已经产出的文档照旧能复现，只是没人再看得见
    "这一版为什么不该用"。
    """
    root = tmp_path / "data"

    with TestClient(_container_form(root)) as first:
        marked = first.post(
            "/api/v1/templates/demo-operations/deprecate",
            json={"version": "1", "reason": "演示：使用者动过这一版"},
        )
    assert marked.status_code == 200, marked.text
    assert marked.json()["deprecated"] is True

    with TestClient(_container_form(root)) as second:
        health = second.get("/health").json()
        active = second.get("/api/v1/templates", params={"include_deprecated": False}).json()

    assert health["demo_seed"] == {"skipped": True, "seeded": [], "errors": []}
    assert [item["ref"] for item in active["items"]] == [TEMPLATE_REF], "被弃用的示例又被塞回来了"


# ==================================================== 判据三：容器形态跑通全链路
def test_the_container_form_really_runs_the_shipped_example_to_a_deliverable(
    tmp_path: Path,
) -> None:
    """迁移 → api → 示例资产 → 独立 worker 消费 → 产物落在数据卷上。"""
    root = tmp_path / "data"
    app = _container_form(root)
    _stage_source_files(root)

    with TestClient(app) as client:
        # ① 两个进程角色共用同一套装配：另起一份状态，看到的必须是同一个库。
        #    装配分叉是这类进程最容易出的错（"api 能构建、worker 说阶段缺失"），
        #    而两边的日志各自看都很正常——所以这里让它成为一个当场会红的判据。
        other = build_state(settings=_settings(root), stages=reference_registry())
        assert sorted(item.ref for item in other.repository.list_templates()) == list(SEEDED_REFS)

        # ② 接口自身的形态：能接构建，且本进程不起消费线程（消费交给 df-worker）
        health = client.get("/health").json()
        assert health["build_ready"] is True
        assert health["worker_mode"] == "external"
        assert client.get("/readyz").status_code == 200
        assert client.get("/healthz").json() == {"status": "alive"}

        # ③ 随发行版分发的示例资产从接口进库（与使用者在界面上点的是同一条链路）
        for model in _json(EXAMPLES / "domains.json"):
            posted = client.post(
                "/api/v1/domain-models", json={"model": model, "author": "one-click"}
            )
            assert posted.status_code == 201, posted.text

        profile = client.post(
            "/api/v1/profiles",
            json={
                "profile": _json(EXAMPLES / "profiles" / f"{PROFILE_ID}.json"),
                "author": "one-click",
            },
        )
        assert profile.status_code == 201, profile.text

        project_id: str = client.post("/api/v1/projects", json={"name": "一键体验"}).json()[
            "project_id"
        ]

        added = client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={"config": _source_declaration(), "author": "one-click"},
        )
        assert added.status_code == 201, added.text

        config = client.put(
            f"/api/v1/projects/{project_id}/config",
            json={
                "project_id": project_id,
                "author": "one-click",
                "documents": [
                    {
                        "doc_key": DOC_KEY,
                        "title": "接口总表",
                        "profile_id": PROFILE_ID,
                        "template_ref": TEMPLATE_REF,
                        "source_ids": [SOURCE_ID],
                    }
                ],
            },
        )
        assert config.status_code == 200, config.text

        submitted = client.post(f"/api/v1/projects/{project_id}/jobs", json={"author": "one-click"})
        assert submitted.status_code == 201, submitted.text
        orchestration: str = submitted.json()["job_id"]

        # ④ 消费作业的是**另一个进程角色**：走 ``python -m server.worker --once`` 的
        #    同一条代码路径（``server/worker.py`` 的 ``--once`` 分支）
        state = getattr(app.state, APP_STATE_ATTRIBUTE)
        assert state.runner.run_until_idle(), "队列是空的——作业根本没排进去"

        detail = client.get(f"/api/v1/jobs/{orchestration}").json()
        assert detail["job"]["status"] == "succeeded", detail
        child = detail["children"][0]
        assert child["status"] == "succeeded", child

    # ⑤ 产物与元数据都落在**数据卷**上（DD-18.1 的数据边界：一个卷，全在它下面）
    assert (root / "df.db").is_file(), "元数据库不在卷上，重启就丢作业登记"
    artifacts = sorted((root / "jobs" / child["job_id"] / "artifacts").glob("*.docx"))
    assert artifacts, "作业成功了却没有产物落盘"

    with zipfile.ZipFile(artifacts[0]) as package:
        assert package.testzip() is None
        text = package.read("word/document.xml").decode("utf-8")
    assert "/orders" in text, "产物里没有示例输入里的接口路径——跑通了但内容是空的"


# ============================================================ worker 进程入口
def test_the_worker_process_starts_drains_the_queue_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m server.worker --once``：起得来、跑得空、正常返回 0。

    只钉退出码，不钉日志：这里的判据是"这条命令在容器里不会卡住、也不会一上来就崩"。
    日志格式是运维面的事，钉住它只会让改一行格式就红一片。

    ``--once`` 之外的那条常驻分支要验的是"能被 SIGTERM 收尾"（``install_signal_handlers``
    的模块文档解释了为什么不能用 ``KeyboardInterrupt`` 兜底），而信号在测试进程里
    改的是**本进程**的处理器——那属于进程级行为，不是这一条能验的。
    """
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DOCFIDELITY_STORAGE_ROOT", str(root))
    monkeypatch.setenv("DOCFIDELITY_DATABASE_URL", f"sqlite:///{(root / 'df.db').as_posix()}")
    # 首启装载是 api 的职责；worker 只消费，不该顺手往库里塞东西
    monkeypatch.setenv("DOCFIDELITY_SEED_DEMO_TEMPLATES", "false")
    url = _settings(root).database_url
    assert url is not None
    run_migrations(url)

    assert worker_main(["--once"]) == 0
