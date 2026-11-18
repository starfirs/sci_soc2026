"""AI 记忆伙伴控制中心：集中管理采集、记忆、复习和夜间任务。

启动：
    python -m backend.control_center
    python -m backend.control_center --smoke-test --screenshot shot.png
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date as Date, timedelta
from pathlib import Path

from PyQt6.QtCore import QProcess, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import import_doc, pipeline
from .config import ROOT_DIR, configure_stdio, get_db_path, get_llm_config
from .db import connect
from .desktop_pet import EditNodeDialog
from .llm import DeepSeekClient
from .local_ocr import LocalOCREngine
from .nightly import run_nightly
from .node_edit import delete_node, node_detail


class TextIngestWorker(QThread):
    done = pyqtSignal(object, str)

    def __init__(self, db_path, text: str):
        super().__init__()
        self.db_path = db_path
        self.text = text

    def run(self) -> None:
        conn = connect(self.db_path)
        try:
            _, report = pipeline.ingest_text(
                conn,
                DeepSeekClient(get_llm_config()),
                self.text,
            )
            self.done.emit(report, "")
        except Exception as exc:
            self.done.emit(None, str(exc))
        finally:
            conn.close()


class ImportWorker(QThread):
    done = pyqtSignal(str, str)

    def __init__(self, db_path, files, defer: bool):
        super().__init__()
        self.db_path = db_path
        self.files = [Path(path) for path in files]
        self.defer = defer

    def run(self) -> None:
        conn = connect(self.db_path)
        lines: list[str] = []
        try:
            llm = (
                None
                if self.defer
                else DeepSeekClient(get_llm_config())
            )
            for path in self.files:
                report = import_doc.import_file(
                    conn,
                    llm,
                    path,
                    defer=self.defer,
                )
                lines.append(
                    f"{path.name}: {report.segments} 段，"
                    f"新建 {report.created}，合并 {report.merged}"
                )
            self.done.emit("\n".join(lines), "")
        except Exception as exc:
            self.done.emit("\n".join(lines), str(exc))
        finally:
            conn.close()


class NightlyWorker(QThread):
    done = pyqtSignal(object, str)

    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path

    def run(self) -> None:
        conn = connect(self.db_path)
        try:
            report = run_nightly(
                conn,
                DeepSeekClient(get_llm_config()),
            )
            self.done.emit(report, "")
        except Exception as exc:
            self.done.emit(None, str(exc))
        finally:
            conn.close()


class ControlCenter(QMainWindow):
    def __init__(
        self,
        db_path,
        *,
        smoke_test: bool = False,
        screenshot_path: str | None = None,
    ):
        super().__init__()
        self.db_path = str(db_path)
        self.smoke_test = smoke_test
        self.screenshot_path = screenshot_path
        self.conn = connect(self.db_path)
        self.screen_process: QProcess | None = None
        self.audio_process: QProcess | None = None
        self.screenshot_process: QProcess | None = None
        self._workers: set[QThread] = set()
        self.stat_labels: dict[str, QLabel] = {}

        self.setWindowTitle("AI 记忆伙伴 · 控制中心")
        self.resize(1040, 760)
        self._set_style()
        self._build_ui()
        self.refresh_all()
        self.log("控制中心已启动。桌宠里的 Ctrl+Alt+E 快捷键仍可手动截图。")
        if smoke_test:
            if screenshot_path:
                QTimer.singleShot(900, self._save_screenshot)
            QTimer.singleShot(1600, self._smoke_quit)

    # ---------- 样式与布局 ----------

    def _set_style(self) -> None:
        self.setStyleSheet(
            "QMainWindow, QWidget { background: #f5f9fd;"
            " color: #1c2b36; font-family: 'Microsoft YaHei';"
            " font-size: 13px; }"
            "QTabWidget::pane { border: 1px solid #d7e3ee;"
            " background: #ffffff; border-radius: 8px; }"
            "QTabBar::tab { background: #eef4fa; color: #4a6a85;"
            " padding: 8px 18px; margin-right: 2px;"
            " border-top-left-radius: 7px;"
            " border-top-right-radius: 7px; }"
            "QTabBar::tab:selected { background: #ffffff;"
            " color: #1c2b36; font-weight: 600; }"
            "QPushButton { background: #5aa9e6; color: white;"
            " border: none; border-radius: 7px; padding: 7px 13px; }"
            "QPushButton:hover { background: #4a9bd8; }"
            "QPushButton:disabled { background: #c9d9e8; }"
            "QLineEdit, QComboBox, QSpinBox, QPlainTextEdit,"
            " QTextEdit, QTextBrowser { background: white;"
            " border: 1px solid #d7e3ee; border-radius: 6px;"
            " padding: 5px; }"
            "QTableWidget { background: white;"
            " gridline-color: #e6eef6;"
            " selection-background-color: #dceefb;"
            " selection-color: #1c2b36; }"
            "QHeaderView::section { background: #eef4fa;"
            " color: #4a6a85; padding: 6px; border: none; }"
            "QGroupBox { border: 1px solid #d7e3ee;"
            " border-radius: 8px; margin-top: 16px;"
            " padding-top: 10px; background: white; }"
            "QGroupBox::title { subcontrol-origin: margin;"
            " subcontrol-position: top left; left: 10px;"
            " padding: 0 5px; color: #4a6a85; }"
            "QCheckBox { background: transparent; spacing: 6px; }"
            "QCheckBox::indicator { width: 16px; height: 16px;"
            " border: 1px solid #9bb3c7; border-radius: 3px;"
            " background: white; }"
            "QCheckBox::indicator:checked { background: #5aa9e6;"
            " border-color: #5aa9e6; }"
        )

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_overview(), "概览")
        self.tabs.addTab(self._build_memory(), "记忆库")
        self.tabs.addTab(self._build_capture(), "采集")
        self.tabs.addTab(self._build_guide(), "功能说明")
        self.statusBar().showMessage(f"数据库：{self.db_path}")

    def _stat_card(self, title: str, key: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(14, 10, 14, 10)
        title_label = QLabel(title)
        title_label.setStyleSheet("color: #6b8299; font-size: 12px;")
        value = QLabel("0")
        value.setStyleSheet(
            "color: #1c2b36; font-size: 24px; font-weight: 700;"
        )
        layout.addWidget(title_label)
        layout.addWidget(value)
        self.stat_labels[key] = value
        widget.setStyleSheet(
            "QWidget { background: white; border: 1px solid #d7e3ee;"
            " border-radius: 9px; }"
        )
        return widget

    def _build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        stats = QGridLayout()
        stats.addWidget(self._stat_card("记忆节点", "nodes"), 0, 0)
        stats.addWidget(self._stat_card("今日到期", "due"), 0, 1)
        stats.addWidget(self._stat_card("待整合片段", "pending"), 0, 2)
        stats.addWidget(self._stat_card("今日卡片", "cards_today"), 0, 3)
        stats.addWidget(self._stat_card("次日卡片", "cards_tomorrow"), 0, 4)
        layout.addLayout(stats)

        actions = QGroupBox("快捷操作")
        grid = QGridLayout(actions)
        self.review_button = QPushButton("打开桌宠复习")
        self.review_button.clicked.connect(self._open_pet)
        self.text_button = QPushButton("手动输入一段文字")
        self.text_button.clicked.connect(self._manual_text)
        self.import_button = QPushButton("导入文档")
        self.import_button.clicked.connect(self._import_documents)
        self.nightly_button = QPushButton("运行夜间整合 + 次日卡片")
        self.nightly_button.clicked.connect(self._run_nightly)
        self.refresh_button = QPushButton("刷新概览")
        self.refresh_button.clicked.connect(self.refresh_all)
        grid.addWidget(self.review_button, 0, 0)
        grid.addWidget(self.text_button, 0, 1)
        grid.addWidget(self.import_button, 0, 2)
        grid.addWidget(self.nightly_button, 1, 0)
        grid.addWidget(self.refresh_button, 1, 1)
        layout.addWidget(actions)

        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, 1)
        return page

    def _build_memory(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索标题或内容…")
        self.search_edit.returnPressed.connect(self.refresh_nodes)
        search_button = QPushButton("搜索")
        search_button.clicked.connect(self.refresh_nodes)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_nodes)
        top.addWidget(self.search_edit, 1)
        top.addWidget(search_button)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.node_table = QTableWidget(0, 6)
        self.node_table.setHorizontalHeaderLabels(
            ["ID", "类型", "标题", "重要度", "状态", "到期"]
        )
        self.node_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.node_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.node_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.node_table.itemSelectionChanged.connect(
            self._show_selected_node
        )
        self.node_table.doubleClicked.connect(
            lambda _index: self._edit_selected_node()
        )
        layout.addWidget(self.node_table, 1)

        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setMinimumHeight(160)
        layout.addWidget(self.detail_view)

        buttons = QHBoxLayout()
        self.edit_node_button = QPushButton("编辑选中节点")
        self.edit_node_button.clicked.connect(self._edit_selected_node)
        self.edit_node_button.setEnabled(False)
        self.delete_node_button = QPushButton("删除选中节点")
        self.delete_node_button.clicked.connect(self._delete_selected_node)
        self.delete_node_button.setEnabled(False)
        self.delete_node_button.setStyleSheet(
            "QPushButton { background: #e57373; }"
            "QPushButton:hover { background: #d65f5f; }"
        )
        buttons.addWidget(self.edit_node_button)
        buttons.addWidget(self.delete_node_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        return page

    def _build_capture(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        screen_group = QGroupBox("屏幕读取")
        screen_layout = QGridLayout(screen_group)
        self.ocr_engine_combo = QComboBox()
        self.ocr_engine_combo.addItem("自动（优先 Paddle）", "auto")
        self.ocr_engine_combo.addItem("Paddle + mkldnn", "paddle")
        self.ocr_engine_combo.addItem("Rapid（省内存）", "rapid")
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 120)
        self.interval_spin.setValue(2)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setFixedWidth(110)
        self.full_screen_check = QCheckBox("整屏（隐私风险更高）")
        self.screen_start_button = QPushButton("开始后台读屏")
        self.screen_start_button.clicked.connect(self._start_screen)
        self.screen_stop_button = QPushButton("停止读屏")
        self.screen_stop_button.clicked.connect(self._stop_screen)
        self.screen_stop_button.setEnabled(False)
        self.screen_status = QLabel("未运行")
        self.screen_status.setStyleSheet("color: #6b8299;")
        screen_layout.addWidget(QLabel("OCR 引擎"), 0, 0)
        screen_layout.addWidget(self.ocr_engine_combo, 0, 1)
        screen_layout.addWidget(QLabel("检查间隔"), 0, 2)
        screen_layout.addWidget(self.interval_spin, 0, 3)
        screen_layout.addWidget(self.full_screen_check, 1, 0, 1, 2)
        screen_layout.addWidget(self.screen_start_button, 2, 0)
        screen_layout.addWidget(self.screen_stop_button, 2, 1)
        screen_layout.addWidget(self.screen_status, 2, 2, 1, 2)
        manual_shot = QPushButton("手动截图一次")
        manual_shot.clicked.connect(self._manual_screenshot)
        screen_layout.addWidget(manual_shot, 3, 0)
        note = QLabel(
            "全局快捷键 Ctrl+Alt+E 在桌宠运行时生效；读屏默认只读前台窗口。"
        )
        note.setStyleSheet("color: #6b8299; font-size: 12px;")
        screen_layout.addWidget(note, 4, 0, 1, 4)
        layout.addWidget(screen_group)

        audio_group = QGroupBox("录音")
        audio_layout = QGridLayout(audio_group)
        self.audio_source_combo = QComboBox()
        self.audio_source_combo.addItem("麦克风 + 系统声音", "both")
        self.audio_source_combo.addItem("仅麦克风", "microphone")
        self.audio_source_combo.addItem("仅系统声音", "system")
        self.audio_start_button = QPushButton("开始后台录音")
        self.audio_start_button.clicked.connect(self._start_audio)
        self.audio_stop_button = QPushButton("停止录音")
        self.audio_stop_button.clicked.connect(self._stop_audio)
        self.audio_stop_button.setEnabled(False)
        self.audio_status = QLabel("未运行")
        self.audio_status.setStyleSheet("color: #6b8299;")
        audio_layout.addWidget(QLabel("音源"), 0, 0)
        audio_layout.addWidget(self.audio_source_combo, 0, 1)
        audio_layout.addWidget(self.audio_start_button, 1, 0)
        audio_layout.addWidget(self.audio_stop_button, 1, 1)
        audio_layout.addWidget(self.audio_status, 1, 2)
        audio_device = QPushButton("查看音频设备")
        audio_device.clicked.connect(self._list_audio_devices)
        audio_layout.addWidget(audio_device, 2, 0)
        layout.addWidget(audio_group)

        self.capture_log = QPlainTextEdit()
        self.capture_log.setReadOnly(True)
        self.capture_log.setMaximumBlockCount(300)
        layout.addWidget(self.capture_log, 1)
        return page

    def _build_guide(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(
            "<h2>AI 记忆伙伴</h2>"
            "<p>白天采集，夜间整合，次日复习。</p>"
            "<h3>已集成功能</h3>"
            "<ul>"
            "<li>手动文本、PDF/DOCX/TXT/MD 导入</li>"
            "<li>前台窗口屏幕 OCR（Paddle/Rapid，本地私有引擎）</li>"
            "<li>麦克风 + 系统声音录音与 Vosk 本地转写</li>"
            "<li>夜间整合成记忆节点，并预生成次日卡片</li>"
            "<li>桌宠复习、三按钮反馈、重要度/删除</li>"
            "<li>节点查看/直接编辑/LLM 自然语言改写</li>"
            "<li>手动截图快捷键 Ctrl+Alt+E（桌宠运行时）</li>"
            "</ul>"
            "<h3>使用顺序</h3>"
            "<ol>"
            "<li>白天采集或导入资料</li>"
            "<li>晚上点“运行夜间整合”</li>"
            "<li>次日打开桌宠复习</li>"
            "</ol>"
            "<p>完整说明见项目根目录 USAGE.md。</p>"
        )
        layout.addWidget(browser, 1)
        buttons = QHBoxLayout()
        usage = QPushButton("打开 USAGE.md")
        usage.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(ROOT_DIR / "USAGE.md"))
            )
        )
        data_dir = QPushButton("打开数据目录")
        data_dir.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(Path(self.db_path).parent))
            )
        )
        buttons.addWidget(usage)
        buttons.addWidget(data_dir)
        buttons.addStretch()
        layout.addLayout(buttons)
        return page

    # ---------- 数据刷新 ----------

    def refresh_all(self) -> None:
        self.refresh_stats()
        self.refresh_nodes()

    def refresh_stats(self) -> None:
        today = Date.today().isoformat()
        tomorrow = (Date.today() + timedelta(days=1)).isoformat()
        values = {
            "nodes": self.conn.execute(
                "SELECT COUNT(*) AS c FROM node WHERE type != 'day'"
            ).fetchone()["c"],
            "due": self.conn.execute(
                "SELECT COUNT(*) AS c FROM node WHERE status = 'active' "
                "AND due_at IS NOT NULL AND due_at <= ? "
                "AND type NOT IN ('day', 'todo')",
                (today,),
            ).fetchone()["c"],
            "pending": self.conn.execute(
                "SELECT COUNT(*) AS c FROM segment WHERE processed = 0"
            ).fetchone()["c"],
            "cards_today": self.conn.execute(
                "SELECT COUNT(*) AS c FROM daily_card WHERE review_date = ?",
                (today,),
            ).fetchone()["c"],
            "cards_tomorrow": self.conn.execute(
                "SELECT COUNT(*) AS c FROM daily_card WHERE review_date = ?",
                (tomorrow,),
            ).fetchone()["c"],
        }
        for key, value in values.items():
            if key in self.stat_labels:
                self.stat_labels[key].setText(str(value))

    def refresh_nodes(self) -> None:
        search = self.search_edit.text().strip()
        query = (
            "SELECT id, type, title, importance, status, due_at "
            "FROM node WHERE type != 'day'"
        )
        params: list[object] = []
        if search:
            query += " AND (title LIKE ? OR content LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        query += " ORDER BY id DESC LIMIT 500"
        rows = self.conn.execute(query, params).fetchall()
        self.node_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row["id"],
                row["type"],
                row["title"],
                f"{row['importance']:.2f}",
                row["status"],
                row["due_at"] or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(
                        Qt.ItemDataRole.UserRole, int(row["id"])
                    )
                self.node_table.setItem(row_index, column, item)
        self.detail_view.clear()
        self.edit_node_button.setEnabled(False)
        self.delete_node_button.setEnabled(False)

    def _selected_node_id(self) -> int | None:
        row = self.node_table.currentRow()
        if row < 0:
            return None
        item = self.node_table.item(row, 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def _show_selected_node(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        try:
            detail = node_detail(self.conn, node_id)
        except Exception as exc:
            self.detail_view.setPlainText(str(exc))
            return
        self.detail_view.setPlainText(
            json.dumps(detail, ensure_ascii=False, indent=2)
        )
        self.edit_node_button.setEnabled(True)
        self.delete_node_button.setEnabled(True)

    # ---------- 快捷操作 ----------

    def log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _open_pet(self) -> None:
        result = QProcess.startDetached(
            sys.executable,
            ["-m", "backend.desktop_pet", "--db", self.db_path],
            str(ROOT_DIR),
        )
        started = result[0] if isinstance(result, tuple) else result
        if started:
            self.log("已打开桌宠。")
        else:
            self.log("打开桌宠失败。")

    def _manual_text(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self,
            "手动输入",
            "粘贴要记住的文字（会先脱敏，再调用 LLM 整合）：",
        )
        if not ok or not text.strip():
            return
        self.text_button.setEnabled(False)
        self.log("正在整合手动文本…")
        worker = TextIngestWorker(self.db_path, text)
        self._start_worker(worker, self._on_text_done)

    def _on_text_done(self, report, error: str) -> None:
        self.text_button.setEnabled(True)
        if error:
            self.log(f"手动文本整合失败：{error}")
            return
        data = report.to_dict() if report is not None else {}
        self.log(
            "手动文本完成："
            f"新建 {data.get('created', 0)}，"
            f"合并 {data.get('merged', 0)}，"
            f"连线 {data.get('linked', 0)}。"
        )
        self.refresh_all()

    def _import_documents(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "导入资料到记忆",
            "",
            "文档 (*.pdf *.docx *.txt *.md *.markdown);;所有文件 (*.*)",
        )
        if not files:
            return
        answer = QMessageBox.question(
            self,
            "导入方式",
            "立即调用 LLM 整合？\n"
            "选择“否”则只写入片段，等夜间任务整合。",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        defer = answer == QMessageBox.StandardButton.No
        self.import_button.setEnabled(False)
        self.log(
            f"正在导入 {len(files)} 个文件"
            + ("（仅落库）" if defer else "（立即整合）")
            + "…"
        )
        worker = ImportWorker(self.db_path, files, defer)
        self._start_worker(worker, self._on_import_done)

    def _on_import_done(self, message: str, error: str) -> None:
        self.import_button.setEnabled(True)
        if message:
            self.log(message)
        if error:
            self.log(f"导入失败：{error}")
        else:
            self.log("导入完成。可在“记忆库”查看新节点。")
        self.refresh_all()

    def _run_nightly(self) -> None:
        self.nightly_button.setEnabled(False)
        self.log("夜间整合开始，可能需要几十秒…")
        worker = NightlyWorker(self.db_path)
        self._start_worker(worker, self._on_nightly_done)

    def _on_nightly_done(self, report, error: str) -> None:
        self.nightly_button.setEnabled(True)
        if error:
            self.log(f"夜间整合失败：{error}")
            return
        data = report.to_dict() if report is not None else {}
        prep = data.get("preparation") or {}
        self.log(
            "夜间整合完成："
            f"处理 {len(data.get('consolidations', []))} 天，"
            f"次日卡片 {prep.get('total', 0)} 张。"
        )
        self.refresh_all()

    def _start_worker(self, worker: QThread, callback) -> None:
        self._workers.add(worker)
        worker.done.connect(callback)
        worker.finished.connect(
            lambda: self._worker_finished(worker)
        )
        worker.start()

    def _worker_finished(self, worker: QThread) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    # ---------- 记忆编辑 ----------

    def _edit_selected_node(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        try:
            dialog = EditNodeDialog(
                self, self.db_path, node_id, self.conn
            )
        except Exception as exc:
            QMessageBox.critical(self, "无法编辑", str(exc))
            return
        dialog.exec()
        if dialog.changed:
            self.log(f"节点 {node_id} 已修改。")
            self.refresh_all()

    def _delete_selected_node(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        answer = QMessageBox.question(
            self,
            "删除记忆",
            f"确定删除节点 {node_id}？\n"
            "相关边、复习记录和缓存卡片也会删除，不可恢复。",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_node(self.conn, node_id)
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        self.log(f"已删除节点 {node_id}。")
        self.refresh_all()

    # ---------- 采集进程 ----------

    def _process_log(self, name: str, process: QProcess) -> None:
        text = bytes(process.readAllStandardOutput()).decode(
            "utf-8", "replace"
        ).strip()
        if text:
            for line in text.splitlines():
                self.capture_log.appendPlainText(f"[{name}] {line}")

    def _start_screen(self) -> None:
        if (
            self.screen_process is not None
            and self.screen_process.state()
            != QProcess.ProcessState.NotRunning
        ):
            return
        process = QProcess(self)
        self.screen_process = process
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        process.readyReadStandardOutput.connect(
            lambda: self._process_log("screen", process)
        )
        process.finished.connect(self._screen_finished)
        process.errorOccurred.connect(
            lambda _error: self.log("读屏进程启动失败。")
        )
        process.setWorkingDirectory(str(ROOT_DIR))
        args = [
            "-m",
            "backend.screen_capture",
            "--interval",
            str(self.interval_spin.value()),
            "--ocr-backend",
            "local",
            "--ocr-engine",
            self.ocr_engine_combo.currentData(),
        ]
        if self.full_screen_check.isChecked():
            args.append("--full-screen")
        process.start(sys.executable, args)
        self.screen_start_button.setEnabled(False)
        self.screen_stop_button.setEnabled(True)
        self.screen_status.setText("运行中")
        self.log("屏幕读取已启动。")

    def _stop_screen(self) -> None:
        self._stop_process(self.screen_process)
        self.screen_process = None
        self.screen_start_button.setEnabled(True)
        self.screen_stop_button.setEnabled(False)
        self.screen_status.setText("未运行")
        self.log("屏幕读取已停止。")

    def _screen_finished(self, code: int, _status) -> None:
        self.screen_process = None
        self.screen_start_button.setEnabled(True)
        self.screen_stop_button.setEnabled(False)
        self.screen_status.setText(f"已停止（{code}）")
        self.log(f"屏幕读取进程结束（退出码 {code}）。")

    def _start_audio(self) -> None:
        if (
            self.audio_process is not None
            and self.audio_process.state()
            != QProcess.ProcessState.NotRunning
        ):
            return
        process = QProcess(self)
        self.audio_process = process
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        process.readyReadStandardOutput.connect(
            lambda: self._process_log("audio", process)
        )
        process.finished.connect(self._audio_finished)
        process.errorOccurred.connect(
            lambda _error: self.log("录音进程启动失败。")
        )
        process.setWorkingDirectory(str(ROOT_DIR))
        args = [
            "-m",
            "backend.audio_capture",
            "--source",
            self.audio_source_combo.currentData(),
        ]
        process.start(sys.executable, args)
        self.audio_start_button.setEnabled(False)
        self.audio_stop_button.setEnabled(True)
        self.audio_status.setText("运行中")
        self.log("录音已启动。")

    def _stop_audio(self) -> None:
        self._stop_process(self.audio_process)
        self.audio_process = None
        self.audio_start_button.setEnabled(True)
        self.audio_stop_button.setEnabled(False)
        self.audio_status.setText("未运行")
        self.log("录音已停止。")

    def _audio_finished(self, code: int, _status) -> None:
        self.audio_process = None
        self.audio_start_button.setEnabled(True)
        self.audio_stop_button.setEnabled(False)
        self.audio_status.setText(f"已停止（{code}）")
        self.log(f"录音进程结束（退出码 {code}）。")

    def _stop_process(self, process: QProcess | None) -> None:
        if process is None:
            return
        if process.state() == QProcess.ProcessState.NotRunning:
            return
        pid = process.processId()
        if sys.platform == "win32" and pid:
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.kill()
        process.waitForFinished(3000)

    def _manual_screenshot(self) -> None:
        if (
            self.screenshot_process is not None
            and self.screenshot_process.state()
            != QProcess.ProcessState.NotRunning
        ):
            return
        process = QProcess(self)
        self.screenshot_process = process
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        process.readyReadStandardOutput.connect(
            lambda: self._process_log("screenshot", process)
        )
        process.finished.connect(
            lambda code, _status: self.log(
                f"手动截图处理结束（退出码 {code}）。"
            )
        )
        process.setWorkingDirectory(str(ROOT_DIR))
        process.start(
            sys.executable,
            [
                "-m",
                "backend.screen_capture",
                "--once",
                "--ocr-backend",
                "local",
                "--ocr-engine",
                self.ocr_engine_combo.currentData(),
            ],
        )
        self.log("手动截图已触发。")

    def _list_audio_devices(self) -> None:
        process = QProcess(self)
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        process.readyReadStandardOutput.connect(
            lambda: self._process_log("audio-devices", process)
        )
        process.setWorkingDirectory(str(ROOT_DIR))
        process.start(
            sys.executable,
            ["-m", "backend.audio_capture", "--list-devices"],
        )

    # ---------- 关闭与测试 ----------

    def _save_screenshot(self) -> None:
        path = self.screenshot_path
        if not path:
            return
        ok = self.grab().save(path)
        print(f"screenshot {path}: {ok}")

    def _smoke_quit(self) -> None:
        self.close()
        QApplication.instance().quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_process(self.screen_process)
        self._stop_process(self.audio_process)
        self._stop_process(self.screenshot_process)
        for worker in list(self._workers):
            if not worker.wait(2000):
                worker.terminate()
                worker.wait(1000)
        self.conn.close()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="AI 记忆伙伴 - 控制中心"
    )
    parser.add_argument("--db")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    window = ControlCenter(
        args.db or get_db_path(),
        smoke_test=args.smoke_test,
        screenshot_path=args.screenshot,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
