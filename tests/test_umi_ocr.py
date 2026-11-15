import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.umi_ocr import (
    UmiOCRClient,
    UmiOCRError,
    detect_engine,
    read_mkldnn,
    set_mkldnn,
)

PADDLE_OPTIONS = {
    "ocr.language": {
        "type": "enum",
        "default": "models/config_chinese.txt",
    },
    "ocr.cls": {"type": "boolean", "default": False},
    "ocr.limit_side_len": {"type": "enum", "default": 960},
    "data.format": {"type": "enum", "default": "dict"},
}
RAPID_OPTIONS = {
    "ocr.language": {"type": "enum", "default": "简体中文"},
    "ocr.angle": {"type": "boolean", "default": False},
    "ocr.maxSideLen": {"type": "enum", "default": 1024},
    "data.format": {"type": "enum", "default": "dict"},
}


class FakeUmiHandler(BaseHTTPRequestHandler):
    engine = "paddle"
    response_code = 100
    response_text = "识别结果"
    last_payload = None

    def log_message(self, *args):
        pass

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/api/ocr/get_options":
            self.send_error(404)
            return
        options = (
            PADDLE_OPTIONS if self.engine == "paddle" else RAPID_OPTIONS
        )
        self._send(options)

    def do_POST(self):
        if self.path != "/api/ocr":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).last_payload = payload
        self._send(
            {
                "code": self.response_code,
                "data": self.response_text,
                "time": 0.01,
            }
        )


@pytest.fixture()
def fake_umi():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUmiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    FakeUmiHandler.engine = "paddle"
    FakeUmiHandler.response_code = 100
    FakeUmiHandler.response_text = "识别结果"
    FakeUmiHandler.last_payload = None
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()


def test_detect_engine_by_options():
    assert detect_engine(PADDLE_OPTIONS) == "paddle"
    assert detect_engine(RAPID_OPTIONS) == "rapid"
    assert detect_engine({}) == "unknown"


def test_paddle_client_reads_options_and_text(fake_umi):
    client = UmiOCRClient(url=fake_umi, engine="paddle")

    info = client.prepare(require_mkldnn=False)
    text = client.ocr_png(b"fake-png")

    assert info.engine == "paddle"
    assert info.url == fake_umi
    assert text == "识别结果"
    assert FakeUmiHandler.last_payload["options"]["data.format"] == "text"
    assert FakeUmiHandler.last_payload["base64"]


def test_rapid_client_uses_rapid_url(fake_umi):
    FakeUmiHandler.engine = "rapid"
    client = UmiOCRClient(
        rapid_url=fake_umi, engine="rapid"
    )

    info = client.prepare(require_mkldnn=False)

    assert info.engine == "rapid"
    assert info.mkldnn is None
    assert "RapidOCR" in info.mkldnn_source


def test_engine_mismatch_raises(fake_umi):
    FakeUmiHandler.engine = "paddle"
    client = UmiOCRClient(url=fake_umi, engine="rapid")

    with pytest.raises(UmiOCRError, match="当前运行的是 paddle"):
        client.prepare(require_mkldnn=False)


def test_no_text_code_returns_empty(fake_umi):
    FakeUmiHandler.response_code = 101
    FakeUmiHandler.response_text = ""
    client = UmiOCRClient(url=fake_umi, engine="paddle")
    client.prepare(require_mkldnn=False)

    assert client.ocr_png(b"fake") == ""


def test_error_code_raises(fake_umi):
    FakeUmiHandler.response_code = 900
    FakeUmiHandler.response_text = "引擎崩溃"
    client = UmiOCRClient(url=fake_umi, engine="paddle")
    client.prepare(require_mkldnn=False)

    with pytest.raises(UmiOCRError, match="引擎崩溃"):
        client.ocr_png(b"fake")


def test_set_mkldnn_writes_true_and_backs_up(tmp_path):
    data_dir = tmp_path / "UmiOCR-data"
    data_dir.mkdir()
    settings = data_dir / ".settings"
    settings.write_text(
        "[Global]\n"
        "ocr.api=win7_x64_PaddleOCR-json\n"
        "ocr.win7_x64_PaddleOCR-json.enable_mkldnn=false\n",
        encoding="utf-8",
    )

    enabled, source = read_mkldnn(tmp_path)
    assert enabled is False
    assert source.endswith(".settings")

    path = set_mkldnn(tmp_path, True)

    assert path == settings
    assert "enable_mkldnn=true" in settings.read_text(encoding="utf-8")
    backup = data_dir / ".settings.bak"
    assert backup.exists()
    assert "enable_mkldnn=false" in backup.read_text(encoding="utf-8")
    assert read_mkldnn(tmp_path)[0] is True
