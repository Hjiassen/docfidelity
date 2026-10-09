"""SQLAlchemy 表结构（DD-13.1 的 ``server/storage``；P0-14 第 2 轮）。

「JSON 载荷 + 抽取列」而不是「一列一字段」
----------------------------------------
每张表只把**会被查询 / 排序 / 约束**的字段抽成独立列，其余整份契约对象以 JSON 落在
``payload`` 列里。理由不是偷懒：契约模型是唯一事实来源，把它的每个字段都摊成列，
等于在数据库里维护**第二份 schema**，而第二份 schema 必然漂移——契约新增一个可选字段时，
没有人会记得同步加一列，于是"内存版有这个字段、落库版读出来是空"这种最难查的差异
会在某个部署上冒出来。

反过来的代价是"不能按任意字段筛"。这恰好是本文档要约束的：**能筛什么**由这里的列决定，
加了新筛选需求就显式加一列（并写迁移）——而不是"库里有 40 列、实际只用 3 列"。

时刻为什么用文本存
------------------
见 :class:`UtcDateTime`。一句话：SQLite 不保存 ``tzinfo``，而内存版返回 aware——
换实现不该改变语义。

读出时一律经 ``model_validate`` 还原成契约对象，于是「读取返回副本」（``protocols`` 的
纪律 2）由反序列化天然满足：库里那份是文本，不可能与返回的对象共享内存。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

__all__ = [
    "ArtifactRow",
    "Base",
    "ConfigVersionRow",
    "DomainModelRow",
    "FactSourceRow",
    "JobRow",
    "ProfileRow",
    "ProjectRow",
    "ReportRow",
    "StageRow",
    "TemplateRow",
    "UploadRow",
    "UtcDateTime",
]


class UtcDateTime(TypeDecorator[datetime]):
    """UTC 时刻 ↔ ISO-8601 文本。

    ``DateTime(timezone=True)`` 在 PostgreSQL 上是对的，在 **SQLite 上会静默丢掉
    tzinfo**——读回来的是 naive datetime，而内存版给的是 aware。后果不是"少个时区"，
    而是 ``expires_at <= now()`` 直接抛
    ``can't compare offset-naive and offset-aware datetimes``：保留期回收在 SQLite
    部署上从"能跑"变成"每次调用都 500"。

    统一成"存 ISO 文本、解析回 aware"后，两种方言的行为一致；ISO-8601 的 UTC 文本
    又天然按字典序 = 时间序，排序语义不受影响。
    """

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # naive 一律按 UTC 解释，而不是"原样存"。原样存会让同一列混进两种含义的
            # 时刻，而"这条是本地时间还是 UTC"从数据本身再也看不出来。
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    """声明基类。

    **建表有且只有一条生产路径：Alembic**（``server/storage/migrations``）。
    ``engine.create_schema`` 里的 ``Base.metadata.create_all`` 只服务测试与演示，
    两者是否一致由 ``tests/api/test_server_sqlalchemy.py`` 的
    「迁移结果 == ORM 元数据」用例盯住。
    """


class ProjectRow(Base):
    """项目。``Project`` 是小数据类（不是契约模型），故直接铺成列。

    ``name`` 的"未归档项目内唯一"**不**做成部分唯一索引：SQLite 与 PostgreSQL 对
    部分索引的语法与语义支持不同（且归档项目要能重名），交给
    :meth:`SqlAlchemyRepository._assert_name_free` 在同一事务里查——口径只有一处，
    两种方言行为一致。索引只加在 ``name`` 上做查询加速，不承担唯一性。
    """

    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ConfigVersionRow(Base):
    """配置版本留痕（DD-13.6：写入不覆盖历史）。

    主键是 ``(project_id, version)`` 而不是自增 id：**版本号由仓储派生**
    （同项目内自增），把它做成自增主键会让"项目的第 3 版"变成一个全局计数。
    复合主键同时是那个约束本身——并发写入撞号时由数据库拒绝，而不是靠应用层小心。
    """

    __tablename__ = "config_versions"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class JobRow(Base):
    """作业。``payload`` 是整份 ``contracts.job.Job`` 的 JSON。

    抽出的三列（``project_id`` / ``status`` / ``kind``）**只服务列表筛选**，
    它们是 ``JobRepository.list_jobs`` 的三个入参；``created_at`` 服务 FIFO 排序
    （``JobRunner.run_once`` 的公平性靠它）。

    ``seq`` 为什么必须存在
    ----------------------
    ``created_at`` **不足以保证 FIFO**：一次多文档构建在同一时刻提交若干个子作业
    （注入时钟、或时钟粒度粗于提交间隔），它们的时刻完全相同。此时次序只能靠一个
    平局键，而唯一可用的候选 ``job_id`` 是从 ``doc_key`` 派生的——按它排等于按文档
    名**字母序**执行，而队列的语义是**提交序**。``seq`` 是仓储在插入时分配的单调
    序号：它在两种方言下都存在、都与提交序同向，且**不进入契约**（FIFO 是仓储的
    职责，不是领域概念）。
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    #: 提交序号（插入时分配，单调）。平局时按它排，见类文档。
    seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class StageRow(Base):
    """阶段记录。按 ``(job_id, ordinal)`` 存，``ordinal`` = ``STAGE_ORDER`` 里的次序。

    存完整列表而不是逐阶段 upsert：``save_stages`` 的语义是"这就是当前全部阶段"，
    逐条更新会让"上一轮多出来的那条"留在库里，而对同一个作业来说阶段集合是固定的。
    """

    __tablename__ = "stages"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class ArtifactRow(Base):
    """产物登记。``created_at`` 参与 ``list_artifacts`` 的稳定排序。"""

    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class ReportRow(Base):
    """门禁报告。一个作业至多一份（``put_report`` 是覆盖语义）。"""

    __tablename__ = "reports"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class UploadRow(Base):
    """上传件登记。

    ``filename`` 与 ``purpose`` 抽成列而不是只留在 ``payload`` 里：审计与保留清理
    都要按它们筛（"三天前的模板包都删掉"），而这类查询不该退化成"全表读 JSON
    再在 Python 里过滤"。
    """

    __tablename__ = "uploads"

    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class TemplateRow(Base):
    """已登记进模板库的模板包版本（P05 第 5 步）。

    主键 ``(template_id, version)`` **就是**身份本身，与项目配置里
    ``template_ref = "<template_id>@<version>"`` 一一对应；做成自增 id 再加唯一约束
    会让"库里存的键"和"配置里引用的键"长期对不上。

    只抽三列：``doc_type``（按文档类型筛模板）、``registered_at``（列表排序）、
    ``deprecated``（默认不列废弃版本）。包内容本身（清单 / 样式 / 骨架 / 块索引）在
    磁盘上的模板包目录里，不在这里——把契约 JSON 也影射进库等于维护第二份 schema，
    而它必然与包目录里的那份漂移。
    """

    __tablename__ = "templates"

    template_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_type: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class ProfileRow(Base):
    """已登记的文档类型档案版本（``doc_type_profiles``，DD-11.1）。

    形状与 :class:`TemplateRow` 刻意保持一致：两者都是"带版本的资产"，
    都要按 ``(id, version)`` 引用、都要能按文档类型筛、都要有废弃标记。让它们长得一样，
    是因为界面上的两行（模板包行、档案行）本来就并排展示——数据形状分叉之后，
    两个列表的排序与筛选口径就会各自演化。

    档案本体（体例 + 投影规则 + 绑定）落在 ``profiles/<profile_id>/<version>/`` 下的
    ``profile.json``：档案要能整包导出给第三方（FR-M2-25），磁盘上的那份才是原件。
    """

    __tablename__ = "doc_type_profiles"

    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class DomainModelRow(Base):
    """已登记的领域模型声明（``domain_models``，``server/domain_models.py``）。

    主键是 ``kind`` **本身**，**没有** ``version`` 列——与 ``templates`` /
    ``doc_type_profiles`` 的 ``(id, version)`` 刻意不同。理由写在
    ``protocols.RegisteredDomainModel`` 的类文档里，一句话：引用领域模型的地方
    （档案的 ``for_each``、追溯边的 ``<kind>:<key>``）**带不上版本**，所以"哪一版"
    这个问题在任何调用点都没有答案，库里也就不该存在多版。

    只抽两列（``registered_at`` 排序、``deprecated`` 默认过滤），``title`` 与
    ``fingerprint`` 都留在 ``payload`` 里：前者只用于显示、后者只用于"这次登记与
    库里那份一样吗"的一次比较，都不承担筛选职责。抽成列会让"列与载荷谁是真相"
    多出两个需要维持一致的字段。
    """

    __tablename__ = "domain_models"

    kind: Mapped[str] = mapped_column(String(128), primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class FactSourceRow(Base):
    """一个项目的事实源实例（``fact_sources``，DD-11.1）。

    主键是 ``(project_id, source_id)`` 而不是全局唯一的 ``source_id``：事实源的标识
    只在项目内需要唯一（``Fact.source`` 引用的是本项目里的那一个），做成全局唯一会逼着
    每个项目给自己的来源名加前缀。

    抽 ``connector`` 一列是为了"按接入器类型筛"（"本项目有哪几条 OpenAPI 来源"）。
    ``item_count`` / ``options`` / ``bindings`` 全留在 ``payload`` 里：它们要么是数值、
    要么是嵌套结构，都不承担筛选职责。
    """

    __tablename__ = "fact_sources"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    connector: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    #: 最近一次"试接入"的时刻；``None`` = 从未探过（**不是**"探过了没问题"）。
    last_probed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
