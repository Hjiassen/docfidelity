"""api 层装配：路由注册与异常处理器挂载。

调用顺序只有一处（:func:`register`），避免"应用工厂挂了一半、测试另挂一半"。
"""

from __future__ import annotations

from fastapi import FastAPI

from . import (
    decisions,
    documents,
    domain_models,
    handlers,
    jobs,
    profiles,
    projects,
    sources,
    system,
    template_imports,
    templates,
    uploads,
)

__all__ = ["ROUTERS", "register", "register_routes"]

#: 全部路由。顺序即 OpenAPI 里的分组顺序：先项目（用户从这里开始），再上传收件、
#: 模板入库与模板库，然后**领域模型 → 档案 → 事实源**（先有词汇表，才有引用它的档案，
#: 最后才是来源），最后作业与裁决、系统（设置与维护）。
ROUTERS = (
    projects.router,
    uploads.router,
    templates.router,
    template_imports.router,
    documents.router,
    domain_models.router,
    profiles.router,
    sources.router,
    jobs.router,
    decisions.router,
    system.router,
)


def register_routes(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router)
    template_imports.register_schema(app)


def register(app: FastAPI) -> None:
    """挂路由 + 挂异常处理器。"""
    register_routes(app)
    handlers.register(app)
