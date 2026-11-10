# 许可证 / License: GPL-3.0-or-later; see LICENSE.
"""恢复解析结果 JSON，保留源文件。 / Recover parsed JSON results without changing the source."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ios_gpu_counter import recover_result_json


def main():
    # 重定向时也使用 UTF-8，保持 Windows 中文输出一致。
    # Use UTF-8 for redirected streams too, preserving Chinese output on Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = recover_result_json(args.source, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"恢复失败 / Recovery failed: {exc}", file=sys.stderr)
        return 1
    print(f"已恢复 / Recovered: {result.path.resolve()} (status={result.status})")
    # 恢复成功仍不代表采集完整；使用非零退出码，避免被自动化误判。
    # Successful recovery does not prove capture completeness; keep a nonzero exit code.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
