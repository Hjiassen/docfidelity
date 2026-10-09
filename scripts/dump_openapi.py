"""把服务的 OpenAPI 规格导出到 ``web/openapi.json``。

为什么不让前端直接连运行中的服务拉
----------------------------------
``npm run gen:api`` 若写成"访问 http://127.0.0.1:8000/openapi.json"，
那么每次改接口都得先起服务、再生成——CI 里还得额外起一个进程。
规格本来就由应用对象**算**得出来，直接调 ``app.openapi()`` 即可，
产物落盘后既确定性可复现，也能进版本库供人核对接口变更。

用法：
    PYTHONPATH="src;." python scripts/dump_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "web" / "openapi.json"

#: 前端只消费接口形状，故不必起 worker（也就不必注册真实阶段体）。
_SPARE_WORKER = False


def main() -> int:
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # 延迟导入：必须在上面把 ``src`` 与仓库根放进 ``sys.path`` 之后才成立。
    from server.main import create_app

    app = create_app(start_worker=_SPARE_WORKER)
    spec = app.openapi()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # 固定 ``sort_keys`` 与 ``indent``：好让"接口变了"在 git diff 里看得见，
    # 而不是每次生成都整文件抖动。
    TARGET.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths = len(spec.get("paths", {}))
    print(f"OK OpenAPI 已导出：{TARGET}（{paths} 条路径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
