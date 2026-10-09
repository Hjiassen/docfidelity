"""仓储层：协议 + 两种实现（DD-13.1）。

``protocols`` 定义接缝，``memory`` 是进程内实现（默认），``sqlalchemy_repo`` +
``tables`` + ``engine`` + ``migrations`` 是落库实现（P0-14 第 2 轮）。

**上层（``server.services`` / ``server.jobs`` / ``server.api``）一行不改**——这正是把
协议单独成一个模块的原因。选哪条实现在 :func:`build_repository` 一处收口，
因为"用哪种存储"是部署决策，不该散落在组装点里。
"""

from __future__ import annotations

from .engine import (
    create_engine_for,
    create_schema,
    is_memory_url,
    migration_config,
    run_migrations,
)
from .memory import MemoryRepository
from .protocols import (
    ConfigVersion,
    DomainModelRepository,
    FactSourceRecord,
    ProfileRepository,
    Project,
    RegisteredDomainModel,
    RegisteredProfile,
    RegisteredTemplate,
    Repository,
    SourceRepository,
    TemplateRepository,
    UploadRecord,
)
from .sqlalchemy_repo import SqlAlchemyRepository
from .tables import Base

__all__ = [
    "Base",
    "ConfigVersion",
    # 单类仓储的**协议名**也要导出：用例层与编排层的签名写作
    # ``resolve_profile(repository: ProfileRepository, ...)``——它们只要求"能查档案"，
    # 而不要求"是一整个 Repository"。拿不到名字时调用方只会把签名写成 ``Repository``，
    # 于是"这个函数其实只用到三个方法"这件事就从签名上消失了。
    "DomainModelRepository",
    "FactSourceRecord",
    "MemoryRepository",
    "ProfileRepository",
    "Project",
    "RegisteredDomainModel",
    "RegisteredProfile",
    "RegisteredTemplate",
    "Repository",
    "SourceRepository",
    "SqlAlchemyRepository",
    "TemplateRepository",
    "UploadRecord",
    "build_repository",
    "create_engine_for",
    "create_schema",
    "is_memory_url",
    "migration_config",
    "run_migrations",
]


def build_repository(database_url: str | None = None) -> Repository:
    """按 ``database_url`` 选仓储。

    为 ``None``（未配置）即进程内仓储：单机演示与测试因此**零配置、不落盘**
    （DD-17.1 的 ``unit`` 层禁 DB 依赖正是靠这条路径成立）。配了连接串就走落库——
    进程重启后作业、断点登记、报告都还在，"构建到一半重启就丢工作"这件事才不成立。

    **不在这里自动建表**：schema 归 Alembic（``run_migrations`` / CLI ``alembic
    upgrade head``）。自动建表会让"忘记跑迁移"从一次明确的启动失败，变成一次
    "库结构比代码旧"的运行期诡异行为。
    """
    if database_url is None:
        return MemoryRepository()
    return SqlAlchemyRepository(create_engine_for(database_url))
