"""资料完整性检查（不进入业务分析）。

用法：

    python3 tools/check_materials.py [data_dir]

之前这里通过 ``runpy`` 执行测试文件；现在直接调用载入器，输出错误/警告清单，
并在存在错误级问题时以非零码退出。资料目录默认为项目根下的 ``data``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from speed_camera_slowdown_evaluation.loader import (  # noqa: E402
    MaterialSchemaError,
    load_materials,
)


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "data"
    try:
        result = load_materials(data_dir)
    except MaterialSchemaError as exc:
        print(f"资料结构错误：{exc}", file=sys.stderr)
        return 2

    print(json.dumps({"row_counts": result.row_counts}, ensure_ascii=False))
    errors = [issue for issue in result.issues if issue.severity == "error"]
    warnings = [issue for issue in result.issues if issue.severity != "error"]
    for issue in result.issues:
        print(json.dumps(issue.as_dict(), ensure_ascii=False))
    print(
        f"错误 {len(errors)} 条，警告 {len(warnings)} 条；"
        "警告是有效业务事实，不会自动按零事件处理。"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
