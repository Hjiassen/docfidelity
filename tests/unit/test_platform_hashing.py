"""``platform.hashing`` 单测（DD-2.1）。

守的是"同一份数据 → 同一个摘要"这条承诺。它一旦失守，症状是跨机器 / 跨平台
的产物比对全部失效，而且**不会报错**——只会给出错误结论。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from docfidelity.platform.hashing import (
    canonical_json,
    combine_digests,
    digest_entries,
    digest_payload,
    sha256_file,
    sha256_hex,
)

pytestmark = pytest.mark.unit


class TestCanonicalJson:
    def test_key_order_does_not_matter(self) -> None:
        first = canonical_json({"b": 1, "a": 2, "c": 3})
        second = canonical_json({"c": 3, "a": 2, "b": 1})
        assert first == second

    def test_output_has_no_optional_whitespace(self) -> None:
        payload = canonical_json({"a": [1, 2], "b": {"c": "d"}})
        assert payload == b'{"a":[1,2],"b":{"c":"d"}}'
        assert b" " not in payload

    def test_chinese_is_not_escaped(self) -> None:
        """中文保持可读——摘要算错时人工比对能直接看出差异在哪。"""
        payload = canonical_json({"标题": "概述"})
        assert "标题".encode() in payload
        assert b"\\u" not in payload

    def test_nan_is_rejected_instead_of_producing_invalid_json(self) -> None:
        """NaN 在 JSON 里不是合法字面量，放进去会产出无法解析的产物。"""
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})

    def test_output_is_valid_json(self) -> None:
        assert json.loads(canonical_json({"a": 1, "b": [1.5, None, True]})) == {
            "a": 1,
            "b": [1.5, None, True],
        }

    def test_nested_key_order_also_normalized(self) -> None:
        first = canonical_json({"outer": {"z": 1, "a": 2}})
        second = canonical_json({"outer": {"a": 2, "z": 1}})
        assert first == second


class TestDigestPayload:
    def test_same_payload_same_digest_regardless_of_order(self) -> None:
        assert digest_payload({"a": 1, "b": 2}) == digest_payload({"b": 2, "a": 1})

    def test_different_payload_different_digest(self) -> None:
        assert digest_payload({"a": 1}) != digest_payload({"a": 2})

    def test_digest_is_sha256_of_canonical_bytes(self) -> None:
        payload = {"k": "值"}
        assert digest_payload(payload) == hashlib.sha256(canonical_json(payload)).hexdigest()

    def test_list_order_is_significant(self) -> None:
        """列表是**有序**的：排序会掩盖真实的顺序变更，所以不能顺手排掉。"""
        assert digest_payload([1, 2]) != digest_payload([2, 1])


class TestSha256:
    def test_hex_matches_hashlib(self) -> None:
        assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()

    def test_file_digest_equals_content_digest(self, tmp_path: Path) -> None:
        target = tmp_path / "payload.bin"
        target.write_bytes(b"hello world")
        assert sha256_file(target) == sha256_hex(b"hello world")

    def test_file_digest_streams_large_content(self, tmp_path: Path) -> None:
        """跨块边界的读取必须与一次性哈希一致（流式实现最容易在这里出错）。"""
        payload = bytes(range(256)) * 8192  # 2 MiB，跨过 1 MiB 的块大小
        target = tmp_path / "large.bin"
        target.write_bytes(payload)
        assert sha256_file(target) == sha256_hex(payload)

    def test_empty_file_digest(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.bin"
        target.write_bytes(b"")
        assert sha256_file(target) == sha256_hex(b"")


class TestCombineDigests:
    def test_insertion_order_does_not_matter(self) -> None:
        first = combine_digests({"styles": "a", "page_setup": "b", "numbering": "c"})
        second = combine_digests({"numbering": "c", "styles": "a", "page_setup": "b"})
        assert first == second

    def test_different_values_different_result(self) -> None:
        assert combine_digests({"a": "1"}) != combine_digests({"a": "2"})

    def test_framing_prevents_silent_collision(self) -> None:
        """不长度前缀分帧的话，这两种完全不同的结构会撞成同一个摘要。"""
        assert combine_digests({"ab": "c"}) != combine_digests({"a": "bc"})

    def test_section_rename_is_a_change(self) -> None:
        assert combine_digests({"styles": "x"}) != combine_digests({"style": "x"})

    def test_empty_mapping_is_well_defined(self) -> None:
        assert combine_digests({}) == sha256_hex(b"")


class TestDigestEntries:
    def test_order_independent(self) -> None:
        first = digest_entries([("a.txt", b"1"), ("b.txt", b"2")])
        second = digest_entries([("b.txt", b"2"), ("a.txt", b"1")])
        assert first == second

    def test_backslash_paths_normalized_to_posix(self) -> None:
        """同一份模板包在 Windows 与 Linux 上必须得到同一个摘要。"""
        assert digest_entries([("contract\\styles.json", b"x")]) == digest_entries(
            [("contract/styles.json", b"x")]
        )

    def test_path_and_content_are_not_interchangeable(self) -> None:
        assert digest_entries([("ab", b"c")]) != digest_entries([("a", b"bc")])

    def test_content_change_changes_digest(self) -> None:
        assert digest_entries([("a.txt", b"1")]) != digest_entries([("a.txt", b"1 ")])
