"""独立 worker 进程（DD-18.1 的 ``df-worker``）。

为什么要有它：:func:`server.main.create_app` 默认会在**自己的进程里**起一个 worker 线程
（单机一键体验够用），但 ``docker compose`` 的拓扑里 worker 是**独立服务**——它要能单独
重启、单独扩容，而且"某个构建把进程拖住"时不该连带把 HTTP 服务一起重启。

用法::

    python -m server.worker              # 一直跑（容器里的 df-worker 就是这一条）
    python -m server.worker --once       # 把当前排队的作业跑完就退出（自检 / 一次性补跑）

与 api 共用同一套装配
--------------------
两边都走 :func:`server.api.deps.build_state`：同一份 ``DOCFIDELITY_*`` 配置、同一个
``storage_root``、同一套入口点发现的阶段体。**装配分叉是这类进程最容易出的错**——
"api 能构建、worker 说阶段缺失"就是两边各写了一份装配的典型症状，而且两边的日志
各自看都很正常。

为什么不起 uvicorn
------------------
worker 不提供任何 HTTP 面。为了"统一入口"而给它挂一个空的应用，换来的是一个只用于
健康检查的端口和一份多余的依赖；容器里用进程是否存活来判断它，本来就够了。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from collections.abc import Sequence
from typing import Any, Final

from .api.deps import build_state
from .jobs import WorkerLoop
from .settings import ServerSettings

__all__ = ["DEFAULT_IDLE_SECONDS", "LOG_LEVEL_ENV", "install_signal_handlers", "main"]

_LOGGER = logging.getLogger("docfidelity.worker")

#: 空闲时的轮询间隔（秒）。与 :class:`~server.jobs.worker.WorkerLoop` 的缺省一致：
#: 一次构建要几十秒，半秒的调度延迟看不出来，而忙等会白占一个核。
DEFAULT_IDLE_SECONDS: Final = 0.5

#: 日志级别环境变量。**用独立的键名而不是 `DOCFIDELITY_` 前缀**：它不是服务配置
#: （不进 `SettingsView`、不影响产物），只是一个进程级的日志开关。
LOG_LEVEL_ENV: Final = "DOCFIDELITY_LOG_LEVEL"


def install_signal_handlers(stop: threading.Event) -> None:
    """把 SIGINT / SIGTERM 变成"请退出"。

    不用 ``KeyboardInterrupt`` 兜底：容器停容器时发的是 SIGTERM，而它默认会**直接
    终止进程**，于是 ``finally`` 里的收尾（释放项目锁）来不及跑——那会把一个项目锁
    留到租约过期为止，表现为"重启之后这个项目十几分钟不能构建"。
    """

    def _request_stop(signum: int, _frame: Any) -> None:
        _LOGGER.info("收到信号 %s，准备退出", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:  # SIGBREAK 只在 Windows 上存在
            continue
        try:
            signal.signal(number, _request_stop)
        except (ValueError, OSError):  # 非主线程 / 平台不支持
            continue


def main(argv: Sequence[str] | None = None) -> int:
    """进程入口。返回退出码（``0`` = 正常收尾）。"""
    parser = argparse.ArgumentParser(prog="python -m server.worker", description="作业消费者")
    parser.add_argument("--once", action="store_true", help="把当前排队的作业跑完即退出")
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=DEFAULT_IDLE_SECONDS,
        help=f"空闲轮询间隔（秒，默认 {DEFAULT_IDLE_SECONDS}）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get(LOG_LEVEL_ENV, "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = ServerSettings()
    state = build_state(settings=settings)
    missing = state.runner.registry.missing()
    if missing:
        # 与 ``create_app`` 同一条纪律：缺阶段不是本进程的问题，但必须说出来——
        # 否则表现是"作业一直排队、日志一片安静"。
        _LOGGER.warning("流水线阶段未注册，作业将无法推进：%s", sorted(missing))

    if args.once:
        loop = WorkerLoop(state.runner, additional_runner=state.document_runs.run_once)
        count = 0
        while loop.tick():
            count += 1
        _LOGGER.info("一次性补跑完成：处理 %d 轮作业", count)
        return 0

    stop = threading.Event()
    install_signal_handlers(stop)
    loop = WorkerLoop(
        state.runner, idle_seconds=args.idle_seconds, additional_runner=state.document_runs.run_once
    )
    _LOGGER.info("worker 已就绪：storage_root=%s", state.root)
    try:
        while not stop.is_set():
            if not loop.tick():
                stop.wait(args.idle_seconds)
    finally:
        # 此刻不在任何作业里（``tick`` 是同步的），故没有需要等待的收尾；写一行是为了
        # 让"退出"在两个方向上都留下痕迹——排查"谁把它停了"时，日志的最后一句话很重要。
        _LOGGER.info("worker 已退出")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
