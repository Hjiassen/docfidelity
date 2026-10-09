#!/bin/sh
# 容器入口（DD-18.1）。
#
# 只做一件事：**在起进程之前把数据库结构升到 head**（若这个部署配了库）。
#
# 为什么不让服务自己建表：`storage.build_repository` 的文档写过这条纪律——
# 自动建表会让"忘记跑迁移"从一次**明确的启动失败**，变成一次"库结构比代码旧"的
# 运行期诡异行为（列不存在、约束没生效），而后者要在第一个写入请求时才显形。
# 放在入口脚本里，"迁移"仍然是一个**运维动作**（看得见、可跳过、失败会拦住启动），
# 只是不必让人记住多敲一条命令。
#
# 为什么用 `exec`：不 exec 的话 PID 1 是这个 shell，`docker stop` 发的 SIGTERM 落在
# shell 上而不会传给 uvicorn，于是每次停止容器都要等满 10 秒的 kill 超时。worker 进程
# 更在意这一点——它靠 SIGTERM 走收尾路径（见 `server.worker.install_signal_handlers`）。

set -eu

if [ -n "${DOCFIDELITY_DATABASE_URL:-}" ]; then
    echo "[entrypoint] 升级数据库结构"
    python -m server.migrate
else
    echo "[entrypoint] 未配置 DOCFIDELITY_DATABASE_URL：使用进程内仓储（重启即丢作业）"
fi

exec "$@"
