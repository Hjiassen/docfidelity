"""docfidelity —— 模板与体例驱动的通用文档撰写引擎。

本包是**内核**：零 Web 依赖、零 DB 依赖，可在无 HTTP、无数据库的环境中完整运行
（决策四，见 ``scripts/check_kernel_no_web.py``）。

内核分层（自下而上，上层可依赖下层，反向禁止）：

    contracts  ← 依赖之根，只依赖 pydantic
    platform   ← 路径 / 哈希 / 日志等纯基础设施
    model      ← 领域模型注册与投影求值
    ooxml      ← 唯一 OOXML 读写出口
    ingest     ← 事实源接入
    registry   ← 模板入库解析与契约查询
    compose    ← 内容 IR 装配
    render     ← IR + 模板包 -> docx
    qa         ← 门禁判定与报告
    pipeline   ← 阶段机与作业编排
    agents     ← 决策层（只做决策，不做 OOXML）

对外交付形态（``server`` / ``web``）不在此包内。
"""

__version__ = "0.0.1"

__all__ = ["__version__"]
