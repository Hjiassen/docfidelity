"""冻结输入的解析（TASK-P0-19：Office 姿态进冻结输入）。

只测本轮新加的两项，不重复 coverage 那些已有的：``office_enabled`` / ``office_reason``
是**唯一**能表达"这次构建要不要让本机 Word 参与"的入口，而它的解析有一个很容易写错的
地方——``bool("false")`` 是 ``True``。
"""

from __future__ import annotations

from typing import Any

import pytest

from docfidelity.builder.inputs import BuildInputs
from docfidelity.contracts.errors import ConfigurationError

pytestmark = pytest.mark.unit


#: 最小合法快照。``applies`` 三条是必填（档案要能告诉使用者"什么情况适用"），
#: 其余都有默认值——故这里只写这三条，不铺一份完整的档案。
BASE: dict[str, Any] = {
    "project_id": "example",
    "doc_key": "overview",
    "template_ref": "example@1",
    "source_root": ".",
    "sources": [],
    "domain_models": [],
    "profile": {
        "profile_id": "example",
        "doc_type": "example",
        "title": "示例",
        "applies": {
            "layout_source": "example@1",
            "structure_enumerable": "骨架来自模板目录",
            "content_traceable": "内容来自登记的事实源",
        },
        "genre": {},
    },
}


def _inputs(**extra: Any) -> BuildInputs:
    return BuildInputs.of({**BASE, **extra})


def test_absent_keys_leave_the_pose_undeclared() -> None:
    """两个键都不写 ⇒ ``None``，即"调用方没表态"，由调用方（服务端/示例）决定。"""
    inputs = _inputs()
    assert inputs.office_enabled is None
    assert inputs.office_reason is None


@pytest.mark.parametrize("raw", [False, "false", "False", "0", "no"])
def test_false_words_parse_as_false(raw: object) -> None:
    """``"false"`` 必须解析成 ``False``。

    这不是吹毛求疵：``bool("false")`` 是 ``True``，用朴素真值判断的话，"我明明把 Word
    关了"会静默变成"开着"——而症状（产物多出一次 Word 刷新、字节随机器而变）出现在
    很远的下游，与这里隔着一整个构建。
    """
    assert _inputs(office_enabled=raw).office_enabled is False


@pytest.mark.parametrize("raw", [True, "true", "True", "1", "yes"])
def test_true_words_parse_as_true(raw: object) -> None:
    assert _inputs(office_enabled=raw).office_enabled is True


def test_a_null_value_means_undeclared() -> None:
    """显式 ``null`` 与缺键等价（JSON 里两者都表示"没有这一项"）。"""
    assert _inputs(office_enabled=None).office_enabled is None


def test_an_unparsable_value_fails_loudly() -> None:
    """写不出来的值**报错**，不静默兜底。

    静默取缺省会让"关掉 Word"写错一个字母就变成"照常探测 Word"，而排查的人只会看到
    "产物怎么又不一样了"。
    """
    with pytest.raises(ConfigurationError) as excinfo:
        _inputs(office_enabled="maybe")

    assert excinfo.value.where["key"] == "office_enabled"


def test_the_reason_travels_with_the_pose() -> None:
    """原因原样带出来（渲染报告要拿它区分"关掉的"与"探测不到的"）。"""
    inputs = _inputs(office_enabled=False, office_reason="确定性姿态关闭了域刷新")
    assert inputs.office_enabled is False
    assert inputs.office_reason == "确定性姿态关闭了域刷新"


def test_an_empty_reason_is_the_same_as_none() -> None:
    """空串等同于没给：报告里"原因：空字符串"是一句毫无信息的话。"""
    assert _inputs(office_reason="").office_reason is None
