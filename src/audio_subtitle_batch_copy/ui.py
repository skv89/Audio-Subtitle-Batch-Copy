from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import (
    QMimeData,
    QObject,
    QRectF,
    QRunnable,
    QStandardPaths,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .constants import APP_NAME, APP_VERSION, DEFAULT_SUFFIX, MAX_FILES_PER_SIDE
from .media import (
    FFmpegToolchain,
    MediaProbeError,
    ToolchainError,
    collect_media_files,
    discover_toolchain,
    probe_media,
    stream_display_label,
)
from .models import BatchState, DestinationEntry, MediaInfo, SourceEntry, TrackSelection
from .planner import (
    PlanError,
    PreparedJob,
    apply_reliable_default_container,
    assign_fresh_output_paths,
    normalized_path,
    plan_output,
)
from .processor import BatchProcessor, JobResult

NO_DEFAULT_SUBTITLE_TOKEN = "no-default-subtitle"


def _decode_track_selection(
    data: object,
    *,
    allow_no_default: bool,
) -> tuple[bool, TrackSelection | None]:
    if allow_no_default and data == NO_DEFAULT_SUBTITLE_TOKEN:
        return True, None
    if not isinstance(data, str):
        return False, None
    try:
        return True, TrackSelection.from_token(data)
    except ValueError:
        return False, None


class VisibleCheckBox(QCheckBox):
    """Theme-independent checkbox with a high-contrast box and check mark."""

    def paintEvent(self, event: QPaintEvent) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_CheckBox, option)

        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator, option, self
        )
        box = QRectF(indicator).adjusted(1.0, 1.0, -1.0, -1.0)
        checked = self.checkState() == Qt.CheckState.Checked
        enabled = self.isEnabled()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#0f56ad" if checked else "#52647a"), 2.0))
        painter.setBrush(QColor("#1769d2" if checked else ("#ffffff" if enabled else "#eef1f5")))
        painter.drawRoundedRect(box, 2.5, 2.5)
        if checked:
            check = QPainterPath()
            check.moveTo(box.left() + box.width() * 0.23, box.top() + box.height() * 0.53)
            check.lineTo(box.left() + box.width() * 0.43, box.top() + box.height() * 0.73)
            check.lineTo(box.left() + box.width() * 0.78, box.top() + box.height() * 0.30)
            pen = QPen(QColor("#ffffff"), 2.1)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(check)


class BatchTableWidget(QTableWidget):
    INTERNAL_REORDER_MIME = "application/x-audio-subtitle-batch-copy-entry"
    files_dropped = Signal(int, object)
    reorder_requested = Signal(int, str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(False)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self._drop_indicator: tuple[int, int] | None = None

    def drop_column_for_x(self, x: int) -> int:
        column = self.columnAt(x)
        return column if column in (0, 1) else -1

    @classmethod
    def internal_drag_details(cls, mime_data: QMimeData) -> tuple[int, str] | None:
        if not mime_data.hasFormat(cls.INTERNAL_REORDER_MIME):
            return None
        try:
            raw_payload = mime_data.data(cls.INTERNAL_REORDER_MIME).data()
            payload = bytes(raw_payload).decode("utf-8")
            column_text, entry_id = payload.split("\n", 1)
            column = int(column_text)
        except (UnicodeDecodeError, ValueError):
            return None
        if column not in (0, 1) or not entry_id:
            return None
        return column, entry_id

    def insertion_index_for_y(self, y: int) -> int:
        row = self.rowAt(y)
        if row < 0:
            return 0 if y <= 0 else self.rowCount()
        rect = self.visualRect(self.model().index(row, 0))
        return row if y < rect.center().y() else row + 1

    def startDrag(self, _supported_actions: Qt.DropAction) -> None:
        index = self.currentIndex()
        if not index.isValid() or index.column() not in (0, 1):
            return
        item = self.item(index.row(), index.column())
        if item is None:
            return
        entry_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry_id, str) or not entry_id:
            return
        mime_data = QMimeData()
        mime_data.setData(
            self.INTERNAL_REORDER_MIME,
            f"{index.column()}\n{entry_id}".encode(),
        )
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        item_rect = self.visualItemRect(item)
        if item_rect.isValid():
            drag.setPixmap(self.viewport().grab(item_rect))
        drag.exec(Qt.DropAction.MoveAction)

    def _set_drop_indicator(self, indicator: tuple[int, int] | None) -> None:
        if indicator == self._drop_indicator:
            return
        self._drop_indicator = indicator
        self.viewport().update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self._drop_indicator is None:
            return
        column, insertion_index = self._drop_indicator
        left = self.columnViewportPosition(column) + 2
        right = left + self.columnWidth(column) - 5
        if self.rowCount() == 0 or insertion_index <= 0:
            y = 1
        elif insertion_index >= self.rowCount():
            y = self.visualRect(self.model().index(self.rowCount() - 1, column)).bottom()
        else:
            y = self.visualRect(self.model().index(insertion_index, column)).top()
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor("#1769d2"), 3))
        painter.drawLine(left, y, right, y)
        painter.end()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.internal_drag_details(event.mimeData()) is not None:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        elif event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        details = self.internal_drag_details(event.mimeData())
        if details is not None:
            origin_column, _entry_id = details
            point = event.position().toPoint()
            column = self.drop_column_for_x(point.x())
            if column == origin_column:
                insertion_index = self.insertion_index_for_y(point.y())
                self._set_drop_indicator((column, insertion_index))
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
            else:
                self._set_drop_indicator(None)
                event.ignore()
            return
        if (
            event.mimeData().hasUrls()
            and self.drop_column_for_x(event.position().toPoint().x()) >= 0
        ):
            self._set_drop_indicator(None)
            event.acceptProposedAction()
        else:
            self._set_drop_indicator(None)
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drop_indicator(None)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        details = self.internal_drag_details(event.mimeData())
        if details is not None:
            origin_column, entry_id = details
            point = event.position().toPoint()
            column = self.drop_column_for_x(point.x())
            self._set_drop_indicator(None)
            if column == origin_column:
                self.reorder_requested.emit(
                    column,
                    entry_id,
                    self.insertion_index_for_y(point.y()),
                )
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
            else:
                event.ignore()
            return
        column = self.drop_column_for_x(event.position().toPoint().x())
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if column >= 0 and paths:
            self.files_dropped.emit(column, paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class ProbeSignals(QObject):
    finished = Signal(str, str, object, object)


class ProbeTask(QRunnable):
    def __init__(
        self,
        side: Literal["source", "destination"],
        entry_id: str,
        path: Path,
        ffprobe: Path,
    ) -> None:
        super().__init__()
        self.side = side
        self.entry_id = entry_id
        self.path = path
        self.ffprobe = ffprobe
        self.signals = ProbeSignals()

    @Slot()
    def run(self) -> None:
        try:
            info = probe_media(self.ffprobe, self.path)
        except Exception as exc:  # QRunnable must return failures to the main thread.
            self.signals.finished.emit(self.side, self.entry_id, None, str(exc))
            return
        self.signals.finished.emit(self.side, self.entry_id, info, None)


class ProcessingWorker(QObject):
    row_started = Signal(int)
    row_progress = Signal(int, float)
    row_finished = Signal(object)
    log = Signal(str)
    finished = Signal(object)

    def __init__(self, processor: BatchProcessor, jobs: list[PreparedJob]) -> None:
        super().__init__()
        self.processor = processor
        self.jobs = jobs

    @Slot()
    def run(self) -> None:
        results = self.processor.run_batch(
            self.jobs,
            row_started=self.row_started.emit,
            row_progress=self.row_progress.emit,
            row_finished=self.row_finished.emit,
            log=self.log.emit,
        )
        self.finished.emit(results)


class OverwriteDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        *,
        row_number: int,
        path: Path,
        replaces_destination: bool,
        fallback: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm file replacement")
        self.setModal(True)
        layout = QVBoxLayout(self)
        title = QLabel(
            "Destination video will be overwritten"
            if replaces_destination
            else "Output file already exists"
        )
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        if replaces_destination:
            explanation = (
                f"Row {row_number} has a blank suffix and uses the destination folder. "
                "A temporary file will be verified first, then the original destination "
                "will be replaced only after a successful direct copy."
            )
        elif fallback:
            explanation = (
                f"The MKV fallback path for row {row_number} already exists. It is used only "
                "if the destination container rejects direct stream copy."
            )
        else:
            explanation = f"The planned output for row {row_number} already exists."
        label = QLabel(f"{explanation}\n\n{path}")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        self.apply_all = VisibleCheckBox(
            "Apply this choice to all remaining file-replacement warnings"
        )
        layout.addWidget(self.apply_all)
        buttons = QDialogButtonBox()
        self.replace_button = buttons.addButton("Replace", QDialogButtonBox.ButtonRole.AcceptRole)
        self.skip_button = buttons.addButton("Skip", QDialogButtonBox.ButtonRole.RejectRole)
        self.replace_button.setDefault(False)
        self.skip_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, toolchain: FFmpegToolchain | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1520, 920)
        self.setMinimumSize(1120, 720)
        self.state = BatchState()
        self.toolchain: FFmpegToolchain | None = toolchain
        self.toolchain_error: str | None = None
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(4)
        self._source_sort_ascending = True
        self._destination_sort_ascending = True
        self._last_sort_column: int | None = None
        self._render_pending = False
        self._processing = False
        self._pending_close = False
        self._processor: BatchProcessor | None = None
        self._process_thread: QThread | None = None
        self._process_worker: ProcessingWorker | None = None
        self._jobs: list[PreparedJob] = []
        self._job_positions: dict[int, int] = {}
        self._row_progress_values: dict[int, float] = {}
        self._row_results: dict[int, JobResult] = {}
        self._audio_default_choices: dict[
            tuple[str, str], TrackSelection | None
        ] = {}
        self._subtitle_default_choices: dict[
            tuple[str, str], TrackSelection | None
        ] = {}
        self._output_directory: Path | None = None
        self._log_handle: Any = None
        self._build_ui()
        self._apply_style()
        if self.toolchain is None:
            self._discover_toolchain()
        else:
            self._show_toolchain_status()
        self.render_table()

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        heading_row = QHBoxLayout()
        heading = QLabel(APP_NAME)
        heading.setObjectName("appHeading")
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        self.tool_status = QLabel()
        self.tool_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        heading_row.addWidget(self.tool_status)
        about = QPushButton("About")
        about.clicked.connect(self._show_about)
        heading_row.addWidget(about)
        root.addLayout(heading_row)

        guidance = QLabel(
            "Drop files into Source or Destination, or use the add buttons. Drag a filename "
            "up or down within its own column to adjust the pairing. Click a file heading to "
            "sort that side; each visible row is one processing pair."
        )
        guidance.setWordWrap(True)
        guidance.setObjectName("guidance")
        root.addWidget(guidance)

        self.input_controls = QWidget()
        controls_layout = QGridLayout(self.input_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setHorizontalSpacing(10)
        source_group = self._make_side_group("Source video or audio", "source")
        destination_group = self._make_side_group("Destination video", "destination")
        controls_layout.addWidget(source_group, 0, 0)
        controls_layout.addWidget(destination_group, 0, 1)
        clear_button = QPushButton("Clear all")
        clear_button.clicked.connect(self._clear_all)
        controls_layout.addWidget(clear_button, 0, 2, alignment=Qt.AlignmentFlag.AlignBottom)
        controls_layout.setColumnStretch(0, 1)
        controls_layout.setColumnStretch(1, 1)
        root.addWidget(self.input_controls)

        self.table = BatchTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            [
                "Source (sort heading · drag files)",
                "Destination (sort heading · drag files)",
                "Default audio",
                "Default subtitle",
            ]
        )
        self.table.verticalHeader().setDefaultSectionSize(92)
        self.table.verticalHeader().setMinimumWidth(42)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._header_clicked)
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 340)
        self.table.setColumnWidth(1, 340)
        self.table.setColumnWidth(2, 365)
        self.table.setColumnWidth(3, 385)
        self.table.files_dropped.connect(self._files_dropped)
        self.table.reorder_requested.connect(self._reorder_entry)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_args: self._table_selection_changed()
        )
        root.addWidget(self.table, 1)

        output_group = QGroupBox("Output and processing")
        output_layout = QGridLayout(output_group)
        output_layout.addWidget(QLabel("Filename suffix:"), 0, 0)
        self.suffix_edit = QLineEdit(DEFAULT_SUFFIX)
        self.suffix_edit.setPlaceholderText("Blank = no suffix")
        self.suffix_edit.setMaximumWidth(300)
        output_layout.addWidget(self.suffix_edit, 0, 1)
        self.destination_folders_check = VisibleCheckBox("Save beside each destination file")
        self.destination_folders_check.setChecked(True)
        self.destination_folders_check.toggled.connect(self._destination_folder_mode_changed)
        output_layout.addWidget(self.destination_folders_check, 0, 2)
        self.fresh_output_paths_check = VisibleCheckBox(
            "Use a fresh filename if an output already exists"
        )
        self.fresh_output_paths_check.setChecked(True)
        self.fresh_output_paths_check.setToolTip(
            "Keeps the existing file and adds a fresh identity to the new filename. "
            "This preserves the older output and can avoid pathname-based media-player "
            "history. Destination-overwrite jobs are never renamed."
        )
        output_layout.addWidget(self.fresh_output_paths_check, 1, 0, 1, 3)
        self.reliable_defaults_check = VisibleCheckBox(
            "Reliable MPC-HC/LAV audio defaults (use MKV when needed)"
        )
        self.reliable_defaults_check.setChecked(False)
        self.reliable_defaults_check.setToolTip(
            "Optional MPC-HC/LAV workaround. Some versions ignore MP4/MOV audio default "
            "flags when tracks share one language tag, then choose a different track by "
            "audio quality. Check this to copy affected rows directly to MKV. Leave it "
            "unchecked to preserve the destination container for players such as VLC and "
            "Windows Media Player that honor the MP4/MOV default."
        )
        output_layout.addWidget(self.reliable_defaults_check, 2, 0, 1, 3)
        self.output_folder_edit = QLineEdit("Same folder as each destination")
        self.output_folder_edit.setReadOnly(True)
        self.output_folder_edit.setEnabled(False)
        output_layout.addWidget(self.output_folder_edit, 3, 0, 1, 3)
        self.output_folder_button = QPushButton("Choose output folder…")
        self.output_folder_button.setEnabled(False)
        self.output_folder_button.clicked.connect(self._choose_output_folder)
        output_layout.addWidget(self.output_folder_button, 3, 3)
        self.open_output_folder_button = QPushButton("Open output folder")
        self.open_output_folder_button.setEnabled(False)
        self.open_output_folder_button.setToolTip(
            "Opens the selected destination row's folder; if none is selected, "
            "opens the first destination folder."
        )
        self.open_output_folder_button.clicked.connect(self._open_output_folder)
        output_layout.addWidget(self.open_output_folder_button, 3, 4)
        self.process_button = QPushButton("Process batch")
        self.process_button.setObjectName("primaryButton")
        self.process_button.clicked.connect(self._prepare_and_start)
        output_layout.addWidget(self.process_button, 0, 3)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_processing)
        output_layout.addWidget(self.cancel_button, 0, 4)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        output_layout.addWidget(self.progress, 4, 0, 1, 5)
        output_layout.setColumnStretch(2, 1)
        root.addWidget(output_group)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setPlaceholderText("Batch details and FFmpeg diagnostics appear here.")
        self.log_view.setFixedHeight(135)
        root.addWidget(self.log_view)

    def _make_side_group(self, title: str, side: Literal["source", "destination"]) -> QGroupBox:
        group = QGroupBox(title)
        layout = QHBoxLayout(group)
        add_files = QPushButton("Add files…")
        add_files.clicked.connect(lambda: self._browse_files(side))
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(lambda: self._browse_folder(side))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self._remove_selected(side))
        layout.addWidget(add_files)
        layout.addWidget(add_folder)
        layout.addWidget(remove)
        layout.addStretch(1)
        return group

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#centralRoot { background: #f5f7fb; color: #172033; }
            QLabel { color: #172033; }
            QLabel#appHeading { font-size: 22px; font-weight: 650; color: #172033; }
            QLabel#guidance { color: #46536b; padding: 3px 0 4px 0; }
            QLabel#dialogTitle { font-size: 17px; font-weight: 650; color: #7d2800; }
            QGroupBox { color: #172033; font-weight: 650; border: 1px solid #c8d2e1;
                        border-radius: 7px; margin-top: 8px; padding-top: 8px;
                        background: #ffffff; }
            QGroupBox::title { color: #172033; background: #ffffff;
                               subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QPushButton { color: #172033; background: #eef2f7; border: 1px solid #aeb9c9;
                          border-radius: 5px; min-height: 27px; padding: 2px 11px; }
            QPushButton:hover { background: #e1e8f2; border-color: #7f8fa5; }
            QPushButton:pressed { background: #d5deeb; }
            QPushButton:disabled { color: #717b8d; background: #e8ebf0;
                                   border-color: #cbd2dc; }
            QPushButton#primaryButton { background: #1769d2; color: white; border: 0;
                                        border-radius: 5px; font-weight: 650; min-height: 31px; }
            QPushButton#primaryButton:hover { background: #125dbf; }
            QPushButton#primaryButton:pressed { background: #0e4f9f; }
            QPushButton#primaryButton:disabled { background: #9ca9ba; }
            QLineEdit, QComboBox { color: #172033; background: #ffffff;
                                   border: 1px solid #aeb9c9; border-radius: 4px;
                                   selection-background-color: #cfe3ff;
                                   selection-color: #102a56; }
            QLineEdit { min-height: 26px; padding: 1px 6px; }
            QLineEdit:disabled { color: #5f6979; background: #eef1f5;
                                 border-color: #cbd2dc; }
            QComboBox { min-height: 27px; padding: 1px 7px; }
            QComboBox:disabled { color: #6c7584; background: #eef1f5;
                                 border-color: #cbd2dc; }
            QComboBox QAbstractItemView { color: #172033; background: #ffffff;
                                          selection-background-color: #dbeafe;
                                          selection-color: #102a56; }
            QCheckBox { color: #172033; background: transparent; spacing: 7px; }
            QCheckBox:disabled { color: #717b8d; }
            QCheckBox::indicator { width: 18px; height: 18px; background: #ffffff;
                                   border: 2px solid #52647a; border-radius: 3px; }
            QCheckBox::indicator:hover { border-color: #1769d2; }
            QCheckBox::indicator:checked { background: #1769d2;
                                           border-color: #0f56ad; }
            QCheckBox::indicator:disabled { background: #eef1f5;
                                            border-color: #8b96a8; }
            QTableWidget { color: #172033; background: #ffffff;
                           alternate-background-color: #f7f9fc;
                           gridline-color: #d5ddea; selection-background-color: #dbeafe;
                           selection-color: #102a56; border: 1px solid #c8d2e1; }
            QTableWidget::item { color: #172033; padding: 5px; }
            QTableWidget::item:selected { color: #102a56; background: #dbeafe; }
            QHeaderView::section { background: #e8edf5; color: #26334a; padding: 7px;
                                   border: 0; border-right: 1px solid #c8d0de; font-weight: 650; }
            QTableCornerButton::section { background: #e8edf5; border: 0;
                                          border-right: 1px solid #c8d0de; }
            QWidget#trackCell { color: #172033; background: #ffffff; }
            QWidget#trackCell[alternate="true"] { background: #f7f9fc; }
            QWidget#trackCell[selected="true"] { background: #dbeafe; }
            QWidget#trackCell QCheckBox { color: #172033; background: transparent; }
            QProgressBar { color: #172033; background: #eef2f7; border: 1px solid #aeb9c9;
                           border-radius: 4px; min-height: 22px; text-align: center; }
            QProgressBar::chunk { background: #4b8fe2; border-radius: 3px; }
            QPlainTextEdit { background: #111827; color: #e5e7eb; border-radius: 5px;
                             border: 1px solid #273449; font-family: Consolas, monospace;
                             selection-background-color: #31588f; selection-color: #ffffff; }
            QToolTip { color: #172033; background: #fffbea; border: 1px solid #aeb9c9;
                       padding: 4px; }
            """
        )

    def _discover_toolchain(self) -> None:
        try:
            self.toolchain = discover_toolchain()
            self.toolchain_error = None
        except ToolchainError as exc:
            self.toolchain = None
            self.toolchain_error = str(exc)
        self._show_toolchain_status()

    def _show_toolchain_status(self) -> None:
        if self.toolchain:
            self.tool_status.setText(f"FFmpeg {self.toolchain.version.raw} ready")
            self.tool_status.setStyleSheet("color: #147a36; font-weight: 600;")
            self.tool_status.setToolTip(str(self.toolchain.ffmpeg))
            self.process_button.setEnabled(not self._processing)
        else:
            self.tool_status.setText("FFmpeg 9.x not found")
            self.tool_status.setStyleSheet("color: #a63a16; font-weight: 600;")
            self.tool_status.setToolTip(self.toolchain_error or "")
            self.process_button.setEnabled(False)

    def _browse_files(self, side: Literal["source", "destination"]) -> None:
        title = (
            "Add source video or audio files" if side == "source" else "Add destination video files"
        )
        file_filter = (
            "Media files (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.mts *.m2ts *.ts *.mp3 *.m4a *.wav *.flac *.aac *.ac3 *.eac3 *.ogg *.opus *.wma);;All files (*.*)"
            if side == "source"
            else "Video files (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.mts *.m2ts *.ts *.wmv *.flv);;All files (*.*)"
        )
        names, _ = QFileDialog.getOpenFileNames(self, title, "", file_filter)
        if names:
            self._add_paths(side, [Path(name) for name in names])

    def _browse_folder(self, side: Literal["source", "destination"]) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose folder")
        if not selected:
            return
        try:
            paths = collect_media_files(Path(selected), side)
        except MediaProbeError as exc:
            QMessageBox.critical(self, "Could not read folder", str(exc))
            return
        if not paths:
            QMessageBox.information(
                self, "No supported files", "No supported media files were found in that folder."
            )
            return
        self._add_paths(side, paths)

    @Slot(int, object)
    def _files_dropped(self, column: int, raw_paths: object) -> None:
        side: Literal["source", "destination"] = "source" if column == 0 else "destination"
        paths: list[Path] = []
        path_values = raw_paths if isinstance(raw_paths, list) else []
        for path in path_values:
            candidate = Path(path)
            if candidate.is_dir():
                try:
                    paths.extend(collect_media_files(candidate, side))
                except MediaProbeError as exc:
                    self._append_log(str(exc))
            else:
                paths.append(candidate)
        self._add_paths(side, paths)

    def _add_paths(self, side: Literal["source", "destination"], paths: list[Path]) -> None:
        if not paths:
            return
        before_ids = {
            entry.id
            for entry in (self.state.sources if side == "source" else self.state.destinations)
        }
        result = self.state.add_paths(side, paths)
        entries = self.state.sources if side == "source" else self.state.destinations
        added_entries = [entry for entry in entries if entry.id not in before_ids]
        self._row_results.clear()
        if self.toolchain:
            for entry in added_entries:
                task = ProbeTask(side, entry.id, entry.path, self.toolchain.ffprobe)
                task.signals.finished.connect(self._probe_finished)
                self.thread_pool.start(task)
        else:
            message = self.toolchain_error or "FFmpeg 9.x is unavailable."
            for entry in added_entries:
                entry.apply_probe_error(message)
        self.schedule_render()
        notices: list[str] = []
        if result.invalid:
            notices.append(f"{len(result.invalid)} item(s) were not readable files.")
        if result.over_limit:
            notices.append(
                f"Only {MAX_FILES_PER_SIDE} files are allowed per side; "
                f"{len(result.over_limit)} excess file(s) were not added."
            )
        if notices:
            QMessageBox.warning(self, "Some files were not added", "\n".join(notices))

    @Slot(str, str, object, object)
    def _probe_finished(
        self, side: str, entry_id: str, info_object: object, error_object: object
    ) -> None:
        entry: SourceEntry | DestinationEntry | None
        entry = (
            self.state.find_source(entry_id)
            if side == "source"
            else self.state.find_destination(entry_id)
        )
        if entry is None:
            return
        if error_object:
            entry.apply_probe_error(str(error_object))
        else:
            info = info_object
            if not isinstance(info, MediaInfo):
                entry.apply_probe_error("Internal error: media probe returned no result.")
            elif side == "destination" and not info.video_streams:
                entry.apply_probe_error("Destination contains no video stream.")
            else:
                entry.apply_probe(info)
        self.schedule_render()

    def schedule_render(self) -> None:
        if self._render_pending:
            return
        self._render_pending = True
        QTimer.singleShot(35, self.render_table)

    def _file_item(
        self, entry: SourceEntry | DestinationEntry | None, side: str, row: int
    ) -> QTableWidgetItem:
        if entry is None:
            item = QTableWidgetItem("Drop or add a file")
            item.setForeground(QColor("#8a94a5"))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            return item
        status = "Reading tracks…" if entry.probing else "Ready"
        if entry.probe_error:
            status = f"Error: {entry.probe_error}"
        elif entry.info:
            if side == "source":
                status = (
                    f"{len(entry.info.audio_streams)} audio · "
                    f"{len(entry.info.subtitle_streams)} subtitle"
                )
            else:
                status = (
                    f"{len(entry.info.video_streams)} video · "
                    f"{len(entry.info.audio_streams)} audio · "
                    f"{len(entry.info.subtitle_streams)} subtitle"
                )
        result = self._row_results.get(row + 1)
        detailed_status = status
        if result:
            detailed_status = f"{result.status.upper()}: {result.message}"
            if result.status == "success" and result.output_path:
                status = f"Completed · saved {result.output_path.name}"
            elif result.status == "fallback" and result.output_path:
                status = f"Completed as MKV · saved {result.output_path.name}"
            elif result.status == "failed":
                status = "Failed · see the batch log for details"
            elif result.status == "skipped":
                status = "Skipped · see the batch log for details"
            else:
                status = "Cancelled"
        item = QTableWidgetItem(f"{entry.path.name}\n{status}")
        item.setToolTip(
            f"{entry.path}\n\n{detailed_status}\n\n"
            f"Drag this file up or down within the {side.title()} column to change pairing."
        )
        item.setData(Qt.ItemDataRole.UserRole, entry.id)
        item.setForeground(QColor("#172033"))
        if entry.probe_error or (result and result.status == "failed"):
            item.setForeground(QColor("#b42318"))
        elif result and result.status in {"success", "fallback"}:
            item.setForeground(QColor("#147a36"))
        return item

    @staticmethod
    def _pair_key(
        source: SourceEntry | None,
        destination: DestinationEntry | None,
    ) -> tuple[str, str] | None:
        if source is None or destination is None:
            return None
        return source.id, destination.id

    @staticmethod
    def _audio_track_choices(
        source: SourceEntry | None,
        destination: DestinationEntry | None,
    ) -> list[tuple[str, TrackSelection]]:
        choices: list[tuple[str, TrackSelection]] = []
        if source and source.info and source.copy_audio:
            for ordinal, stream in enumerate(source.info.audio_streams, 1):
                choices.append(
                    (
                        f"Source - {stream_display_label(stream, ordinal)}",
                        TrackSelection("source", stream.index),
                    )
                )
        if destination and destination.info and destination.keep_audio:
            for ordinal, stream in enumerate(destination.info.audio_streams, 1):
                choices.append(
                    (
                        f"Destination - {stream_display_label(stream, ordinal)}",
                        TrackSelection("destination", stream.index),
                    )
                )
        return choices

    @staticmethod
    def _subtitle_track_choices(
        source: SourceEntry | None,
        destination: DestinationEntry | None,
    ) -> list[tuple[str, TrackSelection]]:
        choices: list[tuple[str, TrackSelection]] = []
        if source and source.info and source.copy_subtitles:
            for ordinal, stream in enumerate(source.info.subtitle_streams, 1):
                choices.append(
                    (
                        f"Source - {stream_display_label(stream, ordinal)}",
                        TrackSelection("source", stream.index),
                    )
                )
        if destination and destination.info and destination.keep_subtitles:
            for ordinal, stream in enumerate(destination.info.subtitle_streams, 1):
                choices.append(
                    (
                        f"Destination - {stream_display_label(stream, ordinal)}",
                        TrackSelection("destination", stream.index),
                    )
                )
        return choices

    def _effective_audio_selection(
        self,
        source: SourceEntry | None,
        destination: DestinationEntry | None,
        choices: list[tuple[str, TrackSelection]],
    ) -> TrackSelection | None:
        available = {selection for _label, selection in choices}
        key = self._pair_key(source, destination)
        if key is not None:
            saved = self._audio_default_choices.get(key)
            if saved in available:
                return saved
        if source and source.selected_audio_index is not None:
            preferred = TrackSelection("source", source.selected_audio_index)
            if preferred in available:
                return preferred
        if destination and destination.info:
            preferred_stream = next(
                (stream for stream in destination.info.audio_streams if stream.is_default),
                destination.info.audio_streams[0] if destination.info.audio_streams else None,
            )
            if preferred_stream:
                preferred = TrackSelection("destination", preferred_stream.index)
                if preferred in available:
                    return preferred
        return choices[0][1] if choices else None

    def _effective_subtitle_selection(
        self,
        source: SourceEntry | None,
        destination: DestinationEntry | None,
        choices: list[tuple[str, TrackSelection]],
    ) -> TrackSelection | None:
        available = {selection for _label, selection in choices}
        key = self._pair_key(source, destination)
        if key is not None and key in self._subtitle_default_choices:
            saved = self._subtitle_default_choices[key]
            if saved is None or saved in available:
                return saved
        if source and source.selected_subtitle_index is not None:
            preferred = TrackSelection("source", source.selected_subtitle_index)
            if preferred in available:
                return preferred
        return None

    @staticmethod
    def _discard_choice_for_origin(
        choices_by_pair: dict[tuple[str, str], TrackSelection | None],
        source: SourceEntry | None,
        destination: DestinationEntry | None,
        origin: Literal["source", "destination"],
        enabled: bool,
    ) -> None:
        if enabled:
            return
        key = MainWindow._pair_key(source, destination)
        selected = choices_by_pair.get(key) if key is not None else None
        if key is not None and selected is not None and selected.origin == origin:
            choices_by_pair.pop(key, None)

    def _audio_cell(
        self,
        source: SourceEntry | None,
        destination: DestinationEntry | None,
        row: int,
    ) -> QWidget:
        widget = QWidget()
        widget.setObjectName("trackCell")
        widget.setProperty("alternate", row % 2 == 1)
        widget.setProperty("selected", False)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(5, 4, 5, 4)
        layout.setSpacing(3)
        combo = QComboBox()
        combo.setAccessibleName("Default audio track")
        choices = self._audio_track_choices(source, destination)
        if choices:
            for label, selection in choices:
                combo.addItem(label, selection.to_token())
            selected = self._effective_audio_selection(source, destination, choices)
            selected_index = combo.findData(selected.to_token() if selected else "")
            combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        elif (source and source.probing and source.copy_audio) or (
            destination and destination.probing and destination.keep_audio
        ):
            combo.addItem("Reading eligible audio tracks…", None)
        else:
            combo.addItem("No audio tracks selected for output", None)
        layout.addWidget(combo)
        checks = QHBoxLayout()
        copy = VisibleCheckBox("Copy audio tracks")
        copy.setChecked(source.copy_audio if source else True)
        keep = VisibleCheckBox("Keep destination audio tracks")
        keep.setChecked(destination.keep_audio if destination else False)
        checks.addWidget(copy)
        checks.addWidget(keep)
        checks.addStretch(1)
        layout.addLayout(checks)
        copy.setEnabled(source is not None)
        keep.setEnabled(destination is not None)
        combo.setEnabled(bool(choices))

        def audio_selected(_index: int) -> None:
            valid, selected = _decode_track_selection(
                combo.currentData(), allow_no_default=False
            )
            if not valid or selected is None:
                return
            key = self._pair_key(source, destination)
            if key is not None:
                self._audio_default_choices[key] = selected
            if source and selected.origin == "source":
                source.selected_audio_index = selected.stream_index

        def copy_toggled(checked: bool) -> None:
            if source:
                source.copy_audio = checked
                self._discard_choice_for_origin(
                    self._audio_default_choices, source, destination, "source", checked
                )
                self.schedule_render()

        def keep_toggled(checked: bool) -> None:
            if destination:
                destination.keep_audio = checked
                self._discard_choice_for_origin(
                    self._audio_default_choices, source, destination, "destination", checked
                )
                self.schedule_render()

        combo.currentIndexChanged.connect(audio_selected)
        combo.currentTextChanged.connect(combo.setToolTip)
        combo.setToolTip(combo.currentText())
        copy.toggled.connect(copy_toggled)
        keep.toggled.connect(keep_toggled)
        return widget

    def _subtitle_cell(
        self,
        source: SourceEntry | None,
        destination: DestinationEntry | None,
        row: int,
    ) -> QWidget:
        widget = QWidget()
        widget.setObjectName("trackCell")
        widget.setProperty("alternate", row % 2 == 1)
        widget.setProperty("selected", False)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(5, 4, 5, 4)
        layout.setSpacing(3)
        combo = QComboBox()
        combo.setAccessibleName("Default subtitle track")
        choices = self._subtitle_track_choices(source, destination)
        combo.addItem("No default subtitle", NO_DEFAULT_SUBTITLE_TOKEN)
        for label, selection in choices:
            combo.addItem(label, selection.to_token())
        selected = self._effective_subtitle_selection(source, destination, choices)
        selected_token = selected.to_token() if selected else NO_DEFAULT_SUBTITLE_TOKEN
        selected_index = combo.findData(selected_token)
        combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        layout.addWidget(combo)
        checks = QHBoxLayout()
        copy = VisibleCheckBox("Copy subtitle tracks")
        copy.setChecked(source.copy_subtitles if source else True)
        keep = VisibleCheckBox("Keep destination subtitle tracks")
        keep.setChecked(destination.keep_subtitles if destination else False)
        checks.addWidget(copy)
        checks.addWidget(keep)
        checks.addStretch(1)
        layout.addLayout(checks)
        copy.setEnabled(source is not None)
        keep.setEnabled(destination is not None)
        combo.setEnabled(bool(choices))

        def subtitle_selected(_index: int) -> None:
            valid, selected = _decode_track_selection(
                combo.currentData(), allow_no_default=True
            )
            if not valid:
                return
            key = self._pair_key(source, destination)
            if key is not None:
                self._subtitle_default_choices[key] = selected
            if source and selected is not None and selected.origin == "source":
                source.selected_subtitle_index = selected.stream_index

        def copy_toggled(checked: bool) -> None:
            if source:
                source.copy_subtitles = checked
                self._discard_choice_for_origin(
                    self._subtitle_default_choices, source, destination, "source", checked
                )
                self.schedule_render()

        def keep_toggled(checked: bool) -> None:
            if destination:
                destination.keep_subtitles = checked
                self._discard_choice_for_origin(
                    self._subtitle_default_choices,
                    source,
                    destination,
                    "destination",
                    checked,
                )
                self.schedule_render()

        combo.currentIndexChanged.connect(subtitle_selected)
        combo.currentTextChanged.connect(combo.setToolTip)
        combo.setToolTip(combo.currentText())
        copy.toggled.connect(copy_toggled)
        keep.toggled.connect(keep_toggled)
        return widget

    @Slot()
    def render_table(self) -> None:
        self._render_pending = False
        scroll_value = self.table.verticalScrollBar().value()
        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(self.state.row_count)
        for row in range(self.state.row_count):
            source = self.state.sources[row] if row < len(self.state.sources) else None
            destination = (
                self.state.destinations[row] if row < len(self.state.destinations) else None
            )
            self.table.setItem(row, 0, self._file_item(source, "source", row))
            self.table.setItem(row, 1, self._file_item(destination, "destination", row))
            self.table.setCellWidget(row, 2, self._audio_cell(source, destination, row))
            self.table.setCellWidget(row, 3, self._subtitle_cell(source, destination, row))
        self.table.setUpdatesEnabled(True)
        self.table.verticalScrollBar().setValue(scroll_value)
        self._sync_row_selection_styles()
        self._update_open_output_folder_button()
        self.table.viewport().update()

    def _table_selection_changed(self) -> None:
        self._sync_row_selection_styles()
        self._update_open_output_folder_button()

    def _sync_row_selection_styles(self) -> None:
        selection_model = self.table.selectionModel()
        selected_rows = (
            {index.row() for index in selection_model.selectedRows()}
            if selection_model is not None
            else set()
        )
        for row in range(self.table.rowCount()):
            for column in (2, 3):
                widget = self.table.cellWidget(row, column)
                if widget is None:
                    continue
                selected = row in selected_rows
                if widget.property("selected") == selected:
                    continue
                widget.setProperty("selected", selected)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()

    def _header_clicked(self, column: int) -> None:
        if self._processing or column not in (0, 1):
            return
        self._row_results.clear()
        if column == 0:
            self._source_sort_ascending = (
                not self._source_sort_ascending if self._last_sort_column == 0 else True
            )
            self.state.sort_sources(self._source_sort_ascending)
            ascending = self._source_sort_ascending
        else:
            self._destination_sort_ascending = (
                not self._destination_sort_ascending if self._last_sort_column == 1 else True
            )
            self.state.sort_destinations(self._destination_sort_ascending)
            ascending = self._destination_sort_ascending
        self._last_sort_column = column
        order = Qt.SortOrder.AscendingOrder if ascending else Qt.SortOrder.DescendingOrder
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSortIndicator(column, order)
        self.render_table()

    @Slot(int, str, int)
    def _reorder_entry(self, column: int, entry_id: str, insertion_index: int) -> None:
        if self._processing or column not in (0, 1):
            return
        side: Literal["source", "destination"] = "source" if column == 0 else "destination"
        if side == "source":
            old_row = next(
                (row for row, entry in enumerate(self.state.sources) if entry.id == entry_id),
                None,
            )
        else:
            old_row = next(
                (row for row, entry in enumerate(self.state.destinations) if entry.id == entry_id),
                None,
            )
        new_row = self.state.move_entry(side, entry_id, insertion_index)
        if new_row is None or new_row == old_row:
            return
        self._row_results.clear()
        self._last_sort_column = None
        self.table.horizontalHeader().setSortIndicatorShown(False)
        self.render_table()
        self.table.setCurrentCell(new_row, column)
        self.table.selectRow(new_row)
        self._append_log(f"Manual order updated: {side.title()} file moved to row {new_row + 1}.")

    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _remove_selected(self, side: Literal["source", "destination"]) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        self.state.remove_rows(side, rows)
        self._row_results.clear()
        self.render_table()

    def _clear_all(self) -> None:
        self.state.clear()
        self._audio_default_choices.clear()
        self._subtitle_default_choices.clear()
        self._row_results.clear()
        self.log_view.clear()
        self.render_table()

    def _destination_folder_mode_changed(self, checked: bool) -> None:
        self.output_folder_button.setEnabled(not checked and not self._processing)
        self.output_folder_edit.setEnabled(not checked)
        if checked:
            self._output_directory = None
            self.output_folder_edit.setText("Same folder as each destination")
        elif self._output_directory is None:
            self.output_folder_edit.setText("Choose one output folder")
        self._update_open_output_folder_button()

    def _choose_output_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if selected:
            self._output_directory = Path(selected)
            self.output_folder_edit.setText(str(self._output_directory))
        self._update_open_output_folder_button()

    def _resolved_output_folder(self) -> Path | None:
        if not self.destination_folders_check.isChecked():
            return self._output_directory
        selected_rows = self._selected_rows()
        for row in selected_rows:
            if 0 <= row < len(self.state.destinations):
                return self.state.destinations[row].path.parent
        for row in selected_rows:
            result = self._row_results.get(row + 1)
            if result and result.output_path:
                return result.output_path.parent
        for row_number in sorted(self._row_results, reverse=True):
            result = self._row_results[row_number]
            if result.output_path:
                return result.output_path.parent
        if self.state.destinations:
            return self.state.destinations[0].path.parent
        return None

    def _update_open_output_folder_button(self) -> None:
        folder = self._resolved_output_folder()
        self.open_output_folder_button.setEnabled(folder is not None)
        if folder is None:
            tooltip = "Add a destination file or choose one output folder first."
        elif self.destination_folders_check.isChecked():
            tooltip = f"Open {folder}\n\nSelect a different destination row to open its folder."
        else:
            tooltip = f"Open {folder}"
        self.open_output_folder_button.setToolTip(tooltip)

    def _open_output_folder(self) -> None:
        folder = self._resolved_output_folder()
        if folder is None:
            QMessageBox.information(
                self,
                "No output folder yet",
                "Add a destination file or choose one output folder first.",
            )
            return
        if not folder.is_dir():
            QMessageBox.warning(
                self,
                "Output folder is unavailable",
                f"Windows cannot find this output folder:\n\n{folder}",
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(
                self,
                "Could not open output folder",
                f"Windows could not open this folder:\n\n{folder}",
            )
            return
        self._append_log(f"Opened output folder: {folder}")

    def _build_jobs(self) -> tuple[list[PreparedJob], list[str]]:
        self._sync_visible_track_selections()
        jobs: list[PreparedJob] = []
        skipped: list[str] = []
        suffix = self.suffix_edit.text()
        output_directory = (
            None if self.destination_folders_check.isChecked() else self._output_directory
        )
        if not self.destination_folders_check.isChecked() and output_directory is None:
            raise PlanError(
                "Choose an output folder or restore 'Save beside each destination file'."
            )
        for row in range(self.state.row_count):
            row_number = row + 1
            source = self.state.sources[row] if row < len(self.state.sources) else None
            destination = (
                self.state.destinations[row] if row < len(self.state.destinations) else None
            )
            if source is None or destination is None:
                skipped.append(f"Row {row_number}: source or destination is missing.")
                continue
            if source.probing or destination.probing:
                skipped.append(f"Row {row_number}: track inspection is still running.")
                continue
            if source.info is None:
                skipped.append(
                    f"Row {row_number}: source error — {source.probe_error or 'unknown error'}"
                )
                continue
            if destination.info is None:
                skipped.append(
                    f"Row {row_number}: destination error — {destination.probe_error or 'unknown error'}"
                )
                continue
            output = plan_output(destination.path, suffix, output_directory)
            job = PreparedJob(
                row_number=row_number,
                source=source.info,
                destination=destination.info,
                selected_audio=self._effective_audio_selection(
                    source,
                    destination,
                    self._audio_track_choices(source, destination),
                ),
                selected_subtitle=self._effective_subtitle_selection(
                    source,
                    destination,
                    self._subtitle_track_choices(source, destination),
                ),
                copy_audio=source.copy_audio,
                copy_subtitles=source.copy_subtitles,
                keep_destination_audio=destination.keep_audio,
                keep_destination_subtitles=destination.keep_subtitles,
                output=output,
            )
            if self.reliable_defaults_check.isChecked():
                job = apply_reliable_default_container(job)
            jobs.append(job)
        self._check_cross_job_conflicts(jobs)
        if self.fresh_output_paths_check.isChecked():
            jobs = assign_fresh_output_paths(jobs, uuid.uuid4().hex.upper())
            self._check_cross_job_conflicts(jobs)
        return jobs, skipped

    def _sync_visible_track_selections(self) -> None:
        """Make each displayed combo authoritative immediately before planning."""
        for row in range(self.state.row_count):
            source = self.state.sources[row] if row < len(self.state.sources) else None
            destination = (
                self.state.destinations[row] if row < len(self.state.destinations) else None
            )
            if (
                source is None
                or destination is None
                or source.probing
                or destination.probing
                or source.info is None
                or destination.info is None
            ):
                continue
            key = self._pair_key(source, destination)
            assert key is not None

            audio_choices = self._audio_track_choices(source, destination)
            if audio_choices:
                audio_widget = self.table.cellWidget(row, 2)
                audio_combo = audio_widget.findChild(QComboBox) if audio_widget else None
                valid, selected_audio = _decode_track_selection(
                    audio_combo.currentData() if audio_combo else None,
                    allow_no_default=False,
                )
                available_audio = {selection for _label, selection in audio_choices}
                if not valid or selected_audio not in available_audio:
                    raise PlanError(
                        f"Row {row + 1}: the displayed default audio choice could not be read. "
                        "Re-select the audio track and try again."
                    )
                assert selected_audio is not None
                self._audio_default_choices[key] = selected_audio
                if selected_audio.origin == "source":
                    source.selected_audio_index = selected_audio.stream_index

            subtitle_widget = self.table.cellWidget(row, 3)
            subtitle_combo = subtitle_widget.findChild(QComboBox) if subtitle_widget else None
            valid, selected_subtitle = _decode_track_selection(
                subtitle_combo.currentData() if subtitle_combo else None,
                allow_no_default=True,
            )
            available_subtitles = {
                selection
                for _label, selection in self._subtitle_track_choices(source, destination)
            }
            if not valid or (
                selected_subtitle is not None and selected_subtitle not in available_subtitles
            ):
                raise PlanError(
                    f"Row {row + 1}: the displayed default subtitle choice could not be read. "
                    "Re-select the subtitle choice and try again."
                )
            self._subtitle_default_choices[key] = selected_subtitle
            if selected_subtitle is not None and selected_subtitle.origin == "source":
                source.selected_subtitle_index = selected_subtitle.stream_index

    @staticmethod
    def _selection_description(
        job: PreparedJob,
        selection: TrackSelection | None,
        codec_type: Literal["audio", "subtitle"],
    ) -> str:
        if selection is None:
            return "No default subtitle" if codec_type == "subtitle" else "No output audio track"
        info = job.source if selection.origin == "source" else job.destination
        stream = info.stream_by_index(selection.stream_index)
        if stream is None:
            return f"{selection.origin.title()} stream #{selection.stream_index}"
        streams = info.audio_streams if codec_type == "audio" else info.subtitle_streams
        ordinal = next(
            (index for index, candidate in enumerate(streams, 1) if candidate.index == stream.index),
            0,
        )
        return f"{selection.origin.title()} - {stream_display_label(stream, ordinal)}"

    def _check_cross_job_conflicts(self, jobs: list[PreparedJob]) -> None:
        output_owners: dict[str, list[str]] = {}
        input_owners: dict[str, list[str]] = {}
        for job in jobs:
            input_owners.setdefault(normalized_path(job.source.path), []).append(
                f"row {job.row_number} source"
            )
            input_owners.setdefault(normalized_path(job.destination.path), []).append(
                f"row {job.row_number} destination"
            )
            output_owners.setdefault(normalized_path(job.output.primary), []).append(
                f"row {job.row_number} primary"
            )
            if job.output.fallback:
                output_owners.setdefault(normalized_path(job.output.fallback), []).append(
                    f"row {job.row_number} fallback"
                )
        conflicts: list[str] = []
        for path, owners in output_owners.items():
            if len(owners) > 1:
                conflicts.append(f"{path}: {', '.join(owners)}")
        for job in jobs:
            for label, output_path in (
                ("primary", job.output.primary),
                ("fallback", job.output.fallback),
            ):
                if output_path is None:
                    continue
                path = normalized_path(output_path)
                allowed_self = (
                    label == "primary"
                    and job.output.overwrites_destination
                    and input_owners.get(path) == [f"row {job.row_number} destination"]
                )
                if path in input_owners and not allowed_self:
                    conflicts.append(
                        f"{output_path}: row {job.row_number} {label} would replace "
                        f"{', '.join(input_owners[path])}"
                    )
        if conflicts:
            preview = "\n".join(f"• {conflict}" for conflict in conflicts[:12])
            raise PlanError(
                "Output paths conflict with other batch outputs or inputs. Change the suffix, "
                f"output folder, or pairing before processing.\n\n{preview}"
            )

    def _confirm_collisions(self, jobs: list[PreparedJob]) -> tuple[list[PreparedJob], list[str]]:
        approved: list[PreparedJob] = []
        skipped: list[str] = []
        apply_all_choice: bool | None = None
        for job in jobs:
            primary_replace = False
            fallback_replace = False
            skip_job = False
            paths = [(False, job.output.primary, job.output.overwrites_destination)]
            if job.output.fallback is not None:
                paths.append((True, job.output.fallback, False))
            for is_fallback, path, replaces_destination in paths:
                if not path.exists():
                    continue
                if apply_all_choice is None:
                    dialog = OverwriteDialog(
                        self,
                        row_number=job.row_number,
                        path=path,
                        replaces_destination=replaces_destination,
                        fallback=is_fallback,
                    )
                    replace_choice = dialog.exec() == QDialog.DialogCode.Accepted
                    if dialog.apply_all.isChecked():
                        apply_all_choice = replace_choice
                else:
                    replace_choice = apply_all_choice
                if is_fallback:
                    fallback_replace = replace_choice
                elif replace_choice:
                    primary_replace = True
                else:
                    skip_job = True
                    skipped.append(f"Row {job.row_number}: replacement was declined for {path}")
                    break
            if not skip_job:
                approved.append(
                    dataclasses.replace(
                        job,
                        replace_primary=primary_replace,
                        replace_fallback=fallback_replace,
                    )
                )
        return approved, skipped

    def _prepare_and_start(self) -> None:
        if self._processing:
            return
        if not self.toolchain:
            QMessageBox.critical(
                self, "FFmpeg 9.x is required", self.toolchain_error or "Not found"
            )
            return
        try:
            jobs, skipped = self._build_jobs()
        except PlanError as exc:
            QMessageBox.warning(self, "Cannot start batch", str(exc))
            return
        if not jobs:
            details = "\n".join(skipped[:20]) or "Add at least one valid source/destination pair."
            QMessageBox.information(self, "No valid pairs", details)
            return
        jobs, collision_skips = self._confirm_collisions(jobs)
        skipped.extend(collision_skips)
        if not jobs:
            self._append_log("No rows were approved for processing.")
            return
        self.log_view.clear()
        self._open_log()
        self._append_log(
            f"Starting {len(jobs)} row(s) with FFmpeg {self.toolchain.version.raw}. "
            "All media streams use direct copy; no re-encoding is permitted."
        )
        for job in jobs:
            if job.output.freshened:
                self._append_log(
                    f"Row {job.row_number}: an older output was kept; using fresh pathname "
                    f"{job.output.primary.name}."
                )
            self._append_log(
                f"Row {job.row_number}: captured default audio: "
                f"{self._selection_description(job, job.selected_audio, 'audio')}"
            )
            self._append_log(
                f"Row {job.row_number}: captured default subtitle: "
                f"{self._selection_description(job, job.selected_subtitle, 'subtitle')}"
            )
        for message in skipped:
            self._append_log(f"SKIP: {message}")
        self._start_worker(jobs)

    def _open_log(self) -> None:
        base = Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        )
        log_directory = base / "logs"
        try:
            log_directory.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = log_directory / f"batch_{stamp}.log"
            self._log_handle = path.open("a", encoding="utf-8")
            self._append_log(f"Log: {path}")
        except OSError as exc:
            self._log_handle = None
            self.log_view.appendPlainText(f"Could not open persistent log: {exc}")

    def _start_worker(self, jobs: list[PreparedJob]) -> None:
        assert self.toolchain is not None
        self._processing = True
        self._jobs = jobs
        self._job_positions = {job.row_number: index for index, job in enumerate(jobs)}
        self._row_progress_values = {job.row_number: 0.0 for job in jobs}
        self._row_results.clear()
        self._set_controls_for_processing(True)
        self.progress.setValue(0)
        self.progress.setFormat(f"0 / {len(jobs)} rows")
        self._processor = BatchProcessor(self.toolchain)
        thread = QThread(self)
        worker = ProcessingWorker(self._processor, jobs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.row_started.connect(self._on_row_started)
        worker.row_progress.connect(self._on_row_progress)
        worker.row_finished.connect(self._on_row_finished)
        worker.log.connect(self._append_log)
        worker.finished.connect(self._on_batch_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._process_thread = thread
        self._process_worker = worker
        thread.start()

    def _set_controls_for_processing(self, processing: bool) -> None:
        self.input_controls.setEnabled(not processing)
        self.table.setEnabled(not processing)
        self.suffix_edit.setEnabled(not processing)
        self.destination_folders_check.setEnabled(not processing)
        self.fresh_output_paths_check.setEnabled(not processing)
        self.reliable_defaults_check.setEnabled(not processing)
        self.output_folder_edit.setEnabled(
            not processing and not self.destination_folders_check.isChecked()
        )
        self.output_folder_button.setEnabled(
            not processing and not self.destination_folders_check.isChecked()
        )
        self._update_open_output_folder_button()
        self.process_button.setEnabled(not processing and self.toolchain is not None)
        self.cancel_button.setEnabled(processing)

    @Slot(int)
    def _on_row_started(self, row_number: int) -> None:
        self._append_log(f"Row {row_number}: processing started.")

    @Slot(int, float)
    def _on_row_progress(self, row_number: int, value: float) -> None:
        self._row_progress_values[row_number] = max(0.0, min(1.0, value))
        total = sum(self._row_progress_values.values()) / max(1, len(self._jobs))
        completed = sum(1 for result in self._row_results.values() if result.status != "cancelled")
        self.progress.setValue(round(total * 1000))
        self.progress.setFormat(f"{completed} / {len(self._jobs)} rows · {total:.0%}")

    @Slot(object)
    def _on_row_finished(self, result_object: object) -> None:
        if not isinstance(result_object, JobResult):
            return
        result = result_object
        self._row_results[result.row_number] = result
        if result.status in {"success", "fallback"}:
            self._row_progress_values[result.row_number] = 1.0
        self._append_log(f"Row {result.row_number}: {result.status.upper()} — {result.message}")
        if result.recovery_path:
            self._append_log(
                f"Row {result.row_number}: completed recovery file: {result.recovery_path}"
            )
        self.schedule_render()

    @Slot(object)
    def _on_batch_finished(self, results_object: object) -> None:
        results = results_object if isinstance(results_object, list) else []
        self._processing = False
        self._set_controls_for_processing(False)
        success_count = sum(result.status in {"success", "fallback"} for result in results)
        failed_count = sum(result.status == "failed" for result in results)
        skipped_count = sum(result.status in {"skipped", "cancelled"} for result in results)
        self.progress.setValue(1000 if success_count + failed_count + skipped_count else 0)
        self.progress.setFormat(
            f"Finished: {success_count} saved, {failed_count} failed, {skipped_count} skipped/cancelled"
        )
        self._append_log(self.progress.format())
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        self._processor = None
        self._process_worker = None
        self._process_thread = None
        if self._pending_close:
            self._pending_close = False
            self.close()
            return
        if failed_count:
            QMessageBox.warning(
                self,
                "Batch finished with errors",
                self.progress.format() + "\n\nSee the log for exact FFmpeg diagnostics.",
            )
        else:
            QMessageBox.information(self, "Batch finished", self.progress.format())

    def _cancel_processing(self) -> None:
        if self._processor:
            self.cancel_button.setEnabled(False)
            self.progress.setFormat("Cancelling safely…")
            self._append_log(
                "Cancellation requested. The current FFmpeg process will be stopped and its temporary file removed."
            )
            self._processor.cancel()

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_view.appendPlainText(line)
        if self._log_handle:
            try:
                self._log_handle.write(line + "\n")
                self._log_handle.flush()
            except OSError:
                pass

    def _show_about(self) -> None:
        tool_text = (
            f"FFmpeg {self.toolchain.version.raw}\n{self.toolchain.ffmpeg}"
            if self.toolchain
            else self.toolchain_error or "FFmpeg 9.x not found"
        )
        QMessageBox.about(
            self,
            APP_NAME,
            f"<h3>{APP_NAME} {APP_VERSION}</h3>"
            "<p>Copies audio and subtitle streams into destination videos without "
            "re-encoding. The optional MPC-HC/LAV compatibility setting can use Matroska "
            "(MKV) for affected duplicate-language audio. Otherwise the destination "
            "container is preserved unless it rejects a stream combination, in which case "
            "the app retries once as MKV.</p>"
            f"<p><b>Media engine:</b><br>{tool_text}</p>"
            "<p>FFmpeg is separate GPL-licensed software. See THIRD_PARTY_NOTICES.txt "
            "in the portable package.</p>",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._processing:
            self._pending_close = True
            self._cancel_processing()
            event.ignore()
            return
        self.thread_pool.clear()
        self.thread_pool.waitForDone(3000)
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        event.accept()


def active_window() -> MainWindow | None:
    widget = QApplication.activeWindow()
    return widget if isinstance(widget, MainWindow) else None
