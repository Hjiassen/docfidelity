"""模板指纹与分区摘要（DD-7.4 / DD-4.7，FR-M2-06 / FR-M2-18）。

分区比 DD-7.4 多几个，理由必须写清楚
------------------------------------
DD-7.4 的伪代码列了五个分区（``styles`` / ``page_setup`` / ``numbering`` /
``outline`` / ``blocks``）。实现上补了三个：

* ``used_styles`` —— 生成期**可引用**的样式面（FR-M2-13）。它变了而 ``styles``
  没变是最常见的情形（模板里新增了一处对某样式的引用）。折叠进 ``styles`` 会让
  "样式定义没改、但生成期能引用哪些样式改了"这个变更无法被单独报出，而版本 diff
  的意义恰恰是指出"该去检查什么"。
* ``placeholders`` —— 生成期**必须替换或显式清除**的模板文本集合。它变了意味着
  产物要多替换（或少替换）若干处文本，属于会直接影响产物的变更。
* ``source`` —— 原件归一化摘要。它回答一个别的分区都答不了的问题："原件真的变了，
  但没有任何契约分区变"（模板被重存过，或改动落在未建模的属性上）。

``source`` 与 ``manifest.source_hash`` **不是一回事**：后者是原始字节摘要，Word
每次保存都会变（rsid、docId、关系 ID 全在动），拿它比对必然满屏假差异。前者走
:func:`docfidelity.ooxml.normalized_digest`，只有**真实**改动才会让它变。

两个必须守住的纪律
------------------
1. **归一化只有一处实现**（1.2 第 7 条教训）。本模块不自己抹平任何易变标识，
   需要摘要就去调 ``ooxml.normalize``；``source`` 分区也直接是它的输出。
2. **分区边界只有一处定义**。:func:`section_payloads` 是唯一说"哪个分区包含哪些
   内容"的地方——算指纹、存指纹、校验指纹、做版本 diff 四件事共用它。四处各写
   一遍的必然结果是"指纹说 styles 变了、diff 却说 styles 没事"。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from docfidelity.contracts.template import TemplateFingerprint, TemplatePackage
from docfidelity.ooxml import DocxPackage, normalized_digest
from docfidelity.platform.hashing import combine_digests, digest_payload, sha256_hex

#: 参与摘要的契约分区，顺序即 ``TemplateFingerprint.sections`` 的呈现顺序。
FINGERPRINT_SECTIONS: Final[tuple[str, ...]] = (
    "styles",
    "page_setup",
    "numbering",
    "used_styles",
    "outline",
    "blocks",
    "placeholders",
)

#: 原件分区。单独拎出来是因为它**无法从模板包目录重算**（包不带原件）。
SOURCE_SECTION: Final = "source"

#: 全部分区名（含原件分区）。
ALL_SECTIONS: Final[tuple[str, ...]] = (*FINGERPRINT_SECTIONS, SOURCE_SECTION)


def section_payloads(
    package: TemplatePackage,
    *,
    block_assets: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """分区 → 参与摘要的内容。**分区边界只有这一处定义**。

    ``blocks`` 同时覆盖索引与 ``blocks/`` 下的二进制资产：只算索引的话，
    "把封面图换了一张、索引一个字没变"会被判成"版块分区没变"，
    而那恰恰是最容易被忽视、又最影响产物外观的一类变更。
    """
    return {
        "styles": [style.model_dump(mode="json") for style in package.styles],
        "page_setup": package.page_setup.model_dump(mode="json"),
        "numbering": package.numbering.model_dump(mode="json"),
        "used_styles": package.used_styles.model_dump(mode="json"),
        "outline": package.outline.model_dump(mode="json"),
        "blocks": {
            "index": package.blocks.model_dump(mode="json"),
            "assets": {
                name: sha256_hex(data) for name, data in sorted((block_assets or {}).items())
            },
        },
        "placeholders": package.placeholders.model_dump(mode="json"),
    }


def source_digest(package: DocxPackage) -> str:
    """原件归一化摘要。

    薄包装一层是为了把"原件摘要走哪套口径"这件事**钉在一个名字上**：
    调用方不该知道 ``normalize_volatile`` 的存在，否则迟早有人自己写一份。
    """
    return normalized_digest(package)


def compute_fingerprint(
    package: TemplatePackage,
    *,
    block_assets: Mapping[str, bytes] | None = None,
    source_digest_value: str | None = None,
) -> TemplateFingerprint:
    """算分区指纹。

    ``source_digest_value`` 为 ``None`` 时**不写** ``source`` 分区（而不是写空
    字符串）：写空串的话，"没有原件"与"原件归一化后是空"会算出同一个指纹。
    """
    sections: dict[str, str] = {
        name: digest_payload(payload)
        for name, payload in section_payloads(package, block_assets=block_assets).items()
    }
    if source_digest_value is not None:
        sections[SOURCE_SECTION] = source_digest_value
    return TemplateFingerprint(digest=combine_digests(sections), sections=sections)


def verify_fingerprint(
    package: TemplatePackage,
    *,
    block_assets: Mapping[str, bytes] | None = None,
    source_digest_value: str | None = None,
) -> list[str]:
    """重算并比对指纹，返回问题清单（空表示通过）。

    三件事都要查，缺一件这套校验就是摆设：

    1. **分区摘要是否与包内容一致** —— 包被外部改过、或指纹算完之后又被改过；
    2. **``digest`` 是否等于自身分区的合成** —— 只改 ``digest`` 不改 ``sections``
       的篡改（或者算 ``digest`` 的口径变了）必须能被抓到；
    3. **有没有指纹** —— 缺 ``fingerprint.json`` 时报出来，而不是静默通过。

    ``source`` 分区在原件不可用时**跳过**：它在模板包目录里本就无法重算。
    跳过略过的是"原件是否变了"，不是"包内容是否变了"——后者由其余分区覆盖。
    """
    stored = package.fingerprint
    if stored is None:
        return ["模板包没有指纹：fingerprint.json 缺失"]

    problems: list[str] = []
    expected = section_payloads(package, block_assets=block_assets)
    for name, payload in expected.items():
        actual = digest_payload(payload)
        recorded = stored.sections.get(name)
        if recorded is None:
            problems.append(f"指纹缺少分区：{name}")
        elif recorded != actual:
            problems.append(
                f"分区摘要与包内容不一致：{name}"
                f"（指纹 {_short(recorded)}…，实际 {_short(actual)}…）"
            )

    # 反向查一遍：指纹里多出来的分区名说明算指纹的代码与校验用的是两套分区集合。
    for name in stored.sections:
        if name != SOURCE_SECTION and name not in expected:
            problems.append(f"指纹含未知分区：{name}")

    if (
        source_digest_value is not None
        and SOURCE_SECTION in stored.sections
        and stored.sections[SOURCE_SECTION] != source_digest_value
    ):
        problems.append("原件摘要与指纹不一致：模板原件已被替换")

    if stored.digest != combine_digests(stored.sections):
        problems.append("整体摘要与分区摘要不自洽：指纹被改写过，或算法已变更")
    return problems


def _short(digest: str) -> str:
    return digest[:12]


__all__ = [
    "ALL_SECTIONS",
    "FINGERPRINT_SECTIONS",
    "SOURCE_SECTION",
    "compute_fingerprint",
    "section_payloads",
    "source_digest",
    "verify_fingerprint",
]
