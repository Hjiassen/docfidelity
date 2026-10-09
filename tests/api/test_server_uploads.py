"""上传校验的四道闸门（DD-13.5 / 时序 15.1）。

四道闸门的**顺序即语义**，故每条闸门都单独有一组用例，且专门有一组"顺序"用例：
若"大小"先于"文件名"判定，那么 ``../x.docx``（10 字节）与 ``../x.docx``（100 MB）
会得到同一个 ``E_UPLOAD_SIZE``——一次越界尝试被伪装成一次体积超限，
而这两件事的处理方式完全不同（前者要查客户端，后者只需压缩）。

本模块是纯函数（吃 ``UploadProbe``，不碰磁盘），故这些用例不需要任何临时目录。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from server.settings import ServerSettings
from server.uploads import (
    MACRO_MEMBER,
    UPLOAD_PURPOSES,
    UploadProbe,
    probe_upload,
    validate_upload,
    zip_members,
)

pytestmark = pytest.mark.api

DOCX_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF-1.7\n"


def settings(**kwargs: object) -> ServerSettings:
    return ServerSettings(_env_file=None, **kwargs)


def expect_error(
    code: str, probe: UploadProbe, *, config: ServerSettings | None = None
) -> EngineError:
    with pytest.raises(EngineError) as caught:
        validate_upload(probe, config or settings())
    assert caught.value.code == code, caught.value.message
    return caught.value


def docx_bytes(body: bytes = b"payload") -> bytes:
    return DOCX_MAGIC + body


# ---------------------------------------------------------------- 闸门① 文件名
@pytest.mark.parametrize("name", ["", "   "])
def test_empty_filename_is_rejected(name: str) -> None:
    expect_error(ErrorCode.UPLOAD_TYPE, probe_upload(name, docx_bytes()))


@pytest.mark.parametrize(
    "name",
    [
        "../escape.docx",
        "a/b.docx",
        "a\\b.docx",
        "..\\escape.docx",
        "/abs.docx",
        "dir/",
    ],
)
def test_path_separators_and_parent_marks_are_rejected(name: str) -> None:
    """**拒绝**而不是清洗：改写成安全名字会把一次越界尝试藏起来。"""
    payload = docx_bytes()
    error = expect_error(ErrorCode.PATH_ESCAPE, probe_upload(name, payload))
    assert error.where["filename"] == name


def test_filename_check_runs_before_size_check() -> None:
    """顺序断言（见模块文档）：越界尝试不该被报成"体积超限"。"""
    probe = probe_upload("../x.docx", docx_bytes())
    small = settings(max_upload_bytes=1)
    # 文件本身也没超限？—— 让它同时超限，才能证明顺序而不是碰巧。
    expect_error(ErrorCode.PATH_ESCAPE, probe, config=small)


def test_null_byte_in_filename_is_rejected() -> None:
    expect_error(ErrorCode.PATH_ESCAPE, probe_upload("a\x00b.docx", docx_bytes()))


# ------------------------------------------------------------------ 闸门② 大小
def test_empty_payload_is_rejected() -> None:
    """空文件不是"小文件"——它通常意味着客户端上传中途失败了。"""
    expect_error(ErrorCode.UPLOAD_SIZE, probe_upload("t.docx", b""))


def test_oversized_payload_is_rejected_with_both_numbers() -> None:
    config = settings(max_upload_bytes=16)
    probe = probe_upload("t.docx", docx_bytes(b"x" * 64))
    error = expect_error(ErrorCode.UPLOAD_SIZE, probe, config=config)
    assert error.where["size_bytes"] == probe.size
    assert error.where["max_upload_bytes"] == 16


def test_payload_exactly_at_the_limit_is_accepted() -> None:
    """边界是 ``<=``，不是 ``<``：正好等于上限必须能收。"""
    limit = len(DOCX_MAGIC) + 8
    config = settings(max_upload_bytes=limit)
    receipt = validate_upload(probe_upload("t.docx", docx_bytes(b"x" * 8)), config)
    assert receipt.size_bytes == limit


# ------------------------------------------------------------------ 闸门③ 类型
def test_disallowed_suffix_is_rejected() -> None:
    error = expect_error(ErrorCode.UPLOAD_TYPE, probe_upload("t.tar", docx_bytes()))
    assert error.where["suffix"] == ".tar"
    assert ".docx" in error.where["allowed"]


def test_suffix_outside_the_configured_whitelist_is_rejected() -> None:
    """后缀在 ``MAGIC_BY_SUFFIX`` 里但不在部署白名单里 → 仍拒（白名单是配置项）。"""
    config = settings(allowed_upload_suffixes=(".docx",))
    expect_error(ErrorCode.UPLOAD_TYPE, probe_upload("t.pdf", PDF_MAGIC), config=config)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("renamed.docx", PDF_MAGIC),
        ("renamed.pdf", DOCX_MAGIC),
        ("renamed.zip", PDF_MAGIC),
        ("t.docx", b"not a zip at all"),
    ],
)
def test_magic_number_must_match_the_suffix(name: str, payload: bytes) -> None:
    """只信后缀能被改名骗过；只信魔数无法区分 ``.pdf`` 与 ``.zip``（都是容器）。"""
    error = expect_error(ErrorCode.UPLOAD_TYPE, probe_upload(name, payload))
    assert "suffix" in error.where and "head" in error.where


def test_zip_suffix_accepts_an_empty_archive_magic() -> None:
    """空 zip 的魔数是 ``PK\\x05\\x06``（没有中央目录）；它同样是合法 zip。"""
    receipt = validate_upload(probe_upload("t.zip", b"PK\x05\x06" + b"\x00" * 18), settings())
    assert receipt.suffix == ".zip"


def test_suffix_matching_is_case_insensitive() -> None:
    receipt = validate_upload(probe_upload("T.DOCX", docx_bytes()), settings())
    assert receipt.suffix == ".docx"
    assert receipt.content_type.endswith("wordprocessingml.document")


# ------------------------------------------------------------------ 闸门④ 宏
def test_macro_member_is_rejected() -> None:
    probe = probe_upload("t.docx", docx_bytes(), members=("word/document.xml", MACRO_MEMBER))
    error = expect_error(ErrorCode.UPLOAD_TYPE, probe)
    assert error.where["member"] == MACRO_MEMBER


def test_non_macro_members_pass() -> None:
    probe = probe_upload(
        "t.docx", docx_bytes(), members=("word/document.xml", "[Content_Types].xml")
    )
    assert validate_upload(probe, settings()).filename == "t.docx"


def test_macro_gate_runs_after_type_gate() -> None:
    """一个改名成 ``.tar`` 的含宏文件：该报类型错，而不是宏错（先问"是不是我能收的"）。"""
    probe = probe_upload("t.tar", docx_bytes(), members=(MACRO_MEMBER,))
    expect_error(ErrorCode.UPLOAD_TYPE, probe)


# -------------------------------------------------------------- 回执与探测
def test_receipt_carries_everything_the_ledger_needs() -> None:
    payload = docx_bytes(b"body")
    receipt = validate_upload(probe_upload("t.docx", payload), settings())
    assert receipt.filename == "t.docx"
    assert receipt.suffix == ".docx"
    assert receipt.size_bytes == len(payload)
    assert receipt.digest == probe_upload("t.docx", payload).digest


def test_digest_is_over_the_whole_payload_not_the_head() -> None:
    """回归：只哈希头部 8 字节会让**所有** ``.docx`` 得到同一个摘要。

    它们都以 ``PK\\x03\\x04`` 开头，于是"同一份文件重复入库"的判定会把两份完全
    不同的模板判成同一份。这条用例钉住"摘要覆盖全文"。
    """
    # 前 8 字节刻意相同（含 4 字节魔数），差异落在头部之外——这才是原缺陷的形状。
    first = probe_upload("a.docx", docx_bytes(b"A" * 4 + b"tail-1"))
    second = probe_upload("b.docx", docx_bytes(b"A" * 4 + b"tail-2"))
    assert first.head == second.head, "前提失效：两份文件的头部不再相同"
    assert first.size == second.size, "前提失效：两份文件不再等长"
    assert first.digest != second.digest


def test_zip_members_reads_the_central_directory() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr(MACRO_MEMBER, b"\x00\x01")
    members = zip_members(buffer.getvalue())
    assert "word/document.xml" in members
    assert MACRO_MEMBER in members


def test_zip_members_returns_empty_for_non_zip() -> None:
    """非 zip / 已损坏 → 空元组（**不抛**）："可解析"是另一档，由受限子进程判定。"""
    assert zip_members(b"%PDF-1.7\nnot a zip") == ()
    assert zip_members(b"") == ()
    assert zip_members(b"PK\x03\x04truncated") == ()


def test_zip_members_output_feeds_the_macro_gate() -> None:
    """两条函数接起来跑一遍：这是 ``services.accept_upload`` 的真实调用形状。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MACRO_MEMBER, b"\x00")
    payload = buffer.getvalue()
    probe = probe_upload("t.docx", payload, members=zip_members(payload))
    assert expect_error(ErrorCode.UPLOAD_TYPE, probe).where["member"] == MACRO_MEMBER


def test_upload_purposes_is_a_closed_set() -> None:
    assert frozenset({"template", "profile", "reference"}) == UPLOAD_PURPOSES
