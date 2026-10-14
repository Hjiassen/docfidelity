"""决策留痕兼容入口。实现已下沉，保留旧导入路径。"""

from docfidelity.pipeline.decisioning.recorder import *  # noqa: F403
from docfidelity.pipeline.decisioning.recorder import __all__ as __all__
from docfidelity.pipeline.decisioning.recorder import options_of as options_of
