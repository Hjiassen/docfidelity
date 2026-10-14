"""决策路由兼容入口。实现已下沉，保留旧导入路径。"""

from docfidelity.pipeline.decisioning.routing import *  # noqa: F403
from docfidelity.pipeline.decisioning.routing import __all__ as __all__
from docfidelity.pipeline.decisioning.routing import _resolve_one as _resolve_one
