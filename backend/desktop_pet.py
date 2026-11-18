"""简易 PyQt6 桌宠：显示今日复习卡片，收集“记得 / 模糊 / 不记得”反馈。

用法：
    python -m backend.desktop_pet
    python -m backend.desktop_pet --offline
    python -m backend.desktop_pet --smoke-test --screenshot shot.png
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from datetime import date as Date

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import configure_stdio, get_db_path, get_llm_config
from .db import connect
from .llm import DeepSeekClient
from .local_ocr import LocalOCREngine
from .node_edit import (
    EDITABLE_TYPES,
    NodeEditResult,
    edit_node_with_llm,
    get_node,
    update_node,
)
from .pet_controller import PetController
from .scheduler import IMPORTANCE_VALUES, get_today_queue
from .screen_capture import (
    _image_to_png,
    capture_window_images,
    foreground_window_info,
    ingest_screen_text,
    is_excluded,
    load_exclusions,
    session_locked,
)

CARD_TYPE_LABELS = {
    "clue": "线索回忆",
    "cloze": "填空",
    "scenario": "情景重现",
    "relation": "关联提问",
}
IMPORTANCE_ORDER = ("very_high", "high", "normal", "low")
IMPORTANCE_LABELS = {
    "very_high": "很重要",
    "high": "重要",
    "normal": "一般",
    "low": "不重要",
}
TYPE_LABELS = {
    "fact": "事实",
    "event": "事件",
    "person": "人物",
    "concept": "概念",
    "todo": "待办",
}
WEEKDAY_NAMES = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)


def importance_key(importance: float) -> str:
    if importance >= 0.9:
        return "very_high"
    if importance >= 0.7:
        return "high"
    if importance >= 0.3:
        return "normal"
    return "low"


class CardWorker(QThread):
    """后台生成卡片：先发本地模板卡片，再用云端结果尝试升级。"""

    local_ready = pyqtSignal(object)
    loaded = pyqtSignal(object, str)

    def __init__(self, db_path, offline: bool, limit: int):
        super().__init__()
        self.db_path = db_path
        self.offline = offline
        self.limit = limit

    def run(self) -> None:
        from .config import get_llm_config
        from .llm import DeepSeekClient

        conn = connect(self.db_path)
        try:
            controller = PetController(conn, llm=None, limit=self.limit)
            cards = controller.load()
            # 缓存或本地模板几乎零成本：界面先拿到卡片，不必等云端。
            self.local_ready.emit(cards)
            if (
                not cards
                or self.offline
                or all(card.source == "cache" for card in cards)
            ):
                self.loaded.emit(cards, "")
                return

            try:
                controller.llm = DeepSeekClient(get_llm_config())
                upgraded = controller.load()
                self.loaded.emit(
                    upgraded if upgraded else cards,
                    "" if upgraded else "云端未返回卡片，已使用本地模板",
                )
            except Exception as exc:
                controller.llm = None
                self.loaded.emit(cards, f"云端出题失败，已使用本地模板：{exc}")
        except Exception as exc:
            self.local_ready.emit([])
            self.loaded.emit([], f"加载失败：{exc}")
        finally:
            conn.close()


class EditWorker(QThread):
    """在后台调用 LLM 改写当前节点，避免卡住桌宠界面。"""

    done = pyqtSignal(object, str)

    def __init__(self, db_path, node_id: int, instruction: str):
        super().__init__()
        self.db_path = db_path
        self.node_id = node_id
        self.instruction = instruction

    def run(self) -> None:
        conn = connect(self.db_path)
        try:
            llm = DeepSeekClient(get_llm_config())
            result = edit_node_with_llm(
                conn, llm, self.node_id, self.instruction
            )
            self.done.emit(result, "")
        except Exception as exc:
            self.done.emit(None, str(exc))
        finally:
            conn.close()


class EditNodeDialog(QDialog):
    """修改当前记忆节点：直接编辑字段，或让 LLM 按指令改写。"""

    def __init__(
        self,
        parent,
        db_path,
        node_id: int,
        conn,
        *,
        allow_ai: bool = True,
    ):
        super().__init__(parent)
        self.db_path = db_path
        self.node_id = node_id
        self.conn = conn
        self.result_data: NodeEditResult | None = None
        self.changed = False
        self._worker: EditWorker | None = None
        self._busy = False
        self.allow_ai = allow_ai
        node = get_node(conn, node_id)

        self.setWindowTitle("修改记忆")
        self.setMinimumWidth(560)
        self.setModal(True)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.title_edit = QLineEdit(node["title"])
        self.content_edit = QTextEdit()
        self.content_edit.setPlainText(node["content"])
        self.content_edit.setMinimumHeight(180)

        self.type_combo = QComboBox()
        for node_type in EDITABLE_TYPES:
            self.type_combo.addItem(
                TYPE_LABELS.get(node_type, node_type), node_type
            )
        type_index = self.type_combo.findData(node["type"])
        if type_index >= 0:
            self.type_combo.setCurrentIndex(type_index)

        self.importance_combo = QComboBox()
        for key in IMPORTANCE_ORDER:
            self.importance_combo.addItem(
                IMPORTANCE_LABELS[key], key
            )
        importance_index = self.importance_combo.findData(
            importance_key(float(node["importance"]))
        )
        if importance_index >= 0:
            self.importance_combo.setCurrentIndex(importance_index)

        form.addRow("标题", self.title_edit)
        form.addRow("内容", self.content_edit)
        form.addRow("类型", self.type_combo)
        form.addRow("重要度", self.importance_combo)
        layout.addLayout(form)

        ai_row = QHBoxLayout()
        self.ai_instruction = QLineEdit()
        self.ai_instruction.setPlaceholderText(
            "可选：例如“改得更简洁，补充适用条件”"
        )
        self.ai_button = QPushButton("AI 改写")
        self.ai_button.clicked.connect(self._ai_rewrite)
        ai_row.addWidget(self.ai_instruction, 1)
        ai_row.addWidget(self.ai_button)
        layout.addLayout(ai_row)
        if not allow_ai:
            self.ai_instruction.setEnabled(False)
            self.ai_button.setEnabled(False)
            self.ai_button.setToolTip("离线模式不调用云端 LLM")

        self.status = QLabel("")
        self.status.setStyleSheet(
            "color: #6b8299; font-size: 12px;"
        )
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(
            QDialogButtonBox.StandardButton.Save
        ).setText("保存")
        self.buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("取消")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.setStyleSheet(
            "QDialog { background: #ffffff; }"
            "QLabel { color: #4a6a85; font-size: 13px; }"
            "QLineEdit, QTextEdit, QComboBox { background: #f7fbff;"
            " color: #1c2b36; border: 1px solid #d7e3ee;"
            " border-radius: 7px; padding: 6px; font-size: 14px; }"
            "QPushButton { background: #eef4fa; color: #4a6a85;"
            " border: none; border-radius: 8px; padding: 8px 14px;"
            " font-size: 14px; }"
            "QPushButton:hover { background: #e3eef9; }"
        )
        self.ai_button.setStyleSheet(
            "QPushButton { background: #5aa9e6; color: white;"
            " border: none; border-radius: 8px; padding: 8px 14px;"
            " font-size: 14px; }"
            "QPushButton:hover { background: #4a9bd8; }"
        )
        self.buttons.button(
            QDialogButtonBox.StandardButton.Save
        ).setStyleSheet(
            "QPushButton { background: #4caf7d; color: white;"
            " border: none; border-radius: 8px; padding: 8px 18px;"
            " font-size: 14px; }"
            "QPushButton:hover { background: #43a06f; }"
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (
            self.title_edit,
            self.content_edit,
            self.type_combo,
            self.importance_combo,
            self.ai_instruction,
            self.ai_button,
            self.buttons,
        ):
            widget.setEnabled(not busy)

    def _populate(self, result: NodeEditResult) -> None:
        self.title_edit.setText(result.title)
        self.content_edit.setPlainText(result.content)
        type_index = self.type_combo.findData(result.node_type)
        if type_index >= 0:
            self.type_combo.setCurrentIndex(type_index)
        importance_index = self.importance_combo.findData(
            importance_key(result.importance)
        )
        if importance_index >= 0:
            self.importance_combo.setCurrentIndex(importance_index)

    def _ai_rewrite(self) -> None:
        instruction = self.ai_instruction.text().strip()
        if not instruction:
            QMessageBox.information(
                self, "AI 改写", "请先填写修改要求。"
            )
            return
        self._set_busy(True)
        self.status.setText("AI 改写中，请稍候…")
        worker = EditWorker(
            self.db_path, self.node_id, instruction
        )
        self._worker = worker
        worker.done.connect(self._on_ai_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_ai_done(self, result, error: str) -> None:
        self._worker = None
        self._set_busy(False)
        if error or result is None:
            self.status.setText("")
            QMessageBox.critical(
                self, "AI 改写失败", error or "LLM 没有返回结果"
            )
            return
        self._populate(result)
        self.result_data = result
        self.changed = True
        self.status.setText("AI 改写完成；可继续修改后保存。")

    def _save(self) -> None:
        title = self.title_edit.text().strip()
        content = self.content_edit.toPlainText().strip()
        if not title or not content:
            QMessageBox.warning(
                self, "无法保存", "标题和内容都不能为空。"
            )
            return
        try:
            result = update_node(
                self.conn,
                self.node_id,
                title=title,
                content=content,
                node_type=self.type_combo.currentData(),
                importance=IMPORTANCE_VALUES[
                    self.importance_combo.currentData()
                ],
                today=Date.today(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.result_data = result
        self.changed = True
        self.accept()

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("AI 改写中，请稍候…")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("AI 改写中，请稍候…")
            event.ignore()
            return
        super().closeEvent(event)


class HotkeyWorker(QThread):
    """注册 Windows 全局快捷键 Ctrl+Alt+E。"""

    triggered = pyqtSignal()
    failed = pyqtSignal(str)
    HOTKEY_ID = 0x4D45
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(self):
        super().__init__()
        self._thread_id = 0

    def run(self) -> None:
        if sys.platform != "win32":
            self.failed.emit("全局快捷键只支持 Windows")
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
        ]
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = int(kernel32.GetCurrentThreadId())

        MOD_ALT = 0x0001
        MOD_CONTROL = 0x0002
        MOD_NOREPEAT = 0x4000
        VK_E = 0x45
        if not user32.RegisterHotKey(
            None,
            self.HOTKEY_ID,
            MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
            VK_E,
        ):
            error = ctypes.get_last_error()
            self.failed.emit(
                f"注册 Ctrl+Alt+E 失败（错误码 {error}），"
                "可能被其他程序占用"
            )
            return
        try:
            message = wintypes.MSG()
            while user32.GetMessageW(
                ctypes.byref(message), None, 0, 0
            ) > 0:
                if (
                    message.message == self.WM_HOTKEY
                    and message.wParam == self.HOTKEY_ID
                ):
                    self.triggered.emit()
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def stop(self) -> None:
        if sys.platform != "win32" or not self._thread_id:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW(
            self._thread_id, self.WM_QUIT, 0, 0
        )


class ScreenshotWorker(QThread):
    """后台 OCR 一张手动截图；复用常驻的私有 OCR 引擎。"""

    done = pyqtSignal(bool, str)

    def __init__(self, db_path, pngs, engine: LocalOCREngine):
        super().__init__()
        self.db_path = db_path
        self.pngs = pngs
        self.engine = engine

    def run(self) -> None:
        try:
            texts: list[str] = []
            for png in self.pngs:
                text = self.engine.ocr_png(png)
                if text.strip():
                    texts.append(text)
            raw = "\n\n".join(texts)
            if not raw.strip():
                self.done.emit(False, "没有识别到文字")
                return
            conn = connect(self.db_path)
            try:
                result = ingest_screen_text(conn, raw)
            finally:
                conn.close()
            message = (
                f"{result.chars} 字符：{result.reason}"
                if result.reason
                else f"{result.chars} 字符"
            )
            self.done.emit(
                result.action
                in {
                    "written",
                    "skipped_duplicate",
                    "skipped_short",
                    "skipped_sensitive",
                },
                message,
            )
        except Exception as exc:
            self.done.emit(False, str(exc))


class DesktopPet(QWidget):
    def __init__(
        self,
        db_path,
        offline: bool = False,
        limit: int = 8,
        *,
        enable_hotkey: bool = True,
    ):
        super().__init__()
        self.db_path = db_path
        self.offline = offline
        self.limit = limit
        self.warning = ""
        self._cloud_pending = False
        self._drag_offset = None
        self._worker = None
        self._hotkey = None
        self._screenshot_worker = None
        self._manual_engine = None
        self._hotkey_error = ""
        self.controller = PetController(connect(db_path), llm=None, limit=limit)

        self._setup_window()
        self._build_ui()
        self._bind_events()
        self._move_to_corner()
        self.reload()
        if enable_hotkey:
            self._start_hotkey()

    def _start_hotkey(self) -> None:
        self._hotkey = HotkeyWorker()
        self._hotkey.triggered.connect(self._manual_screenshot)
        self._hotkey.failed.connect(self._on_hotkey_failed)
        self._hotkey.start()

    def _on_hotkey_failed(self, message: str) -> None:
        self._hotkey_error = message
        self.status.setToolTip(message)
        print(f"[hotkey] {message}", file=sys.stderr)

    def _manual_screenshot(self) -> None:
        if (
            self._screenshot_worker is not None
            and self._screenshot_worker.isRunning()
        ):
            return
        if session_locked():
            self.status.setText("手动截图失败：屏幕已锁定")
            return
        info = foreground_window_info()
        if info is None:
            self.status.setText("手动截图失败：没有前台窗口")
            return
        reason = is_excluded(info, load_exclusions())
        if reason is not None:
            self.status.setText(f"手动截图已跳过：{reason}")
            return
        images = capture_window_images(info)
        if not images:
            self.status.setText("手动截图失败：无法捕获窗口")
            return
        try:
            pngs = [_image_to_png(image) for _, image in images]
        except Exception as exc:
            self.status.setText(f"手动截图失败：{exc}")
            return
        if self._manual_engine is None:
            try:
                self._manual_engine = LocalOCREngine("auto")
            except Exception as exc:
                self.status.setText(f"OCR 不可用：{exc}")
                self.status.setToolTip(str(exc))
                return
        self.status.setText("正在识别手动截图…")
        worker = ScreenshotWorker(
            self.db_path, pngs, self._manual_engine
        )
        self._screenshot_worker = worker
        worker.done.connect(self._on_screenshot_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_screenshot_done(self, ok: bool, message: str) -> None:
        self._screenshot_worker = None
        self.status.setText(
            "手动截图处理完成，等待夜间整合"
            if ok
            else "手动截图失败"
        )
        self.status.setToolTip(message)
        if not ok:
            print(f"[screenshot] {message}", file=sys.stderr)

    # ---------- 界面搭建 ----------

    @staticmethod
    def _today_text() -> str:
        today = Date.today()
        return f"{today.isoformat()} · {WEEKDAY_NAMES[today.weekday()]}"

    def _refresh_date(self) -> None:
        if hasattr(self, "date_label"):
            self.date_label.setText(self._today_text())

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("AI 记忆伙伴")
        self.setFixedSize(540, 600)

    def _make_button(self, text: str, color: str, text_color: str = "#ffffff") -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            f"QPushButton {{ background-color: {color}; color: {text_color};"
            " border: none; border-radius: 9px; padding: 9px 12px;"
            " font-size: 14px; }"
            "QPushButton:hover { opacity: 0.9; }"
        )
        return button

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("card")
        card.setStyleSheet(
            "#card { background-color: #ffffff;"
            " border: 1px solid #d7e3ee;"
            " border-radius: 16px; }"
        )
        root.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header_left = QVBoxLayout()
        header_left.setSpacing(1)
        title = QLabel("记忆伙伴")
        title.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        title.setStyleSheet("color: #1c2b36;")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.date_label = QLabel(self._today_text())
        self.date_label.setStyleSheet(
            "color: #6b8299; font-size: 12px;"
        )
        self.date_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        header_left.addWidget(title)
        header_left.addWidget(self.date_label)
        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent;"
            " color: #7a93a8; font-size: 18px; }"
            "QPushButton:hover { color: #d9534f; }"
        )
        header.addLayout(header_left)
        header.addStretch()
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("color: #e6eef6;")
        layout.addWidget(separator)

        self.status = QLabel("正在读取今天的记忆…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet("color: #6b8299; font-size: 13px;")
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.status)

        bubble = QFrame()
        bubble.setObjectName("bubble")
        bubble.setStyleSheet(
            "#bubble { background-color: #f7fbff; border: 1px solid #d7e3ee;"
            " border-radius: 14px; }"
        )
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(16, 16, 16, 16)
        bubble_layout.setSpacing(10)

        self.topic_label = QLabel("")
        self.topic_label.setWordWrap(False)
        self.topic_label.setFixedHeight(28)
        self.topic_label.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        self.topic_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
        )
        self.topic_label.setStyleSheet(
            "color: #4a6a85; background: #eef4fa;"
            " border: 1px solid #d7e3ee; border-radius: 8px;"
            " padding: 2px 9px; font-size: 12px;"
        )
        bubble_layout.addWidget(
            self.topic_label,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )

        self.question = QLabel("")
        self.question.setWordWrap(True)
        self.question.setMinimumHeight(64)
        self.question.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.question.setStyleSheet(
            "color: #1c2b36; font-size: 16px; font-weight: 600;"
        )
        bubble_layout.addWidget(self.question)

        self.hint = QLabel("先自己回忆，再点“显示答案”。")
        self.hint.setStyleSheet(
            "color: #8aa0b4; font-size: 12px;"
        )
        bubble_layout.addWidget(self.hint)

        self.importance_row = QWidget()
        importance_layout = QHBoxLayout(self.importance_row)
        importance_layout.setContentsMargins(0, 0, 0, 0)
        importance_layout.setSpacing(5)
        importance_title = QLabel("重要度")
        importance_title.setStyleSheet(
            "color: #6b8299; font-size: 12px;"
        )
        importance_layout.addWidget(importance_title)
        self.importance_buttons: dict[str, QPushButton] = {}
        for key in IMPORTANCE_ORDER:
            button = QPushButton(IMPORTANCE_LABELS[key])
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { border: 1px solid #d7e3ee;"
                " background: #f4f8fc; color: #4a6a85;"
                " border-radius: 7px; padding: 4px 6px;"
                " font-size: 12px; }"
                "QPushButton:hover { background: #e8f2fb; }"
                "QPushButton:checked { background: #5aa9e6;"
                " color: white; border-color: #5aa9e6; }"
            )
            button.clicked.connect(
                lambda checked=False, choice=key: self._set_importance(
                    choice
                )
            )
            self.importance_buttons[key] = button
            importance_layout.addWidget(button)
        self.importance_row.hide()
        bubble_layout.addWidget(self.importance_row)

        self.answer = QLabel("")
        self.answer.setWordWrap(True)
        self.answer.setStyleSheet(
            "color: #2f6b3f; background-color: #eef8f0;"
            " border-radius: 9px; padding: 10px; font-size: 14px;"
        )
        self.answer.hide()
        bubble_layout.addWidget(self.answer)

        self.reveal_btn = self._make_button("显示答案", "#5aa9e6")
        bubble_layout.addWidget(self.reveal_btn)

        self.feedback_widget = QWidget()
        feedback_layout = QHBoxLayout(self.feedback_widget)
        feedback_layout.setContentsMargins(0, 0, 0, 0)
        feedback_layout.setSpacing(8)
        self.remember_btn = self._make_button("记得", "#4caf7d")
        self.fuzzy_btn = self._make_button("模糊", "#f0b429")
        self.forgot_btn = self._make_button("不记得", "#e57373")
        feedback_layout.addWidget(self.remember_btn)
        feedback_layout.addWidget(self.fuzzy_btn)
        feedback_layout.addWidget(self.forgot_btn)
        self.feedback_widget.hide()
        bubble_layout.addWidget(self.feedback_widget)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.edit_btn = self._make_button("修改", "#eef4fa", "#4a6a85")
        self.edit_btn.setFixedWidth(76)
        self.skip_btn = self._make_button("跳过", "#eef4fa", "#4a6a85")
        self.reload_btn = self._make_button("重新加载", "#eef4fa", "#4a6a85")
        footer.addWidget(self.edit_btn)
        footer.addWidget(self.skip_btn)
        footer.addWidget(self.reload_btn)
        bubble_layout.addLayout(footer)

        layout.addWidget(bubble, 1)

    def _bind_events(self) -> None:
        self.close_btn.clicked.connect(self.close)
        self.reveal_btn.clicked.connect(self._reveal)
        self.edit_btn.clicked.connect(self._edit_current)
        self.skip_btn.clicked.connect(self._skip)
        self.reload_btn.clicked.connect(self.reload)
        self.remember_btn.clicked.connect(
            lambda checked=False: self._feedback("remembered")
        )
        self.fuzzy_btn.clicked.connect(
            lambda checked=False: self._feedback("fuzzy")
        )
        self.forgot_btn.clicked.connect(
            lambda checked=False: self._feedback("forgot")
        )

        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._show_card)

    def _move_to_corner(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.right() - self.width() - 24,
            area.bottom() - self.height() - 24,
        )

    # ---------- 数据与状态 ----------

    def reload(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._refresh_date()
        self.warning = ""
        self._cloud_pending = False
        self.status.setToolTip("")
        self.status.setText("思考中…正在生成今天的复习卡片")
        self.question.setText("稍等一下，我在整理今天的记忆。")
        self.answer.hide()
        self.reveal_btn.hide()
        self.feedback_widget.hide()
        self.hint.hide()
        self.topic_label.hide()
        self.importance_row.hide()
        self.edit_btn.hide()
        self.skip_btn.hide()
        self.reload_btn.hide()

        self._worker = CardWorker(self.db_path, self.offline, self.limit)
        self._worker.local_ready.connect(self._on_local_ready)
        self._worker.loaded.connect(self._on_loaded)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._worker:
            self._worker = None
        if worker is not None:
            worker.deleteLater()

    def _on_local_ready(self, cards) -> None:
        self._cloud_pending = (
            bool(cards)
            and not self.offline
            and any(card.source != "cache" for card in cards)
        )
        self.controller.set_cards(cards)
        if not cards:
            self._show_empty()
            return
        self._show_card()

    def _on_loaded(self, cards, warning: str) -> None:
        self._cloud_pending = False
        self.warning = warning
        if warning:
            self.status.setToolTip(warning)

        if not cards:
            self._show_empty()
            return
        if self.controller.index > 0 or self.controller.revealed:
            # 用户已经开始作答，不要中途换题；只更新状态提示。
            self._refresh_status()
            return
        self.controller.set_cards(cards)
        self._show_card()

    def _show_empty(self) -> None:
        self._cloud_pending = False
        self.status.setText("今天没有到期的记忆")
        self.question.setText("休息一下，明天见。")
        self._show_idle_controls()

    def _status_text(self, progress: str) -> str:
        if self._cloud_pending:
            suffix = " · 云端出题中"
        elif self.warning:
            suffix = " · 本地出题"
        else:
            suffix = ""
        return f"{progress}{suffix}"

    def _refresh_status(self) -> None:
        card = self.controller.current
        if card is None:
            return
        current, total = self.controller.progress()
        label = CARD_TYPE_LABELS.get(card.card_type, card.card_type)
        self.status.setText(self._status_text(f"{current}/{total} · {label}"))

    def _refresh_importance_buttons(self, importance: float) -> None:
        bucket = importance_key(importance)
        for key, button in self.importance_buttons.items():
            button.setChecked(key == bucket)

    def _set_importance(self, key: str) -> None:
        try:
            result = self.controller.set_importance(key)
        except Exception as exc:
            QMessageBox.critical(self, "设置重要度失败", str(exc))
            return
        if result is None:
            return
        label = IMPORTANCE_LABELS.get(key, key)
        self._refresh_importance_buttons(result.importance)
        if result.reviewable:
            card = self.controller.current
            if card is not None:
                current, total = self.controller.progress()
                card_label = CARD_TYPE_LABELS.get(
                    card.card_type, card.card_type
                )
                self.status.setText(
                    f"{current}/{total} · {card_label} · "
                    f"重要度：{label}"
                )
            return

        try:
            if self._confirm_delete_after_low():
                self.controller.delete_current()
            else:
                self.controller.advance()
        except Exception as exc:
            QMessageBox.critical(self, "操作失败", str(exc))
            return
        self._show_card()

    def _confirm_delete_after_low(self) -> bool:
        """显式置顶的确认框，避免 Tool 窗口把标准弹窗压在后面。"""
        box = QMessageBox(self)
        box.setWindowTitle("删除记忆")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "已标为不重要，不再进入复习。\n"
            "要同时删除这条记忆吗？删除后不可恢复。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setWindowModality(Qt.WindowModality.ApplicationModal)
        box.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint, True
        )
        box.setStyleSheet(
            "QMessageBox { background: #ffffff; }"
            "QLabel { color: #1c2b36; font-size: 14px; }"
            "QPushButton { background: #eef4fa; color: #4a6a85;"
            " border: none; border-radius: 7px;"
            " padding: 7px 14px; font-size: 14px; }"
            "QPushButton:hover { background: #e3eef9; }"
        )
        yes_button = box.button(QMessageBox.StandardButton.Yes)
        no_button = box.button(QMessageBox.StandardButton.No)
        if yes_button is not None:
            yes_button.setText("删除")
            yes_button.setStyleSheet(
                "QPushButton { background: #e57373; color: white;"
                " border: none; border-radius: 7px;"
                " padding: 7px 14px; font-size: 14px; }"
                "QPushButton:hover { background: #d65f5f; }"
            )
        if no_button is not None:
            no_button.setText("仅标记")
        box.adjustSize()
        center = self.frameGeometry().center()
        box.move(center - box.rect().center())
        box.raise_()
        box.activateWindow()
        return box.exec() == QMessageBox.StandardButton.Yes

    def _show_idle_controls(self) -> None:
        self.answer.hide()
        self.reveal_btn.hide()
        self.feedback_widget.hide()
        self.hint.hide()
        self.topic_label.hide()
        self.importance_row.hide()
        self.edit_btn.hide()
        self.skip_btn.hide()
        self.reload_btn.show()

    def _show_card(self) -> None:
        self._refresh_date()
        card = self.controller.current
        if card is None:
            self.status.setText("今天的复习完成了")
            self.question.setText("全部完成，明天见。")
            self._show_idle_controls()
            return

        self._refresh_status()
        topic = (
            f"{TYPE_LABELS.get(card.type, card.type)} · {card.title}"
        )
        metrics = QFontMetrics(self.topic_label.font())
        self.topic_label.setText(
            metrics.elidedText(
                topic,
                Qt.TextElideMode.ElideRight,
                420,
            )
        )
        self.topic_label.setToolTip(topic)
        self.topic_label.show()
        self.question.setText(card.question)
        self.hint.show()
        self._refresh_importance_buttons(card.importance)
        self.importance_row.show()
        self.answer.setText(card.answer)
        self.answer.hide()
        self.reveal_btn.show()
        self.feedback_widget.hide()
        self.edit_btn.show()
        self.skip_btn.show()
        self.reload_btn.hide()

    def _reveal(self) -> None:
        card = self.controller.current
        if card is None:
            return
        self.controller.reveal()
        self.hint.hide()
        self.answer.setText(card.answer)
        self.answer.show()
        self.reveal_btn.hide()
        self.feedback_widget.show()

    def _feedback(self, value: str) -> None:
        result = self.controller.feedback(value)
        if result is None:
            return
        self.status.setText(f"已记录，下次复习：{result.due_at}")
        self.answer.hide()
        self.feedback_widget.hide()
        self.hint.hide()
        self.topic_label.hide()
        self.importance_row.hide()
        self.reveal_btn.hide()
        self.edit_btn.hide()
        self.skip_btn.hide()
        self._advance_timer.start(900)

    def _skip(self) -> None:
        self.controller.skip()
        self._show_card()

    def _edit_current(self) -> None:
        card = self.controller.current
        if card is None:
            return
        try:
            dialog = EditNodeDialog(
                self,
                self.db_path,
                card.node_id,
                self.controller.conn,
                allow_ai=not self.offline,
            )
        except Exception as exc:
            QMessageBox.critical(self, "无法编辑", str(exc))
            return
        dialog.exec()
        if dialog.changed:
            self._reload_local_queue()
            self.status.setText(f"已保存修改 · {self.status.text()}")
            self.status.setToolTip("已保存修改")

    def _reload_local_queue(self) -> None:
        """编辑后只用本地数据刷新队列，不触发云端出题。"""
        cards = get_today_queue(
            self.controller.conn,
            llm=None,
            limit=self.limit,
        )
        self.controller.set_cards(cards)
        self._cloud_pending = False
        self.warning = ""
        self._show_card()

    # ---------- 窗口交互 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        shot_action = menu.addAction("手动截图 (Ctrl+Alt+E)")
        reload_action = menu.addAction("重新加载")
        quit_action = menu.addAction("退出")
        chosen = menu.exec(event.globalPos())
        if chosen is shot_action:
            self._manual_screenshot()
        elif chosen is reload_action:
            self.reload()
        elif chosen is quit_action:
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey.wait(2000)
        if (
            self._screenshot_worker is not None
            and self._screenshot_worker.isRunning()
        ):
            self._screenshot_worker.wait(5000)
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            if not self._worker.wait(3000):
                # 云端请求卡住时不必让窗口无法关闭；进程即将退出。
                self._worker.terminate()
                self._worker.wait(1000)
        if self._manual_engine is not None:
            self._manual_engine.close()
            self._manual_engine = None
        self.controller.conn.close()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="AI 记忆增强伙伴 - 桌宠")
    parser.add_argument("--db")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--offline", action="store_true", help="本地模板出题")
    parser.add_argument("--smoke-test", action="store_true", help="启动后自动退出")
    parser.add_argument("--screenshot", help="截图保存路径")
    parser.add_argument(
        "--demo-reveal",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    window = DesktopPet(
        args.db or get_db_path(),
        offline=args.offline or args.smoke_test,
        limit=args.limit,
        enable_hotkey=not args.smoke_test,
    )
    window.show()

    if args.screenshot:
        def snap() -> None:
            ok = window.grab().save(args.screenshot)
            print(f"screenshot {args.screenshot}: {ok}")

        QTimer.singleShot(1800, snap)

    if args.demo_reveal:
        QTimer.singleShot(1000, window._reveal)

    if args.smoke_test:
        QTimer.singleShot(2800 if args.screenshot else 1600, app.quit)
        QTimer.singleShot(1400, lambda: print("smoke-ok"))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
