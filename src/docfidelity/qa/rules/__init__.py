"""内置规则实现与声明（DD-10.1 / DD-10.2 / 附录 C）。

导入本包即完成**断言登记**：``checks_style`` / ``checks_layout`` / ``checks_content``
在模块末尾各调一次 :func:`~docfidelity.qa.engine.register_check`。登记放在导入期而不是
由调用方显式装配，理由很实际——声明（``builtin.yaml``）里写的是断言名，两者必须
同时可用；分开装配就会出现"规则在跑、断言没登记"的静默失败。

``builtin.yaml`` 与断言名之间的对应关系由测试保证：规则文件里出现未登记的 ``check``
名，或登记了却没人声明的断言，都会被单测报出来。**不靠人记。**
"""

from __future__ import annotations

from ..engine import builtin_rule_files, check_names

# 导入即登记（这个副作用就是本包存在的理由）。
from . import checks_content, checks_layout, checks_style

__all__ = [
    "builtin_rule_files",
    "check_names",
    "checks_content",
    "checks_layout",
    "checks_style",
]
