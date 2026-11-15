"""Umi-OCR HTTP 客户端、引擎选择与 mkldnn 检查。

Umi-OCR 的 HTTP 接口本身不能按请求切换引擎：当前引擎由 Umi-OCR 中启用
的插件决定。这里用 URL 配置来支持两种用法：

- 单实例：用户切换 Umi-OCR 插件，本模块用 --ocr-engine 校验当前引擎。
- 双实例：Paddle 版和 Rapid 版各跑一个端口，分别配置
  UMI_OCR_PADDLE_URL / UMI_OCR_RAPID_URL，--ocr-engine 自动选对应端口。

PaddleOCR-json 的 enable_mkldnn 是插件启动参数，不是 OCR 请求参数；
默认值为 true。提供 --home 时会读取 UmiOCR-data/.settings 核对，
--set-mkldnn 可以显式写入（会先备份）。

用法：
    python -m backend.umi_ocr --check
    python -m backend.umi_ocr --engine paddle --home "D:/Umi-OCR_Paddle"
    python -m backend.umi_ocr --engine rapid --rapid-url http://127.0.0.1:1225
    python -m backend.umi_ocr --set-mkldnn --home "D:/Umi-OCR_Paddle"
    python -m backend.umi_ocr --test-image data/shot.png
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import configure_stdio

DEFAULT_URL = "http://127.0.0.1:1224"


class UmiOCRError(RuntimeError):
    pass


@dataclass(frozen=True)
class UmiInfo:
    url: str
    engine: str
    mkldnn: bool | None
    mkldnn_source: str
    options: dict

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "engine": self.engine,
            "mkldnn": self.mkldnn,
            "mkldnn_source": self.mkldnn_source,
            "options": sorted(self.options),
        }


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise UmiOCRError(
            f"Umi-OCR 返回 HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UmiOCRError(
            f"无法连接 Umi-OCR（{url}）：{exc}。"
            "请先启动 Umi-OCR，并在全局设置中开启 HTTP 服务。"
        ) from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise UmiOCRError(
            f"Umi-OCR 返回的不是 JSON：{raw[:200]}"
        ) from exc
    if not isinstance(value, dict):
        raise UmiOCRError("Umi-OCR 返回格式异常")
    return value


def detect_engine(options: dict) -> str:
    keys = set(options)
    if {"ocr.cls", "ocr.limit_side_len"} & keys:
        return "paddle"
    if {"ocr.angle", "ocr.maxSideLen"} & keys:
        return "rapid"
    language = options.get("ocr.language")
    if isinstance(language, dict):
        default = str(language.get("default", ""))
        if "models/" in default:
            return "paddle"
    return "unknown"


def _settings_path(home: str | Path | None) -> Path | None:
    if not home:
        home = os.getenv("UMI_OCR_HOME")
    if not home:
        return None
    root = Path(home)
    candidates = [
        root / "UmiOCR-data" / ".settings",
        root / ".settings",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def read_mkldnn(home: str | Path | None) -> tuple[bool | None, str]:
    settings = _settings_path(home)
    if settings is None:
        return None, "未找到 UmiOCR-data/.settings；Paddle 插件默认开启 mkldnn"
    text = settings.read_text(encoding="utf-8", errors="replace")
    api_match = re.search(r"(?m)^ocr\.api\s*=\s*(.+?)\s*$", text)
    plugin = api_match.group(1).strip() if api_match else ""
    pattern = (
        rf"(?mi)^ocr\.{re.escape(plugin)}\.enable_mkldnn\s*=\s*"
        r"(true|false|1|0)\s*$"
        if plugin
        else r"(?mi)^ocr\.[^.]+\.enable_mkldnn\s*=\s*(true|false|1|0)\s*$"
    )
    match = re.search(pattern, text)
    if match is None:
        return None, f"{settings} 中未找到 enable_mkldnn，Paddle 默认开启"
    enabled = match.group(1).lower() in {"true", "1"}
    return enabled, str(settings)


def set_mkldnn(home: str | Path | None, enabled: bool = True) -> Path:
    settings = _settings_path(home)
    if settings is None:
        raise UmiOCRError(
            "没有找到 UmiOCR-data/.settings，请用 --home 指向 Umi-OCR 根目录"
        )
    text = settings.read_text(encoding="utf-8", errors="replace")
    api_match = re.search(r"(?m)^ocr\.api\s*=\s*(.+?)\s*$", text)
    plugin = api_match.group(1).strip() if api_match else ""
    value = "true" if enabled else "false"
    pattern = re.compile(
        rf"(?mi)^(ocr\.{re.escape(plugin)}\.enable_mkldnn\s*=\s*)"
        r"(true|false|1|0)\s*$"
        if plugin
        else r"(?mi)^(ocr\.[^.]+\.enable_mkldnn\s*=\s*)(true|false|1|0)\s*$"
    )
    if pattern.search(text):
        text = pattern.sub(lambda m: m.group(1) + value, text)
    elif plugin:
        line = f"ocr.{plugin}.enable_mkldnn={value}"
        if api_match:
            text = (
                text[: api_match.end()]
                + f"\n{line}"
                + text[api_match.end() :]
            )
        else:
            text = text.rstrip() + f"\n{line}\n"
    else:
        raise UmiOCRError(
            "settings 中没有 ocr.api，无法确定要修改哪个 Paddle 插件"
        )
    backup = settings.with_suffix(settings.suffix + ".bak")
    backup.write_text(
        settings.read_text(encoding="utf-8", errors="replace"),
        encoding="utf-8",
    )
    settings.write_text(text, encoding="utf-8")
    return settings


class UmiOCRClient:
    def __init__(
        self,
        *,
        url: str | None = None,
        engine: str = "auto",
        paddle_url: str | None = None,
        rapid_url: str | None = None,
        timeout: float = 30.0,
        umi_home: str | None = None,
    ):
        if engine == "auto":
            engine = os.getenv("UMI_OCR_ENGINE", "auto").strip().lower()
        self.requested_engine = engine
        self.paddle_url = paddle_url or os.getenv("UMI_OCR_PADDLE_URL")
        self.rapid_url = rapid_url or os.getenv("UMI_OCR_RAPID_URL")
        self.base_url = (
            url
            or os.getenv("UMI_OCR_URL")
            or DEFAULT_URL
        ).rstrip("/")
        self.timeout = timeout
        self.umi_home = umi_home or os.getenv("UMI_OCR_HOME")
        self.engine = engine
        self.options: dict = {}

    def _url_for_engine(self, engine: str) -> str:
        if engine == "paddle" and self.paddle_url:
            return self.paddle_url.rstrip("/")
        if engine == "rapid" and self.rapid_url:
            return self.rapid_url.rstrip("/")
        return self.base_url

    def get_options(self) -> dict:
        data = _request_json(
            self._url_for_engine(self.requested_engine)
            + "/api/ocr/get_options",
            timeout=self.timeout,
        )
        return data

    def prepare(self, *, require_mkldnn: bool = True) -> UmiInfo:
        url = self._url_for_engine(self.requested_engine)
        options = self.get_options()
        detected = detect_engine(options)
        if detected == "unknown":
            raise UmiOCRError(
                "无法识别 Umi-OCR 当前引擎；请确认 PaddleOCR-json 或 "
                "RapidOCR-json 插件已加载。"
            )
        if (
            self.requested_engine != "auto"
            and detected != self.requested_engine
        ):
            raise UmiOCRError(
                f"你要求 {self.requested_engine}，但 {url} 当前运行的是 "
                f"{detected}。请启动对应引擎，或配置 "
                "UMI_OCR_PADDLE_URL / UMI_OCR_RAPID_URL。"
            )
        self.engine = detected
        self.options = options
        self.base_url = url

        if detected == "rapid":
            mkldnn, source = None, "RapidOCR 不支持 mkldnn"
        else:
            mkldnn, source = read_mkldnn(self.umi_home)
            if require_mkldnn and mkldnn is False:
                raise UmiOCRError(
                    "PaddleOCR 的 enable_mkldnn=false。请运行 "
                    "`python -m backend.umi_ocr --set-mkldnn --home "
                    f"{self.umi_home or '<Umi-OCR目录>'}` 并重启 Umi-OCR，"
                    "或在 Umi-OCR 插件设置中勾选“启用MKL-DNN加速”。"
                )
        return UmiInfo(
            url=url,
            engine=detected,
            mkldnn=mkldnn,
            mkldnn_source=source,
            options=options,
        )

    def ocr_png(
        self, png: bytes, *, options: dict | None = None
    ) -> str:
        payload_options = {"data.format": "text"}
        if options:
            payload_options.update(options)
        response = _request_json(
            self._url_for_engine(self.engine)
            + "/api/ocr",
            method="POST",
            payload={
                "base64": base64.b64encode(png).decode("ascii"),
                "options": payload_options,
            },
            timeout=self.timeout,
        )
        code = int(response.get("code", -1))
        if code == 101:
            return ""
        if code != 100:
            raise UmiOCRError(
                f"Umi-OCR 识别失败（code={code}）："
                f"{response.get('data')}"
            )
        data = response.get("data", "")
        if isinstance(data, str):
            return data
        if isinstance(data, list):
            parts: list[str] = []
            for item in data:
                if isinstance(item, dict):
                    parts.append(
                        str(item.get("text", ""))
                        + str(item.get("end", ""))
                    )
            return "".join(parts)
        return ""


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="Umi-OCR 引擎检查 / mkldnn 配置 / 图片测试"
    )
    parser.add_argument("--url", help="Umi-OCR 基础地址")
    parser.add_argument("--paddle-url", help="Paddle 版 Umi-OCR 地址")
    parser.add_argument("--rapid-url", help="Rapid 版 Umi-OCR 地址")
    parser.add_argument(
        "--engine",
        choices=["auto", "paddle", "rapid"],
        default="auto",
        help="期望的 OCR 引擎",
    )
    parser.add_argument(
        "--home",
        help="Umi-OCR 根目录（用于读取/修改 UmiOCR-data/.settings）",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="HTTP 超时秒数"
    )
    parser.add_argument(
        "--no-require-mkldnn",
        action="store_true",
        help="允许 Paddle 的 mkldnn=false",
    )
    parser.add_argument(
        "--set-mkldnn",
        action="store_true",
        help="把 Paddle 的 enable_mkldnn 写为 true（先备份 .settings）",
    )
    parser.add_argument(
        "--test-image", help="用这个图片测试一次 OCR"
    )
    args = parser.parse_args(argv)

    if args.set_mkldnn:
        try:
            path = set_mkldnn(args.home, True)
        except Exception as exc:
            print(f"设置 mkldnn 失败：{exc}", file=sys.stderr)
            return 1
        print(f"已写入 {path}")
        print("请重启 Umi-OCR，或运行 Umi-OCR.exe --reload 使配置生效。")
        return 0

    client = UmiOCRClient(
        url=args.url,
        engine=args.engine,
        paddle_url=args.paddle_url,
        rapid_url=args.rapid_url,
        timeout=args.timeout,
        umi_home=args.home,
    )
    try:
        info = client.prepare(require_mkldnn=not args.no_require_mkldnn)
    except Exception as exc:
        print(f"Umi-OCR 检查失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
    print(
        f"引擎：{info.engine}；mkldnn："
        f"{'开' if info.mkldnn else '关' if info.mkldnn is False else '未知/不适用'}"
        f"（{info.mkldnn_source}）"
    )

    if args.test_image:
        path = Path(args.test_image)
        try:
            text = client.ocr_png(path.read_bytes())
        except Exception as exc:
            print(f"OCR 失败：{exc}", file=sys.stderr)
            return 1
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
