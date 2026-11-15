"""项目私有的本地 OCR 引擎：PaddleOCR-json / RapidOCR-json。

不依赖用户安装的 Umi-OCR，也不占用它的 HTTP 端口：直接启动项目
`vendor/ocr_engines/` 下的引擎子进程，通过 stdin/stdout JSON 管道通信。
用户自己的 OCR 软件可以照常使用，两者互不影响。

- PaddleOCR-json：速度和准确率更好，启动参数 `--enable_mkldnn=true`。
- RapidOCR-json：体积小、内存低，不支持 mkldnn。
- 截图以 base64 传入，不落盘；进程退出时立即结束引擎。

用法：
    python -m backend.local_ocr --check
    python -m backend.local_ocr --engine paddle --image data/pet_cached.png
    python -m backend.local_ocr --engine rapid --image data/pet_cached.png
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT_DIR, configure_stdio

DEFAULT_ENGINE_ROOT = ROOT_DIR / "vendor" / "ocr_engines"
DEFAULT_PADDLE_DIR = DEFAULT_ENGINE_ROOT / "paddle"
DEFAULT_RAPID_DIR = DEFAULT_ENGINE_ROOT / "rapid"
DEFAULT_TIMEOUT = 30.0


class LocalOCRError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalOCRInfo:
    engine: str
    engine_dir: str
    mkldnn: bool | None
    cpu_threads: int


def _default_threads() -> int:
    return max(1, min(16, os.cpu_count() or 4))


def parse_ocr_response(response: dict) -> str:
    code = int(response.get("code", -1))
    if code == 101:
        return ""
    if code != 100:
        raise LocalOCRError(
            f"OCR 失败（code={code}）：{response.get('data')}"
        )
    data = response.get("data", [])
    if isinstance(data, str):
        return data
    parts: list[str] = []
    for item in data:
        if isinstance(item, dict):
            parts.append(str(item.get("text", "")))
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(part for part in parts if part)


def _exe_for(engine: str, directory: Path) -> Path:
    names = (
        ("PaddleOCR-json.exe", "PaddleOCR_json.exe")
        if engine == "paddle"
        else ("RapidOCR-json.exe", "RapidOCR_json.exe")
    )
    for name in names:
        path = directory / name
        if path.exists():
            return path
    raise LocalOCRError(
        f"找不到 {engine} 引擎：{directory}\n"
        "请把 Umi-OCR 插件目录复制到 vendor/ocr_engines/"
        f"{engine}/，或设置 "
        f"LOCAL_OCR_{engine.upper()}_DIR。"
    )


def resolve_engine_dir(
    engine: str, override: str | Path | None = None
) -> tuple[str, Path]:
    if override:
        directory = Path(override)
        if engine == "auto":
            for candidate in ("paddle", "rapid"):
                try:
                    _exe_for(candidate, directory)
                    return candidate, directory
                except LocalOCRError:
                    continue
            raise LocalOCRError(f"{directory} 下没有可用的 OCR 引擎")
        _exe_for(engine, directory)
        return engine, directory

    if engine == "auto":
        paddle = Path(
            os.getenv("LOCAL_OCR_PADDLE_DIR", DEFAULT_PADDLE_DIR)
        )
        rapid = Path(
            os.getenv("LOCAL_OCR_RAPID_DIR", DEFAULT_RAPID_DIR)
        )
        for candidate, directory in (
            ("paddle", paddle),
            ("rapid", rapid),
        ):
            if directory.exists():
                try:
                    _exe_for(candidate, directory)
                    return candidate, directory
                except LocalOCRError:
                    continue
        raise LocalOCRError(
            f"没有可用的本地 OCR 引擎。请把 PaddleOCR-json / "
            f"RapidOCR-json 插件目录放到 {DEFAULT_ENGINE_ROOT}/ 下。"
        )

    env_name = f"LOCAL_OCR_{engine.upper()}_DIR"
    directory = Path(
        os.getenv(env_name)
        or (DEFAULT_PADDLE_DIR if engine == "paddle" else DEFAULT_RAPID_DIR)
    )
    _exe_for(engine, directory)
    return engine, directory


class LocalOCREngine:
    def __init__(
        self,
        engine: str = "auto",
        *,
        engine_dir: str | Path | None = None,
        mkldnn: bool = True,
        cpu_threads: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.engine, self.engine_dir = resolve_engine_dir(
            engine, engine_dir
        )
        self.mkldnn = (
            bool(mkldnn) if self.engine == "paddle" else None
        )
        self.cpu_threads = cpu_threads or _default_threads()
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def info(self) -> LocalOCRInfo:
        return LocalOCRInfo(
            engine=self.engine,
            engine_dir=str(self.engine_dir),
            mkldnn=self.mkldnn,
            cpu_threads=self.cpu_threads,
        )

    def _command(self) -> list[str]:
        exe = _exe_for(self.engine, self.engine_dir)
        if self.engine == "paddle":
            return [
                str(exe),
                f"--enable_mkldnn={'true' if self.mkldnn else 'false'}",
                "--config_path=models/config_chinese.txt",
                f"--cpu_threads={self.cpu_threads}",
                "--limit_side_len=960",
            ]
        return [
            str(exe),
            "--models=models",
            "--det=ch_PP-OCRv3_det_infer.onnx",
            "--cls=ch_ppocr_mobile_v2.0_cls_infer.onnx",
            "--rec=rec_ch_PP-OCRv4_infer.onnx",
            "--keys=dict_chinese.txt",
            f"--numThread={self.cpu_threads}",
            "--ensureAscii=1",
        ]

    def start(self) -> None:
        if self._process is not None:
            return
        command = self._command()
        creationflags = 0
        startupinfo = None
        if sys.platform == "win32":
            creationflags = getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        self._process = subprocess.Popen(
            command,
            cwd=str(self.engine_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        self._wait_ready()

    def _wait_ready(self) -> None:
        assert self._process is not None
        messages: queue.Queue[bytes] = queue.Queue()

        def read_lines() -> None:
            assert self._process is not None
            assert self._process.stdout is not None
            for line in iter(self._process.stdout.readline, b""):
                messages.put(line)
                if b"OCR init completed" in line:
                    return

        thread = threading.Thread(target=read_lines, daemon=True)
        thread.start()
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise LocalOCRError(
                    f"{self.engine} OCR 引擎启动超时"
                )
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty:
                self.close()
                raise LocalOCRError(
                    f"{self.engine} OCR 引擎启动超时"
                ) from None
            if b"OCR init completed" in line:
                return
            if not line and self._process.poll() is not None:
                code = self._process.returncode
                self.close()
                raise LocalOCRError(
                    f"{self.engine} OCR 引擎启动失败，退出码 {code}"
                )

    def ocr_png(self, png: bytes) -> str:
        self.start()
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        payload = json.dumps(
            {"image_base64": base64.b64encode(png).decode("ascii")},
            ensure_ascii=True,
        ) + "\n"
        with self._lock:
            if self._process.poll() is not None:
                raise LocalOCRError("OCR 引擎已退出")
            try:
                self._process.stdin.write(payload.encode("utf-8"))
                self._process.stdin.flush()
                raw = self._process.stdout.readline()
            except (BrokenPipeError, OSError) as exc:
                raise LocalOCRError(
                    f"OCR 引擎通信失败：{exc}"
                ) from exc
        if not raw:
            raise LocalOCRError("OCR 引擎没有返回结果")
        try:
            response = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise LocalOCRError(
                f"OCR 引擎返回非 JSON：{raw[:200]!r}"
            ) from exc
        return parse_ocr_response(response)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if sys.platform == "win32":
            # 引擎可能再派生子进程，/T 确保整棵进程树都结束。
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="项目私有 OCR 引擎（PaddleOCR-json / RapidOCR-json）"
    )
    parser.add_argument(
        "--engine", choices=["auto", "paddle", "rapid"], default="auto"
    )
    parser.add_argument("--engine-dir", help="引擎目录")
    parser.add_argument(
        "--no-mkldnn", action="store_true", help="Paddle 不启用 mkldnn"
    )
    parser.add_argument(
        "--cpu-threads", type=int, default=None, help="OCR 线程数"
    )
    parser.add_argument("--image", help="测试图片路径")
    parser.add_argument("--check", action="store_true", help="只检查配置")
    args = parser.parse_args(argv)

    try:
        engine = LocalOCREngine(
            args.engine,
            engine_dir=args.engine_dir,
            mkldnn=not args.no_mkldnn,
            cpu_threads=args.cpu_threads,
        )
    except Exception as exc:
        print(f"OCR 引擎检查失败：{exc}", file=sys.stderr)
        return 1

    info = engine.info()
    print(
        f"引擎：{info.engine}\n"
        f"目录：{info.engine_dir}\n"
        f"mkldnn："
        f"{'开' if info.mkldnn else '关' if info.mkldnn is False else '不适用'}\n"
        f"线程：{info.cpu_threads}"
    )
    if args.check or not args.image:
        return 0

    try:
        text = engine.ocr_png(Path(args.image).read_bytes())
    except Exception as exc:
        print(f"OCR 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
