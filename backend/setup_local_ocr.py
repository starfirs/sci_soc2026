"""把 Umi-OCR 的 PaddleOCR-json / RapidOCR-json 插件复制为项目私有引擎。

只复制引擎目录，不启动 Umi-OCR、不改它的设置，也不占用它的端口。
用户自己的 Umi-OCR 可以继续正常使用。

用法：
    python -m backend.setup_local_ocr \
      --paddle-from "D:/Umi-OCR_Paddle/UmiOCR-data/plugins/win7_x64_PaddleOCR-json" \
      --rapid-from "D:/Umi-OCR_Rapid/UmiOCR-data/plugins/win7_x64_RapidOCR-json"
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import ROOT_DIR, configure_stdio
from .local_ocr import DEFAULT_PADDLE_DIR, DEFAULT_RAPID_DIR

ENGINE_EXES = {
    "paddle": ("PaddleOCR-json.exe", "PaddleOCR_json.exe"),
    "rapid": ("RapidOCR-json.exe", "RapidOCR_json.exe"),
}


def _has_engine(directory: Path, engine: str) -> bool:
    return any((directory / name).exists() for name in ENGINE_EXES[engine])


def copy_engine(source: Path, target: Path, engine: str) -> None:
    if not source.exists():
        raise FileNotFoundError(f"源目录不存在：{source}")
    if not _has_engine(source, engine):
        raise FileNotFoundError(
            f"{source} 下没有 {engine} 引擎 exe"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # 复制前清空旧目录，避免残留旧模型/DLL
        shutil.rmtree(target)
    shutil.copytree(source, target)
    if not _has_engine(target, engine):
        raise RuntimeError(f"复制后仍未找到 {engine} 引擎：{target}")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="安装项目私有的 PaddleOCR-json / RapidOCR-json"
    )
    parser.add_argument(
        "--paddle-from",
        type=Path,
        help="Umi-OCR 的 win7_x64_PaddleOCR-json 插件目录",
    )
    parser.add_argument(
        "--rapid-from",
        type=Path,
        help="Umi-OCR 的 win7_x64_RapidOCR-json 插件目录",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查 vendor/ocr_engines 是否就绪",
    )
    args = parser.parse_args(argv)

    if not args.check and not args.paddle_from and not args.rapid_from:
        parser.error("需要 --paddle-from、--rapid-from 或 --check")

    if args.paddle_from:
        copy_engine(args.paddle_from, DEFAULT_PADDLE_DIR, "paddle")
        print(f"已安装 PaddleOCR-json：{DEFAULT_PADDLE_DIR}")
    if args.rapid_from:
        copy_engine(args.rapid_from, DEFAULT_RAPID_DIR, "rapid")
        print(f"已安装 RapidOCR-json：{DEFAULT_RAPID_DIR}")

    print("\n当前私有引擎：")
    for engine, directory in (
        ("paddle", DEFAULT_PADDLE_DIR),
        ("rapid", DEFAULT_RAPID_DIR),
    ):
        state = "OK" if _has_engine(directory, engine) else "缺失"
        print(f"  {engine:<7} {state:<4} {directory}")

    if not any(
        _has_engine(directory, engine)
        for engine, directory in (
            ("paddle", DEFAULT_PADDLE_DIR),
            ("rapid", DEFAULT_RAPID_DIR),
        )
    ):
        print(
            "没有可用引擎。请从 Umi-OCR 解压目录复制插件，或用 "
            "--paddle-from/--rapid-from 指定。",
            file=sys.stderr,
        )
        return 1
    print(f"\n项目根目录：{ROOT_DIR}")
    print("Umi 引擎为 MIT 许可；vendor/ 已被 gitignore。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
