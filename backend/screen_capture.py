"""自动读屏：截取前台窗口，本地 OCR、脱敏后写入 segment。

设计取舍：
- 默认只读当前前台窗口，而不是整块屏幕，避免把背景里的密码管理器/网银
  一起识别进去；需要整屏时用 --full-screen。
- 截图只在内存中处理，不落盘；OCR 原文不落库，只有脱敏后的文本写入
  segment（source='screen', processed=0）。
- 密码/密钥/卡号等敏感行整行丢弃；其余文本复用 scrub.mask 脱敏。
- 内容哈希和文本相似度双重去重，避免同一屏反复入库。

用法：
    python -m backend.screen_capture --once
    python -m backend.screen_capture                    # 默认每 2 秒检查一次画面
    python -m backend.screen_capture --dry-run --show-text --once
    python -m backend.screen_capture --full-screen
    python -m backend.screen_capture --self-test
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QTimer
from PyQt6.QtGui import QFont, QGuiApplication, QImage
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from .capture_text import (
    CaptureIngestResult as ScreenIngestResult,
    drop_sensitive_lines,
    ingest_captured_text,
    normalize_captured_text as normalize_ocr_text,
    text_similarity,
)
from .config import ROOT_DIR, configure_stdio, get_db_path
from .db import connect
from .local_ocr import LocalOCREngine
from .umi_ocr import UmiOCRClient, UmiOCRError

DEFAULT_INTERVAL = 2.0
DEFAULT_MIN_CHARS = 20
DEFAULT_MAX_CHARS = 4000
SIGNATURE_THRESHOLD = 10  # 16x16 aHash 的汉明距离阈值
DEDUP_THRESHOLD = 0.9
DEFAULT_EXCLUSIONS_PATH = ROOT_DIR / "data" / "screen_exclusions.json"

DEFAULT_TITLE_KEYWORDS = (
    "program manager",
    "keepass",
    "bitwarden",
    "1password",
    "lastpass",
    "dashlane",
    "password",
    "密码",
    "口令",
    "银行",
    "网银",
    "支付",
    "支付宝",
    "微信支付",
    "incognito",
    "inprivate",
    "无痕",
    "隐私浏览",
    "private browsing",
    "login",
    "sign in",
    "登录",
    "登陆",
    "验证码",
    "verification code",
)
DEFAULT_PROCESSES = (
    "keepass",
    "keepassxc",
    "bitwarden",
    "1password",
    "lastpass",
    "dashlane",
    "authy",
)

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowRect.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetClientRect.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    _user32.GetClientRect.restype = wintypes.BOOL
    _user32.ClientToScreen.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.POINT),
    ]
    _user32.ClientToScreen.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.GetShellWindow.restype = wintypes.HWND
    _user32.OpenInputDesktop.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
else:  # 便于在非 Windows 上导入并跑纯函数测试
    _user32 = None
    _kernel32 = None


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    rect: tuple[int, int, int, int]  # client area: left, top, width, height
    minimized: bool = False


@dataclass(frozen=True)
class ExclusionRules:
    title_keywords: tuple[str, ...]
    processes: tuple[str, ...]


def _dedupe(items) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item).strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            result.append(value)
    return tuple(result)


def load_exclusions(
    path: Path | None = None,
    extra_titles=(),
    extra_processes=(),
) -> ExclusionRules:
    titles = list(DEFAULT_TITLE_KEYWORDS)
    processes = list(DEFAULT_PROCESSES)
    path = path or DEFAULT_EXCLUSIONS_PATH
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            titles.extend(data.get("title_keywords", []))
            processes.extend(data.get("processes", []))
        except (OSError, ValueError, AttributeError) as exc:
            print(f"读取排除规则失败，使用默认值：{exc}", file=sys.stderr)
    titles.extend(extra_titles)
    processes.extend(extra_processes)
    return ExclusionRules(_dedupe(titles), _dedupe(processes))


def is_excluded(info: WindowInfo, rules: ExclusionRules) -> str | None:
    title = info.title.lower()
    process = info.process.lower()
    for keyword in rules.title_keywords:
        if keyword.lower() in title:
            return f"窗口标题包含「{keyword}」"
    for name in rules.processes:
        if name.lower() in process:
            return f"进程 {info.process}"
    return None


def _process_name(pid: int) -> str:
    if _kernel32 is None or not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if _kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return Path(buffer.value).name
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def foreground_window_info() -> WindowInfo | None:
    if _user32 is None:
        raise RuntimeError("屏幕读取目前只支持 Windows")
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    if hwnd == _user32.GetShellWindow():
        return None
    length = _user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, title_buffer, length + 1)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    client = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None
    width = client.right - client.left
    height = client.bottom - client.top
    if width <= 0 or height <= 0:
        return None
    origin = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    if not _user32.IsWindowVisible(hwnd):
        return None
    return WindowInfo(
        hwnd=int(hwnd),
        title=title_buffer.value,
        process=_process_name(pid.value),
        rect=(origin.x, origin.y, width, height),
        minimized=bool(_user32.IsIconic(hwnd)),
    )


def session_locked() -> bool:
    if _user32 is None:
        return False
    DESKTOP_SWITCHDESKTOP = 0x0100
    handle = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not handle:
        return True
    _user32.CloseDesktop(handle)
    return False


def _image_to_png(image: QImage) -> bytes:
    array = QByteArray()
    buffer = QBuffer(array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(array)


def image_signature(image: QImage) -> int:
    if image.isNull():
        return 0
    small = image.scaled(
        16,
        16,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_Grayscale8)
    values = [
        small.pixelColor(x, y).lightness()
        for y in range(16)
        for x in range(16)
    ]
    mean = sum(values) / len(values)
    signature = 0
    for index, value in enumerate(values):
        if value >= mean:
            signature |= 1 << index
    return signature


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


_ocr_engine = None


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        from winrt.windows.media.ocr import OcrEngine

        _ocr_engine = OcrEngine.try_create_from_user_profile_languages()
        if _ocr_engine is None:
            languages = [
                item.language_tag
                for item in OcrEngine.available_recognizer_languages
            ]
            raise RuntimeError(
                "Windows OCR 没有可用语言包。请在 Windows 设置中安装"
                f"「中文（简体）」等 OCR 语言。当前可用：{languages}"
            )
    return _ocr_engine


async def _ocr_png_async(png: bytes) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import (
        DataWriter,
        InMemoryRandomAccessStream,
    )

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await _get_ocr_engine().recognize_async(bitmap)
    return result.text


def windows_ocr_png(png: bytes) -> str:
    return asyncio.run(_ocr_png_async(png))


# 兼容旧名字；实际默认后端在 main() 中选择 Umi-OCR。
ocr_png = windows_ocr_png


def _screen_origin(screen) -> tuple[int, int]:
    ratio = screen.devicePixelRatio() or 1.0
    geometry = screen.geometry()
    return round(geometry.x() * ratio), round(geometry.y() * ratio)


def capture_window_images(
    info: WindowInfo, *, full_screen: bool = False
) -> list[tuple[str, QImage]]:
    """截取屏幕后裁剪到前台窗口。

    直接 grabWindow(hwnd) 对 Windows Terminal 等硬件加速窗口会得到空白，
    所以统一截屏再按窗口矩形裁剪。
    """
    if full_screen:
        images: list[tuple[str, QImage]] = []
        for screen in QGuiApplication.screens():
            pixmap = screen.grabWindow(0)
            if not pixmap.isNull():
                images.append((screen.name(), pixmap.toImage()))
        return images

    left, top, width, height = info.rect
    center_x = left + width / 2
    center_y = top + height / 2
    for screen in QGuiApplication.screens():
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            continue
        origin_x, origin_y = _screen_origin(screen)
        if not (
            origin_x <= center_x < origin_x + pixmap.width()
            and origin_y <= center_y < origin_y + pixmap.height()
        ):
            continue
        x = max(0, left - origin_x)
        y = max(0, top - origin_y)
        w = min(width, pixmap.width() - x)
        h = min(height, pixmap.height() - y)
        if w > 0 and h > 0:
            cropped = pixmap.toImage().copy(x, y, w, h)
            return [(info.process or info.title, cropped)]

    return []


def ingest_screen_text(conn, text: str, **kwargs) -> ScreenIngestResult:
    return ingest_captured_text(conn, text, source="screen", **kwargs)


class ScreenReader:
    def __init__(
        self,
        conn,
        *,
        full_screen: bool = False,
        min_chars: int = DEFAULT_MIN_CHARS,
        max_chars: int = DEFAULT_MAX_CHARS,
        exclusions: ExclusionRules | None = None,
        known_names=(),
        dry_run: bool = False,
        verbose: bool = False,
        ocr_func=None,
    ):
        self.conn = conn
        self.full_screen = full_screen
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.exclusions = exclusions or load_exclusions()
        self.known_names = tuple(known_names)
        self.dry_run = dry_run
        self.verbose = verbose
        self.ocr_func = ocr_func or windows_ocr_png
        self.last_signatures: tuple[int, ...] | None = None
        self.last_context: str | None = None

    def capture_once(self) -> tuple[str, ScreenIngestResult | None]:
        if session_locked():
            return "locked", None
        info = foreground_window_info()
        if info is None:
            return "no-window", None
        if info.minimized:
            return "minimized", None
        reason = is_excluded(info, self.exclusions)
        if reason is not None:
            return f"excluded: {reason}", None

        images = capture_window_images(
            info, full_screen=self.full_screen
        )
        if not images:
            return "capture-failed", None

        signatures = tuple(
            image_signature(image) for _, image in images
        )
        context = f"{info.process}|{info.title}"
        if self.last_signatures is not None and context == self.last_context:
            if all(
                _hamming(old, new) <= SIGNATURE_THRESHOLD
                for old, new in zip(self.last_signatures, signatures)
            ):
                return "unchanged", None

        texts: list[str] = []
        failures = 0
        for _, image in images:
            try:
                text = self.ocr_func(_image_to_png(image))
            except Exception as exc:
                failures += 1
                print(f"OCR 失败：{exc}", file=sys.stderr)
                continue
            if text.strip():
                texts.append(text)

        if failures and failures == len(images):
            # 不记录当前画面签名，下个周期重试 OCR。
            return "ocr-failed", None

        self.last_signatures = signatures
        self.last_context = context
        raw = "\n\n".join(texts)
        if not raw.strip():
            return "empty", None

        result = ingest_screen_text(
            self.conn,
            raw,
            known_names=self.known_names,
            min_chars=self.min_chars,
            max_chars=self.max_chars,
            dry_run=self.dry_run,
        )
        if result.action in {"written", "dry_run"}:
            prefix = "[dry-run] 将写入" if result.action == "dry_run" else "已写入"
            extra = (
                f"，丢弃敏感行 {result.dropped_lines}"
                if result.dropped_lines
                else ""
            )
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] {prefix} "
                f"screen segment #{result.segment_id or '-'}："
                f"{result.chars} 字符，进程 {info.process or '?'}{extra}"
            )
        elif self.verbose:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] 跳过："
                f"{result.reason or result.action}"
            )
        return result.action, result


def run_self_test(ocr_func=None, label: str = "") -> int:
    """用一个自绘窗口验证截图 + OCR，不读取真实屏幕内容。"""
    ocr_func = ocr_func or windows_ocr_png
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = QWidget()
    window.setWindowTitle("ScreenCaptureSelfTest")
    window.resize(900, 220)
    window.setStyleSheet("background: white;")
    label = QLabel("屏幕读取自检 SCREEN OCR 789", window)
    label.setStyleSheet("color: black;")
    label.setGeometry(30, 60, 850, 100)
    label.setFont(QFont("Microsoft YaHei", 36))
    window.show()
    window.raise_()
    window.activateWindow()

    state = {"ok": False}

    def finish() -> None:
        window.raise_()
        window.activateWindow()
        screen = QGuiApplication.primaryScreen()
        pixmap = screen.grabWindow(int(window.winId()))
        if pixmap.isNull():
            print("截图失败", file=sys.stderr)
            window.close()
            app.exit(1)
            return
        try:
            text = normalize_ocr_text(
                ocr_func(_image_to_png(pixmap.toImage()))
            )
        except Exception as exc:
            print(f"OCR 失败：{exc}", file=sys.stderr)
            window.close()
            app.exit(1)
            return
        print(f"OCR：{text}")
        state["ok"] = "SCREEN" in text.upper() and "屏幕" in text
        window.close()
        app.exit(0 if state["ok"] else 1)

    QTimer.singleShot(800, finish)
    code = app.exec()
    if not state["ok"]:
        print("自检失败：OCR 没有识别出预期文字", file=sys.stderr)
    else:
        print("自检通过")
    return code


def build_ocr(args):
    if args.ocr_backend == "windows":
        return windows_ocr_png, "Windows.Media.Ocr", lambda: None
    if args.ocr_backend == "local":
        engine = LocalOCREngine(
            args.ocr_engine,
            engine_dir=args.ocr_engine_dir,
            mkldnn=not args.no_mkldnn,
            cpu_threads=args.cpu_threads,
        )
        engine.start()
        info = engine.info()
        mkldnn = (
            "开"
            if info.mkldnn
            else "关"
            if info.mkldnn is False
            else "不适用"
        )
        label = (
            f"本地 {info.engine} 引擎 @ {info.engine_dir}；"
            f"mkldnn={mkldnn}，线程={info.cpu_threads}"
        )
        print(f"OCR：{label}")
        return engine.ocr_png, label, engine.close

    client = UmiOCRClient(
        url=args.umi_url,
        engine=args.ocr_engine,
        paddle_url=args.umi_paddle_url,
        rapid_url=args.umi_rapid_url,
        timeout=args.umi_timeout,
        umi_home=args.umi_home,
    )
    info = client.prepare(
        require_mkldnn=not args.no_require_mkldnn
    )
    mkldnn = (
        "开"
        if info.mkldnn
        else "关"
        if info.mkldnn is False
        else "未知/不适用"
    )
    label = (
        f"Umi-OCR {info.engine} @ {info.url}；"
        f"mkldnn={mkldnn}（{info.mkldnn_source}）"
    )
    print(f"OCR：{label}")
    return client.ocr_png, label, lambda: None


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="AI 记忆增强伙伴 - 自动读屏"
    )
    parser.add_argument("--db", help="数据库路径，默认 data/memory.db")
    parser.add_argument(
        "--ocr-backend",
        choices=["local", "umi", "windows"],
        default="local",
        help="OCR 后端：local（默认，项目私有引擎）/ umi / windows",
    )
    parser.add_argument(
        "--ocr-engine",
        choices=["auto", "paddle", "rapid"],
        default="auto",
        help="OCR 引擎；local 默认优先 Paddle，Umi 默认当前插件",
    )
    parser.add_argument(
        "--ocr-engine-dir", help="本地 OCR 引擎目录"
    )
    parser.add_argument(
        "--no-mkldnn",
        action="store_true",
        help="本地 Paddle 不启用 mkldnn",
    )
    parser.add_argument(
        "--cpu-threads", type=int, default=None,
        help="本地 OCR 线程数",
    )
    parser.add_argument("--umi-url", help="Umi-OCR 基础地址")
    parser.add_argument("--umi-paddle-url", help="Paddle 版 Umi-OCR 地址")
    parser.add_argument("--umi-rapid-url", help="Rapid 版 Umi-OCR 地址")
    parser.add_argument(
        "--umi-home",
        help="Umi-OCR 根目录，用于核对 Paddle 的 mkldnn 设置",
    )
    parser.add_argument(
        "--umi-timeout", type=float, default=30.0,
        help="Umi-OCR HTTP 超时秒数",
    )
    parser.add_argument(
        "--no-require-mkldnn",
        action="store_true",
        help="允许 Paddle 的 enable_mkldnn=false",
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL,
        help="两次画面检查的间隔秒数（默认 2；不变时不会 OCR）",
    )
    parser.add_argument(
        "--once", action="store_true", help="只读取一次后退出"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只识别和脱敏，不写数据库"
    )
    parser.add_argument(
        "--show-text", action="store_true", help="打印脱敏后的文本"
    )
    parser.add_argument(
        "--full-screen", action="store_true",
        help="读取整块屏幕（默认只读当前前台窗口）",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="打印跳过原因"
    )
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="追加要排除的窗口标题关键词，可重复",
    )
    parser.add_argument(
        "--exclude-process", action="append", default=[],
        help="追加要排除的进程名，可重复",
    )
    parser.add_argument(
        "--exclusions", type=Path,
        help=f"排除规则 JSON（默认 {DEFAULT_EXCLUSIONS_PATH}）",
    )
    parser.add_argument(
        "--name", action="append", default=[],
        help="额外需要脱敏的真实姓名，可重复",
    )
    parser.add_argument(
        "--min-chars", type=int, default=DEFAULT_MIN_CHARS
    )
    parser.add_argument(
        "--max-chars", type=int, default=DEFAULT_MAX_CHARS
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="用自绘窗口验证截图和 OCR 后退出",
    )
    args = parser.parse_args(argv)

    try:
        ocr_func, ocr_label, close_ocr = build_ocr(args)
    except Exception as exc:
        print(f"OCR 初始化失败：{exc}", file=sys.stderr)
        return 1

    if args.self_test:
        try:
            return run_self_test(ocr_func, ocr_label)
        finally:
            close_ocr()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    exclusions = load_exclusions(
        args.exclusions, args.exclude, args.exclude_process
    )
    conn = connect(args.db or get_db_path())
    reader = ScreenReader(
        conn,
        full_screen=args.full_screen,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
        exclusions=exclusions,
        known_names=tuple(args.name),
        dry_run=args.dry_run,
        verbose=args.verbose,
        ocr_func=ocr_func,
    )

    try:
        while True:
            started = time.monotonic()
            try:
                action, result = reader.capture_once()
            except Exception as exc:
                print(f"读取失败：{exc}", file=sys.stderr)
                action, result = "error", None
            if args.show_text and result is not None and result.masked_text:
                print(result.masked_text)
                print("---")
            if args.once and action not in {"written", "dry_run"}:
                print(f"本次结果：{action}")
            if action == "error" and args.once:
                return 1
            if args.once:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, args.interval - elapsed))
    except KeyboardInterrupt:
        print("已停止。")
    finally:
        close_ocr()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
