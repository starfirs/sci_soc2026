"""录音链路：麦克风 + 系统声音 → Vosk 本地转写 → 脱敏 → 写入 segment。

设计取舍：
- 默认同时监听麦克风和系统声音回环；同一个声音被两边录到时，按文本相似度去重。
- 音频只存在内存里，转写后丢弃，不落盘。
- Vosk 使用 16 kHz 单声道 PCM；模型默认 models/vosk-model-small-cn-0.22。
- 转写文本走 capture_text 的共享清洗/脱敏/入库逻辑，和屏幕读取一致。
- Vosk 自带的端点检测负责切句和过滤静音，不额外引入 VAD 依赖。

用法：
    python -m backend.audio_capture --list-devices
    python -m backend.audio_capture --wav data/asr_test.wav --dry-run --show-text
    python -m backend.audio_capture --once --seconds 10
    python -m backend.audio_capture --source both
    python -m backend.audio_capture --self-test
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import warnings
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .capture_text import CaptureIngestResult, ingest_captured_text
from .config import ROOT_DIR, configure_stdio, get_db_path
from .db import connect

SAMPLE_RATE = 16000
BLOCK_FRAMES = 1600  # 0.1 秒
DEFAULT_SECONDS = 10.0
DEFAULT_MIN_CHARS = 10
DEFAULT_MAX_CHARS = 4000
DEFAULT_MODEL_PATH = ROOT_DIR / "models" / "vosk-model-small-cn-0.22"

SOURCE_LABELS = {
    "microphone": "麦克风",
    "system": "系统声音",
}


@dataclass(frozen=True)
class AudioDevice:
    kind: str
    name: str
    device: object


def load_model(model_path: Path):
    import vosk

    vosk.SetLogLevel(-1)
    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 Vosk 模型：{path}\n"
            "请先下载中文小模型并解压到 models/，见 USAGE.md 的录音章节。"
        )
    return vosk.Model(str(path))


def list_devices() -> int:
    import soundcard as sc

    print("扬声器：")
    for speaker in sc.all_speakers():
        print(f"  - {speaker.name}")
    print("\n输入设备（含回环）：")
    for mic in sc.all_microphones(include_loopback=True):
        kind = "回环" if mic.isloopback else "麦克风"
        print(f"  - [{kind}] {mic.name}")
    default_speaker = sc.default_speaker()
    default_mic = sc.default_microphone()
    print(
        f"\n默认扬声器：{default_speaker.name if default_speaker else '无'}"
    )
    print(f"默认麦克风：{default_mic.name if default_mic else '无'}")
    return 0


def _find_speaker(sc, name: str):
    for speaker in sc.all_speakers():
        if name.lower() in speaker.name.lower():
            return speaker
    return None


def _find_microphone(sc, name: str, *, loopback: bool | None = None):
    for mic in sc.all_microphones(include_loopback=True):
        if loopback is not None and mic.isloopback != loopback:
            continue
        if name.lower() in mic.name.lower():
            return mic
    return None


def resolve_devices(
    source: str,
    *,
    mic_name: str | None = None,
    speaker_name: str | None = None,
) -> list[AudioDevice]:
    import soundcard as sc

    try:
        warnings.filterwarnings(
            "ignore", category=sc.SoundcardRuntimeWarning
        )
    except AttributeError:
        pass

    devices: list[AudioDevice] = []
    if source in {"microphone", "both"}:
        try:
            mic = (
                _find_microphone(sc, mic_name, loopback=False)
                if mic_name
                else sc.default_microphone()
            )
        except Exception as exc:
            print(f"打开麦克风失败：{exc}", file=sys.stderr)
            mic = None
        if mic is not None:
            devices.append(
                AudioDevice("microphone", mic.name, mic)
            )
        else:
            print("没有找到可用的麦克风。", file=sys.stderr)

    if source in {"system", "both"}:
        try:
            speaker = (
                _find_speaker(sc, speaker_name)
                if speaker_name
                else sc.default_speaker()
            )
        except Exception as exc:
            print(f"打开扬声器失败：{exc}", file=sys.stderr)
            speaker = None
        loopback = None
        if speaker is not None:
            try:
                loopback = sc.get_microphone(
                    id=str(speaker.name), include_loopback=True
                )
            except Exception:
                loopback = _find_microphone(
                    sc, speaker.name.split(" (")[0], loopback=True
                )
        if loopback is not None:
            devices.append(
                AudioDevice("system", loopback.name, loopback)
            )
        else:
            print("没有找到系统声音回环设备。", file=sys.stderr)

    return devices


class CaptureWorker(threading.Thread):
    def __init__(self, device: AudioDevice, model, out_queue, stop_event):
        super().__init__(daemon=True)
        self.audio_device = device
        self.model = model
        self.out_queue = out_queue
        self.stop_event = stop_event
        self.error: Exception | None = None

    def _emit(self, text: str) -> None:
        text = text.strip()
        if text:
            self.out_queue.put(
                (
                    "text",
                    self.audio_device.kind,
                    text,
                    datetime.now().isoformat(timespec="seconds"),
                )
            )

    def run(self) -> None:
        import numpy as np
        import vosk

        try:
            recognizer = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
            with self.audio_device.device.recorder(
                samplerate=SAMPLE_RATE,
                channels=1,
                blocksize=BLOCK_FRAMES,
            ) as recorder:
                while not self.stop_event.is_set():
                    data = recorder.record(numframes=BLOCK_FRAMES)
                    if data is None or len(data) == 0:
                        continue
                    if getattr(data, "ndim", 1) > 1:
                        data = data[:, 0]
                    pcm = (
                        np.clip(data * 32767.0, -32768, 32767)
                        .astype(np.int16)
                        .tobytes()
                    )
                    if recognizer.AcceptWaveform(pcm):
                        self._emit(
                            json.loads(recognizer.Result()).get("text", "")
                        )
            self._emit(
                json.loads(recognizer.FinalResult()).get("text", "")
            )
        except Exception as exc:
            self.error = exc
            self.out_queue.put(
                (
                    "error",
                    self.audio_device.kind,
                    str(exc),
                    datetime.now().isoformat(timespec="seconds"),
                )
            )


def transcribe_wav(model, path: Path) -> str:
    import vosk

    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("WAV 需要 16-bit 单声道")
        if wav.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"WAV 需要 {SAMPLE_RATE} Hz，当前 {wav.getframerate()} Hz"
            )
        recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        while True:
            data = wav.readframes(4000)
            if not data:
                break
            recognizer.AcceptWaveform(data)
    return json.loads(recognizer.FinalResult()).get("text", "")


def ingest_audio_text(
    conn,
    text: str,
    *,
    source: str,
    ts: str | None = None,
    known_names=(),
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
    dedup: bool = True,
    dry_run: bool = False,
) -> CaptureIngestResult:
    return ingest_captured_text(
        conn,
        text,
        source=source,
        ts=ts,
        known_names=known_names,
        min_chars=min_chars,
        max_chars=max_chars,
        dedup=dedup,
        dry_run=dry_run,
    )


def _print_result(result: CaptureIngestResult, args, source: str) -> None:
    label = SOURCE_LABELS.get(source, source)
    if result.action in {"written", "dry_run"}:
        prefix = "[dry-run] 将写入" if result.action == "dry_run" else "已写入"
        extra = (
            f"，丢弃敏感行 {result.dropped_lines}"
            if result.dropped_lines
            else ""
        )
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] {prefix} "
            f"{source} segment #{result.segment_id or '-'}："
            f"{result.chars} 字符（{label}）{extra}"
        )
    elif args.verbose:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] 跳过 {label}："
            f"{result.reason or result.action}"
        )
    if args.show_text and result.masked_text:
        print(result.masked_text)
        print("---")


def _handle_text(conn, text: str, source: str, ts: str, args) -> None:
    result = ingest_audio_text(
        conn,
        text,
        source=source,
        ts=ts,
        known_names=tuple(args.name),
        min_chars=args.min_chars,
        max_chars=args.max_chars,
        dry_run=args.dry_run,
    )
    _print_result(result, args, source)


def run_live(args, conn, model) -> int:
    devices = resolve_devices(
        args.source, mic_name=args.mic, speaker_name=args.speaker
    )
    if not devices:
        print("没有可用的录音设备。", file=sys.stderr)
        return 1

    for device in devices:
        print(
            f"监听{SOURCE_LABELS[device.kind]}：{device.name}"
        )
    print(
        "按 Ctrl+C 停止。"
        if not args.once
        else f"录音 {args.seconds:g} 秒…"
    )

    out_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    workers = [
        CaptureWorker(device, model, out_queue, stop_event)
        for device in devices
    ]
    for worker in workers:
        worker.start()

    deadline = (
        time.monotonic() + args.seconds if args.once else None
    )

    def drain() -> None:
        while True:
            try:
                message = out_queue.get_nowait()
            except queue.Empty:
                return
            if message[0] == "text":
                _handle_text(
                    conn, message[2], message[1], message[3], args
                )

    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                stop_event.set()
                deadline = None
            try:
                message = out_queue.get(timeout=0.2)
            except queue.Empty:
                if all(not worker.is_alive() for worker in workers):
                    break
                continue
            if message[0] == "error":
                print(
                    f"{SOURCE_LABELS.get(message[1], message[1])} "
                    f"录音失败：{message[2]}",
                    file=sys.stderr,
                )
                continue
            _handle_text(
                conn, message[2], message[1], message[3], args
            )
    except KeyboardInterrupt:
        print("停止中…")
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=3)
        drain()

    print("已停止。")
    return 0


def run_wav(args, conn, model) -> int:
    text = transcribe_wav(model, Path(args.wav))
    print(f"转写：{text}")
    source = "system" if args.source == "system" else "microphone"
    _handle_text(
        conn,
        text,
        source,
        datetime.now().isoformat(timespec="seconds"),
        args,
    )
    return 0


def run_self_test(args, conn, model) -> int:
    print(f"模型加载成功：{args.model_path}")
    wav = Path(args.wav) if args.wav else ROOT_DIR / "data" / "asr_test.wav"
    if wav.exists():
        print(f"使用测试音频：{wav}")
        text = transcribe_wav(model, wav)
        print(f"转写：{text}")
        print("自检完成（不写数据库）。")
        return 0
    print("模型可用；没有找到测试 WAV，可用 --wav 指定一个 16 kHz 单声道 WAV。")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="AI 记忆增强伙伴 - 录音与本地转写"
    )
    parser.add_argument("--db", help="数据库路径，默认 data/memory.db")
    parser.add_argument(
        "--model-path",
        default=str(DEFAULT_MODEL_PATH),
        help="Vosk 模型目录",
    )
    parser.add_argument(
        "--source",
        choices=["microphone", "system", "both"],
        default="both",
        help="监听麦克风、系统声音或两者（默认 both）",
    )
    parser.add_argument("--mic", help="麦克风名称（包含匹配）")
    parser.add_argument("--speaker", help="扬声器名称（包含匹配）")
    parser.add_argument(
        "--seconds", type=float, default=DEFAULT_SECONDS,
        help="配合 --once 的录音秒数（默认 10）",
    )
    parser.add_argument(
        "--once", action="store_true", help="录指定秒数后退出"
    )
    parser.add_argument(
        "--wav", help="不录音，直接转写一个 16 kHz 单声道 WAV"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只转写和脱敏，不写数据库"
    )
    parser.add_argument(
        "--show-text", action="store_true", help="打印脱敏后的文本"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="打印跳过原因"
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
        "--list-devices", action="store_true", help="列出音频设备"
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="加载模型并转写 data/asr_test.wav（如果存在）",
    )
    args = parser.parse_args(argv)

    if args.list_devices:
        return list_devices()

    try:
        model = load_model(Path(args.model_path))
    except Exception as exc:
        print(f"加载 ASR 模型失败：{exc}", file=sys.stderr)
        return 1

    conn = connect(args.db or get_db_path())
    try:
        if args.self_test:
            return run_self_test(args, conn, model)
        if args.wav:
            return run_wav(args, conn, model)
        return run_live(args, conn, model)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
