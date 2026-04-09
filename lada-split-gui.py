#!/usr/bin/env python3
"""
lada-split-gui.py
PySide6 GUI wrapper for lada-split.py.
Requires: pip install PySide6

Usage:
    python lada-split-gui.py
"""

import sys
import time
from pathlib import Path
from threading import Thread

from PySide6.QtCore import (
    Qt, QThread, Signal, QObject, QTimer, QMimeData, QSettings,
)
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QCheckBox, QComboBox,
    QListWidget, QListWidgetItem, QProgressBar, QTextEdit,
    QFileDialog, QGroupBox, QGridLayout, QSplitter,
    QMessageBox, QSizePolicy, QFrame,
)

# Import processing library
import lada_split as ls

# ─── Job status constants ─────────────────────────────────────────────────────
STATUS_PENDING   = "Pending"
STATUS_RUNNING   = "Running"
STATUS_DONE      = "Done"
STATUS_FAILED    = "Failed"
STATUS_SKIPPED   = "Skipped"

STATUS_COLORS = {
    STATUS_PENDING: "#888888",
    STATUS_RUNNING: "#4fc3f7",
    STATUS_DONE:    "#81c784",
    STATUS_FAILED:  "#e57373",
    STATUS_SKIPPED: "#ffb74d",
}

# ─── Worker signals ───────────────────────────────────────────────────────────
class WorkerSignals(QObject):
    progress       = Signal(int, int, int, int, float)  # overall_done, total, chunk_done, chunk_total, fps
    phase          = Signal(str)
    log            = Signal(str)
    job_done       = Signal(int, bool)   # queue_index, success
    all_done       = Signal()
    concat_started = Signal()            # fired when concatenating begins (no frame progress)

# ─── Worker thread ────────────────────────────────────────────────────────────
class Worker(QThread):
    def __init__(self, queue: list, opts: ls.JobOptions):
        super().__init__()
        self.queue   = queue   # list of (input_path, output_path)
        self.opts    = opts
        self.signals = WorkerSignals()
        self._stop   = False

    def stop(self):
        self._stop      = True
        ls.QUIT_CLEAN   = True

    def force_stop(self):
        self._stop      = True
        ls.QUIT_FORCE   = True

    def run(self):
        ls.QUIT_CLEAN = False
        ls.QUIT_FORCE = False

        try:
            for idx, (input_path, output_path) in enumerate(self.queue):
                if self._stop:
                    break

                self.signals.phase.emit(f"File {idx+1}/{len(self.queue)}: {input_path.name}")
                self.signals.log.emit(f"─── Starting: {input_path.name}")

                def on_progress_chunk(overall_done, total, chunk_done, chunk_total, fps):
                    self.signals.progress.emit(overall_done, total, chunk_done, chunk_total, fps)

                def on_progress_nochunk(frames_done, total, fps):
                    self.signals.progress.emit(frames_done, total, frames_done, total, fps)

                def on_phase(phase):
                    self.signals.phase.emit(phase)
                    if phase == "Concatenating":
                        self.signals.concat_started.emit()

                def on_log(msg):
                    self.signals.log.emit(msg)

                fn = ls.process_file_nochunk if self.opts.no_chunk else ls.process_file

                if self.opts.no_chunk:
                    success = fn(
                        input_path, output_path, self.opts,
                        on_progress=on_progress_nochunk,
                        on_phase=on_phase,
                        on_log=on_log,
                    )
                else:
                    success = fn(
                        input_path, output_path, self.opts,
                        on_progress=on_progress_chunk,
                        on_phase=on_phase,
                        on_log=on_log,
                    )

                self.signals.job_done.emit(idx, success)

                # Reset quit flags between files
                ls.QUIT_CLEAN = False
                ls.QUIT_FORCE = False

                if not success and not self._stop:
                    self.signals.log.emit(f"Failed: {input_path.name}. Stopping batch.")
                    break

        except Exception as e:
            self.signals.log.emit(f"Unexpected error in worker: {e}")

        finally:
            self.signals.all_done.emit()

# ─── Outlined progress bar ────────────────────────────────────────────────────
class OutlinedProgressBar(QProgressBar):
    """QProgressBar subclass that draws text with a black outline for readability
    regardless of how much of the bar is filled."""

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen, QColor, QPainterPath
        from PySide6.QtCore import QRectF

        # Draw the bar itself (chunk + background) via the normal style machinery
        super().paintEvent(event)

        if not self.isTextVisible():
            return

        text = self.text()
        if not text:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect  = QRectF(self.rect())
        font  = self.font()
        painter.setFont(font)

        path = QPainterPath()
        path.addText(
            0, 0,
            font,
            text,
        )

        # Centre the path in the widget
        br     = path.boundingRect()
        offset_x = (rect.width()  - br.width())  / 2 - br.x()
        offset_y = (rect.height() + br.height()) / 2 - br.height() / 2 - br.y() - 1
        path.translate(offset_x, offset_y)

        # Draw outline
        painter.setPen(QPen(QColor(0, 0, 0), 3, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.strokePath(path, painter.pen())

        # Draw fill
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawPath(path)

        painter.end()

# ─── Drag-and-drop list ───────────────────────────────────────────────────────
class DropListWidget(QListWidget):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setToolTip("Drag and drop .mp4 files or folders here")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        paths = []
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir():
                paths.extend(sorted(path.glob("*.mp4")))
            elif path.suffix.lower() == ".mp4":
                paths.append(path)
        if paths:
            self.files_dropped.emit(paths)
        event.acceptProposedAction()

# ─── Main window ──────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("lada-split")
        self.setMinimumSize(900, 650)
        self.worker      = None
        self._queue      = []
        self._start_time: float | None = None

        # Timer to keep elapsed ticking during phases with no progress signals (e.g. concat)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self._build_ui()
        self._restore_settings()
        self._check_environment()

    # ── Environment check ─────────────────────────────────────────────────────
    def _check_environment(self):
        issues = []
        if ls.FFMPEG is None:
            issues.append(f"ffmpeg not found: {ls._FFMPEG_ERROR}")
        if not ls.LADA_CLI.exists():
            issues.append(f"lada-cli.exe not found: {ls.LADA_CLI}")
        if issues:
            QMessageBox.warning(self, "Environment issues",
                                "\n".join(issues) + "\n\nCheck config in lada-split.py.")

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.splitter, stretch=1)

        # ── Left panel: queue + settings ─────────────────────────────────────
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        # Queue group
        queue_group = QGroupBox("Queue")
        queue_layout = QVBoxLayout(queue_group)

        self.queue_list = DropListWidget()
        self.queue_list.files_dropped.connect(self._on_files_dropped)
        queue_layout.addWidget(self.queue_list)

        queue_btn_row = QHBoxLayout()
        btn_add = QPushButton("Add Files…")
        btn_add.clicked.connect(self._browse_files)
        btn_add_dir = QPushButton("Add Folder…")
        btn_add_dir.clicked.connect(self._browse_folder)
        btn_remove = QPushButton("Remove Selected")
        btn_remove.clicked.connect(self._remove_selected)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear_queue)
        for b in [btn_add, btn_add_dir, btn_remove, btn_clear]:
            queue_btn_row.addWidget(b)
        queue_layout.addLayout(queue_btn_row)
        left_layout.addWidget(queue_group, stretch=1)

        # Output group
        out_group = QGroupBox("Output")
        out_layout = QGridLayout(out_group)

        out_layout.addWidget(QLabel("Output directory:"), 0, 0)
        self.out_dir_edit = QLineEdit()
        self.out_dir_edit.setPlaceholderText("Required — select output directory")
        out_layout.addWidget(self.out_dir_edit, 0, 1)
        btn_out_dir = QPushButton("Browse…")
        btn_out_dir.clicked.connect(self._browse_output_dir)
        out_layout.addWidget(btn_out_dir, 0, 2)

        out_layout.addWidget(QLabel("Output pattern:"), 1, 0)
        self.out_pattern_edit = QLineEdit(ls.OUTPUT_PATTERN_DEFAULT)
        self.out_pattern_edit.setToolTip(
            "Use {orig_file_name} as placeholder. Extension taken from source."
        )
        out_layout.addWidget(self.out_pattern_edit, 1, 1, 1, 2)

        left_layout.addWidget(out_group)

        # Settings group
        settings_group = QGroupBox("Settings")
        settings_layout = QGridLayout(settings_group)

        self.no_chunk_cb = QCheckBox("No-chunk mode (process whole file at once)")
        self.no_chunk_cb.setToolTip(
            "Skip splitting — lada-cli processes the full file in one call.\n"
            "Faster but no early preview; retry on crash starts from scratch."
        )
        settings_layout.addWidget(self.no_chunk_cb, 0, 0, 1, 3)

        self.downscale_cb = QCheckBox("Pre-downscale to:")
        self.downscale_cb.stateChanged.connect(self._on_downscale_toggle)
        settings_layout.addWidget(self.downscale_cb, 1, 0)
        self.downscale_combo = QComboBox()
        self.downscale_combo.addItems(["720p", "540p", "480p"])
        self.downscale_combo.setEnabled(False)
        settings_layout.addWidget(self.downscale_combo, 1, 1)
        settings_layout.addWidget(QLabel(""), 1, 2)

        self.skip_upscale_cb = QCheckBox("Skip upscale after processing")
        self.skip_upscale_cb.setEnabled(False)
        self.skip_upscale_cb.setToolTip("Output stays at downscaled resolution.")
        self.downscale_cb.stateChanged.connect(
            lambda s: self.skip_upscale_cb.setEnabled(s == Qt.CheckState.Checked.value)
        )
        settings_layout.addWidget(self.skip_upscale_cb, 2, 0, 1, 3)

        self.delete_input_cb = QCheckBox("Delete input file after successful completion")
        settings_layout.addWidget(self.delete_input_cb, 3, 0, 1, 3)

        self.shutdown_cb = QCheckBox(
            f"Shutdown Windows after completion "
            f"(only between {ls.SHUTDOWN_WINDOW_START:02d}:00–{ls.SHUTDOWN_WINDOW_END:02d}:00)"
        )
        settings_layout.addWidget(self.shutdown_cb, 4, 0, 1, 3)

        settings_layout.addWidget(QLabel("Extra lada-cli args:"), 5, 0)
        self.extra_args_edit = QLineEdit()
        self.extra_args_edit.setPlaceholderText('e.g. --max-clip-length 60')
        settings_layout.addWidget(self.extra_args_edit, 5, 1, 1, 2)

        left_layout.addWidget(settings_group)

        # Run buttons
        run_row = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.setFixedHeight(36)
        self.btn_start.clicked.connect(self._start)
        font = self.btn_start.font()
        font.setBold(True)
        self.btn_start.setFont(font)

        self.btn_stop = QPushButton("⏹  Stop")
        self.btn_stop.setFixedHeight(36)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)

        self.btn_force_stop = QPushButton("✕  Force Stop")
        self.btn_force_stop.setFixedHeight(36)
        self.btn_force_stop.setEnabled(False)
        self.btn_force_stop.clicked.connect(self._force_stop)

        run_row.addWidget(self.btn_start, stretch=2)
        run_row.addWidget(self.btn_stop, stretch=1)
        run_row.addWidget(self.btn_force_stop, stretch=1)
        left_layout.addLayout(run_row)

        self.splitter.addWidget(left)

        # ── Right panel: progress + log ───────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        # Progress group
        prog_group = QGroupBox("Progress")
        prog_layout = QVBoxLayout(prog_group)

        self.phase_label = QLabel("Idle")
        self.phase_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.phase_label.font()
        font.setBold(True)
        self.phase_label.setFont(font)
        prog_layout.addWidget(self.phase_label)

        self.overall_bar = OutlinedProgressBar()
        self.overall_bar.setFormat("Overall: %v / %m frames (%p%)")
        self.overall_bar.setTextVisible(True)
        self.overall_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #555; border-radius: 3px; background: #2a2a2a; }
            QProgressBar::chunk { background-color: #4fc3f7; border-radius: 3px; }
        """)
        prog_layout.addWidget(self.overall_bar)

        self.chunk_bar = OutlinedProgressBar()
        self.chunk_bar.setFormat("Current: %v / %m frames (%p%)")
        self.chunk_bar.setTextVisible(True)
        self.chunk_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #555; border-radius: 3px; background: #2a2a2a; }
            QProgressBar::chunk { background-color: #81c784; border-radius: 3px; }
        """)
        prog_layout.addWidget(self.chunk_bar)

        # Stats row: FPS | Elapsed | ETA
        stats_row = QHBoxLayout()
        self.fps_label     = QLabel("FPS: —")
        self.elapsed_label = QLabel("Elapsed: —")
        self.eta_label     = QLabel("ETA: —")
        for lbl in [self.fps_label, self.elapsed_label, self.eta_label]:
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stats_row.addWidget(lbl)
        prog_layout.addLayout(stats_row)

        # Queue status summary
        self.queue_status_label = QLabel("")
        self.queue_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        prog_layout.addWidget(self.queue_status_label)

        right_layout.addWidget(prog_group)

        # Log group
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_view)

        btn_clear_log = QPushButton("Clear Log")
        btn_clear_log.clicked.connect(self.log_view.clear)
        log_layout.addWidget(btn_clear_log, alignment=Qt.AlignmentFlag.AlignRight)

        right_layout.addWidget(log_group, stretch=1)

        self.splitter.addWidget(right)
        self.splitter.setSizes([440, 440])

    # ── Queue management ──────────────────────────────────────────────────────
    def _add_paths(self, paths: list[Path]):
        existing = set()
        for i in range(self.queue_list.count()):
            existing.add(self.queue_list.item(i).data(Qt.ItemDataRole.UserRole))
        for path in paths:
            if str(path) not in existing:
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(str(path))
                self._set_item_status(item, STATUS_PENDING)
                self.queue_list.addItem(item)
                existing.add(str(path))

    def _on_files_dropped(self, paths: list[Path]):
        self._add_paths(paths)

    def _browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select video files", "", "Video files (*.mp4)"
        )
        if files:
            self._add_paths([Path(f) for f in files])

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if folder:
            self._add_paths(sorted(Path(folder).glob("*.mp4")))

    def _remove_selected(self):
        for item in self.queue_list.selectedItems():
            self.queue_list.takeItem(self.queue_list.row(item))

    def _clear_queue(self):
        self.queue_list.clear()

    def _browse_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output directory")
        if folder:
            self.out_dir_edit.setText(folder)

    def _set_item_status(self, item: QListWidgetItem, status: str):
        color = STATUS_COLORS.get(status, "#888888")
        item.setForeground(QColor(color))
        name = item.data(Qt.ItemDataRole.UserRole)
        item.setText(f"[{status}]  {Path(name).name}")

    def _on_downscale_toggle(self, state):
        enabled = state == Qt.CheckState.Checked.value
        self.downscale_combo.setEnabled(enabled)

    # ── Validation ────────────────────────────────────────────────────────────
    def _validate(self) -> bool:
        if self.queue_list.count() == 0:
            QMessageBox.warning(self, "No files", "Add at least one file to the queue.")
            return False
        if not self.out_dir_edit.text().strip():
            QMessageBox.warning(self, "No output directory", "Please select an output directory.")
            return False
        out_dir = Path(self.out_dir_edit.text().strip())
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True)
            except Exception as e:
                QMessageBox.warning(self, "Output directory error", str(e))
                return False
        pattern = self.out_pattern_edit.text().strip()
        if "{orig_file_name}" not in pattern:
            QMessageBox.warning(self, "Invalid pattern",
                                "Output pattern must contain {orig_file_name}.")
            return False
        return True

    # ── Start / Stop ──────────────────────────────────────────────────────────
    def _build_queue(self) -> list:
        out_dir  = Path(self.out_dir_edit.text().strip())
        pattern  = self.out_pattern_edit.text().strip()
        queue    = []
        for i in range(self.queue_list.count()):
            item       = self.queue_list.item(i)
            input_path = Path(item.data(Qt.ItemDataRole.UserRole))
            output_path = ls.resolve_output_path(input_path, None, str(out_dir), pattern)
            queue.append((input_path, output_path))
        return queue

    def _build_opts(self) -> ls.JobOptions:
        return ls.JobOptions(
            no_chunk       = self.no_chunk_cb.isChecked(),
            pre_downscale  = self.downscale_combo.currentText() if self.downscale_cb.isChecked() else None,
            skip_upscale   = self.skip_upscale_cb.isChecked(),
            delete_input   = self.delete_input_cb.isChecked(),
            shutdown_after = self.shutdown_cb.isChecked(),
            extra_args     = self.extra_args_edit.text().split() if self.extra_args_edit.text().strip() else [],
        )

    def _start(self):
        if not self._validate():
            return

        self._queue = self._build_queue()
        opts        = self._build_opts()

        # Reset all items to pending
        for i in range(self.queue_list.count()):
            self._set_item_status(self.queue_list.item(i), STATUS_PENDING)

        # Reset progress
        self._elapsed_timer.stop()
        self.overall_bar.setMaximum(1)
        self.overall_bar.setValue(0)
        self.chunk_bar.setMaximum(1)
        self.chunk_bar.setValue(0)
        self.fps_label.setText("FPS: —")
        self.elapsed_label.setText("Elapsed: —")
        self.eta_label.setText("ETA: —")
        self.phase_label.setText("Starting…")
        self.log_view.clear()
        self._start_time = time.time()

        # UI state
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_force_stop.setEnabled(True)

        self.worker = Worker(self._queue, opts)
        self.worker.signals.progress.connect(self._on_progress)
        self.worker.signals.phase.connect(self._on_phase)
        self.worker.signals.log.connect(self._on_log)
        self.worker.signals.job_done.connect(self._on_job_done)
        self.worker.signals.all_done.connect(self._on_all_done)
        self.worker.signals.concat_started.connect(self._on_concat_started)
        self.worker.start()
        self._elapsed_timer.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self._on_log("Stop requested — waiting for current process to finish...")
            self.btn_stop.setEnabled(False)

    def _force_stop(self):
        if self.worker:
            self.worker.force_stop()
            self._on_log("Force stop requested — killing current process...")
            self.btn_stop.setEnabled(False)
            self.btn_force_stop.setEnabled(False)

    # ── Signal handlers ───────────────────────────────────────────────────────
    def _on_progress(self, overall_done: int, total: int,
                     chunk_done: int, chunk_total: int, fps: float):
        if total > 0:
            self.overall_bar.setMaximum(total)
            self.overall_bar.setValue(overall_done)
        if chunk_total > 0:
            self.chunk_bar.setMaximum(chunk_total)
            self.chunk_bar.setValue(chunk_done)

        if fps > 0:
            self.fps_label.setText(f"FPS: {fps:.1f}")

        if self._start_time is not None:
            elapsed_secs = time.time() - self._start_time
            self.elapsed_label.setText(f"Elapsed: {ls.fmt_duration(elapsed_secs)}")

            if fps > 0 and total > 0 and overall_done > 0:
                remaining_frames = total - overall_done
                eta_secs         = remaining_frames / fps
                self.eta_label.setText(f"ETA: {ls.fmt_duration(eta_secs)}")
            else:
                self.eta_label.setText("ETA: —")

    def _on_phase(self, phase: str):
        self.phase_label.setText(phase)

    def _on_log(self, message: str):
        self.log_view.append(message)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )

    def _on_job_done(self, idx: int, success: bool):
        item   = self.queue_list.item(idx)
        status = STATUS_DONE if success else STATUS_FAILED
        self._set_item_status(item, status)

        done    = sum(1 for i in range(self.queue_list.count())
                      if STATUS_DONE in self.queue_list.item(i).text())
        failed  = sum(1 for i in range(self.queue_list.count())
                      if STATUS_FAILED in self.queue_list.item(i).text())
        total   = self.queue_list.count()
        self.queue_status_label.setText(
            f"Queue: {done} done, {failed} failed, {total - done - failed} pending"
        )

    def _on_all_done(self):
        self._elapsed_timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_force_stop.setEnabled(False)
        self.phase_label.setText("Done")
        self.eta_label.setText("ETA: —")
        if self._start_time is not None:
            total_secs = time.time() - self._start_time
            self.elapsed_label.setText(f"Elapsed: {ls.fmt_duration(total_secs)}")
        self._on_log("─── All jobs finished.")
        self.worker = None

    def _tick_elapsed(self):
        """Called every second by timer — keeps elapsed label updating during concat etc."""
        if self._start_time is not None:
            elapsed_secs = time.time() - self._start_time
            self.elapsed_label.setText(f"Elapsed: {ls.fmt_duration(elapsed_secs)}")

    def _on_concat_started(self):
        """Called when concatenating begins — pulse the bars to show activity."""
        self.overall_bar.setMaximum(0)  # indeterminate mode
        self.chunk_bar.setMaximum(0)

    # ── Settings persistence ──────────────────────────────────────────────────
    def _save_settings(self):
        s = QSettings("lada-split", "lada-split-gui")
        s.setValue("output_dir",     self.out_dir_edit.text())
        s.setValue("output_pattern", self.out_pattern_edit.text())
        s.setValue("no_chunk",       self.no_chunk_cb.isChecked())
        s.setValue("pre_downscale",  self.downscale_cb.isChecked())
        s.setValue("downscale_res",  self.downscale_combo.currentText())
        s.setValue("skip_upscale",   self.skip_upscale_cb.isChecked())
        s.setValue("delete_input",   self.delete_input_cb.isChecked())
        s.setValue("shutdown_after", self.shutdown_cb.isChecked())
        s.setValue("extra_args",     self.extra_args_edit.text())
        s.setValue("splitter",       self.splitter.saveState())
        s.setValue("window_geometry", self.saveGeometry())

    def _restore_settings(self):
        s = QSettings("lada-split", "lada-split-gui")
        self.out_dir_edit.setText(s.value("output_dir", ""))
        self.out_pattern_edit.setText(s.value("output_pattern", ls.OUTPUT_PATTERN_DEFAULT))
        self.no_chunk_cb.setChecked(s.value("no_chunk", False, type=bool))
        pre_downscale = s.value("pre_downscale", False, type=bool)
        self.downscale_cb.setChecked(pre_downscale)
        self.downscale_combo.setEnabled(pre_downscale)
        self.skip_upscale_cb.setEnabled(pre_downscale)
        idx = self.downscale_combo.findText(s.value("downscale_res", "720p"))
        if idx >= 0:
            self.downscale_combo.setCurrentIndex(idx)
        self.skip_upscale_cb.setChecked(s.value("skip_upscale", False, type=bool))
        self.delete_input_cb.setChecked(s.value("delete_input", False, type=bool))
        self.shutdown_cb.setChecked(s.value("shutdown_after", False, type=bool))
        self.extra_args_edit.setText(s.value("extra_args", ""))
        if s.contains("splitter"):
            self.splitter.restoreState(s.value("splitter"))
        if s.contains("window_geometry"):
            self.restoreGeometry(s.value("window_geometry"))

    # ── Close ─────────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "Processing in progress",
                "A job is currently running. Force stop and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.worker.force_stop()
                self.worker.wait(3000)
                self._save_settings()
                event.accept()
            else:
                event.ignore()
        else:
            self._save_settings()
            event.accept()

# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("lada-split")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()