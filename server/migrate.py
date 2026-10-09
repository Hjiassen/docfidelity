"""把元数据库升到 head（部署动作；DD-13.6 / DD-18.1）。

用法::

    python -m server.migrate            # 读 DOCFIDELITY_DATABASE_URL
    python -m server.migrate --url sqlite:///./data/df.db

为什么单独做成一个模块
----------------------
① 容器入口要用它（`deploy/entrypoint.sh`）：迁移必须**发生在进程启动之前**，
   而不是在应用工厂里顺手做掉——后者会把"库结构不对"和"服务起不来"绑在一起，
   排障时看不出是谁的问题。
② 它得能被单独跑：`--url` 让运维可以对一个不在配置里的库执行迁移（换库、修库）。
③ 它是**可跳过**的：没配库的部署（本机开发、单机演示）不该因为"迁移没跑"而报错，
   故这条路径是"说一句就返回 0"，不是失败。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .settings import ServerSettings
from .storage import run_migrations

__all__ = ["main"]

_NO_DATABASE: str = "未配置数据库连接串：本部署使用进程内仓储（无迁移可跑）"


def main(argv: Sequence[str] | None = None) -> int:
    """返回退出码。**配了库却迁移失败 = 非零退出**，让编排层拦住启动。"""
    parser = argparse.ArgumentParser(
        prog="python -m server.migrate", description="升级元数据库结构"
    )
    parser.add_argument("--url", default=None, help="连接串；缺省取 DOCFIDELITY_DATABASE_URL")
    parser.add_argument("--revision", default="head", help="目标版本（默认 head）")
    args = parser.parse_args(argv)

    url = args.url or ServerSettings().database_url
    if not url:
        print(_NO_DATABASE)
        return 0

    run_migrations(url, revision=args.revision)
    print(f"已升级到 {args.revision}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
