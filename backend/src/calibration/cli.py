#!/usr/bin/env python3
"""命令行入口：跑内置演示或对自定义 JSON 载荷执行校准。

用法：
  python -m src.calibration.cli demo                 # 内置演示数据，打印完整结果
  python -m src.calibration.cli run payload.json    # 自定义载荷
  python -m src.calibration.cli serve [--port PORT] [--db PATH]  # 启动 Web 服务
"""

import argparse
import json
import os
import sys

from src.calibration import demo, engine
from src.calibration.server import main as serve_main


def _print(payload: dict) -> None:
    frozen = engine.validate_payload(payload)
    result = engine.run_pipeline(frozen)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="试展跨场评分校准")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="运行内置演示数据")
    p_run = sub.add_parser("run", help="对 JSON 载荷文件执行校准")
    p_run.add_argument("payload_file")
    p_serve = sub.add_parser("serve", help="启动 HTTP 服务与页面")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    if args.cmd == "demo":
        _print(demo.build_payload())
        return 0
    if args.cmd == "run":
        with open(args.payload_file, "r", encoding="utf-8") as fh:
            _print(json.load(fh))
        return 0
    if args.cmd == "serve":
        if args.port:
            os.environ["CALIBRATION_PORT"] = str(args.port)
        if args.db:
            os.environ["CALIBRATION_DB"] = args.db
        serve_main()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
