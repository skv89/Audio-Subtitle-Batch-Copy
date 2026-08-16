from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDropEvent, QPalette
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox

from audio_subtitle_batch_copy.app import _install_light_palette, _selection_self_test
from audio_subtitle_batch_copy.constants import MAX_FILES_PER_SIDE
from audio_subtitle_batch_copy.media import FFmpegToolchain, ToolVersion
from audio_subtitle_batch_copy.models import DestinationEntry, SourceEntry, TrackSelection
from audio_subtitle_batch_copy.planner import PlanError, build_ffmpeg_command
from audio_subtitle_batch_copy.processor import JobResult
from audio_subtitle_batch_copy.ui import (
    NO_DEFAULT_SUBTITLE_TOKEN,
    MainWindow,
    OverwriteDialog,
    VisibleCheckBox,
)

from .helpers import media_info, stream, touch_media


def fake_toolchain(tmp_path: Path) -> FFmpegToolchain:
    executable = touch_media(tmp_path / "tool.exe")
    return FFmpegToolchain(executable, executable, ToolVersion("9.0-test", 9, 0, None))


def populated_window(qtbot: object, tmp_path: Path) -> MainWindow:
    window = MainWindow(toolchain=fake_toolchain(tmp_path))
    source_path = touch_media(tmp_path / "source.mkv")
    destination_path = touch_media(tmp_path / "destination.mp4")
    source = SourceEntry(source_path)
    source.apply_probe(
        media_info(
            source_path,
            (
                stream(0, "audio", "aac", title="Shared track", default=True),
                stream(1, "subtitle", "subrip", title="Shared captions"),
            ),
        )
    )
    destination = DestinationEntry(destination_path)
    destination.apply_probe(
        media_info(
            destination_path,
            (
                stream(0, "video", "h264"),
                stream(1, "audio", "aac", title="Shared track", default=True),
                stream(2, "subtitle", "subrip", title="Shared captions"),
            ),
        )
    )
    window.state.sources.append(source)
    window.state.destinations.append(destination)
    window.render_table()
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    return window


def test_main_grid_has_exactly_four_columns_and_one_shared_scroll(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)
    assert window.table.columnCount() == 4
    assert [window.table.horizontalHeaderItem(index).text() for index in range(4)] == [
        "Source (sort heading · drag files)",
        "Destination (sort heading · drag files)",
        "Default audio",
        "Default subtitle",
    ]
    assert window.table.verticalScrollBar() is not None


def test_row_controls_have_required_defaults_and_no_default_subtitle(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)
    audio_widget = window.table.cellWidget(0, 2)
    subtitle_widget = window.table.cellWidget(0, 3)
    audio_checks = {
        check.text(): check.isChecked() for check in audio_widget.findChildren(QCheckBox)
    }
    subtitle_checks = {
        check.text(): check.isChecked() for check in subtitle_widget.findChildren(QCheckBox)
    }
    assert audio_checks == {"Copy audio tracks": True, "Keep destination audio tracks": False}
    assert subtitle_checks == {
        "Copy subtitle tracks": True,
        "Keep destination subtitle tracks": False,
    }
    subtitle_combo = subtitle_widget.findChild(QComboBox)
    assert subtitle_combo.itemText(0) == "No default subtitle"
    assert subtitle_combo.currentIndex() == 0
    assert window.fresh_output_paths_check.isVisibleTo(window)
    assert window.fresh_output_paths_check.isChecked()
    assert window.reliable_defaults_check.isVisibleTo(window)
    assert not window.reliable_defaults_check.isChecked()


def test_existing_suffixed_output_gets_fresh_path_by_default(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)
    existing = tmp_path / "destination_copied_audio.mp4"
    existing.write_bytes(b"keep this prior output")

    jobs, skipped = window._build_jobs()

    assert skipped == []
    assert len(jobs) == 1
    assert jobs[0].output.freshened
    assert jobs[0].output.primary.parent == tmp_path
    assert jobs[0].output.primary.name.startswith("destination_copied_audio~fresh-")
    assert jobs[0].output.primary.suffix == ".mp4"
    assert existing.read_bytes() == b"keep this prior output"

    window.fresh_output_paths_check.setChecked(False)
    [replacement_job], skipped = window._build_jobs()
    assert skipped == []
    assert replacement_job.output.primary == existing
    assert not replacement_job.output.freshened


def test_checkboxes_have_theme_independent_visible_indicators(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)
    window.show()
    QApplication.processEvents()
    audio_widget = window.table.cellWidget(0, 2)
    checks = audio_widget.findChildren(QCheckBox)

    assert checks
    assert all(isinstance(check, VisibleCheckBox) for check in checks)
    assert "QCheckBox::indicator { width: 18px; height: 18px" in window.styleSheet()
    checked = next(check for check in checks if check.text() == "Copy audio tracks")
    unchecked = next(check for check in checks if check.text() == "Keep destination audio tracks")
    checked_colors = {
        checked.grab().toImage().pixelColor(x, y).name()
        for x in range(min(24, checked.width()))
        for y in range(checked.height())
    }
    unchecked_colors = {
        unchecked.grab().toImage().pixelColor(x, y).name()
        for x in range(min(24, unchecked.width()))
        for y in range(unchecked.height())
    }
    assert "#1769d2" in checked_colors
    assert "#ffffff" in checked_colors
    assert "#52647a" in unchecked_colors


def test_keep_destination_tracks_refreshes_origin_labelled_combined_menus(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)

    def combo(column: int) -> QComboBox:
        result = window.table.cellWidget(0, column).findChild(QComboBox)
        assert result is not None
        return result

    def check(column: int, text: str) -> QCheckBox:
        result = next(
            item
            for item in window.table.cellWidget(0, column).findChildren(QCheckBox)
            if item.text() == text
        )
        return result

    assert [combo(2).itemText(index) for index in range(combo(2).count())] == [
        "Source - Audio 1 · und · Shared track · AAC · current default"
    ]
    check(2, "Keep destination audio tracks").setChecked(True)
    qtbot.waitUntil(lambda: combo(2).count() == 2)  # type: ignore[attr-defined]
    audio_labels = [combo(2).itemText(index) for index in range(combo(2).count())]
    assert audio_labels[0].startswith("Source - ")
    assert audio_labels[1].startswith("Destination - ")
    assert all("Shared track" in label for label in audio_labels)
    assert combo(2).itemData(0) == TrackSelection("source", 0).to_token()
    assert combo(2).itemData(1) == TrackSelection("destination", 1).to_token()
    combo(2).setCurrentIndex(1)

    check(3, "Keep destination subtitle tracks").setChecked(True)
    qtbot.waitUntil(lambda: combo(3).count() == 3)  # type: ignore[attr-defined]
    subtitle_labels = [combo(3).itemText(index) for index in range(combo(3).count())]
    assert subtitle_labels[0] == "No default subtitle"
    assert subtitle_labels[1].startswith("Source - ")
    assert subtitle_labels[2].startswith("Destination - ")
    assert all("Shared captions" in label for label in subtitle_labels[1:])
    assert combo(3).itemData(0) == NO_DEFAULT_SUBTITLE_TOKEN
    assert combo(3).itemData(1) == TrackSelection("source", 1).to_token()
    assert combo(3).itemData(2) == TrackSelection("destination", 2).to_token()
    combo(3).setCurrentIndex(2)

    jobs, skipped = window._build_jobs()
    assert skipped == []
    assert len(jobs) == 1
    assert jobs[0].selected_audio == TrackSelection("destination", 1)
    assert jobs[0].selected_subtitle == TrackSelection("destination", 2)

    check(2, "Keep destination audio tracks").setChecked(False)
    qtbot.waitUntil(lambda: combo(2).count() == 1)  # type: ignore[attr-defined]
    assert combo(2).currentData() == TrackSelection("source", 0).to_token()


def test_visible_nondefault_audio_choice_overrides_stale_source_default_at_process_time(
    qtbot: object, tmp_path: Path
) -> None:
    window = MainWindow(toolchain=fake_toolchain(tmp_path))
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    source_path = touch_media(tmp_path / "source.mkv")
    destination_path = touch_media(tmp_path / "destination.mp4")
    source = SourceEntry(source_path)
    source.apply_probe(
        media_info(
            source_path,
            (
                stream(1, "audio", "aac", title="Cantonese"),
                stream(2, "audio", "aac", title="Mandarin", default=True),
                stream(3, "subtitle", "subrip", title="English", default=True),
            ),
        )
    )
    destination = DestinationEntry(destination_path)
    destination.apply_probe(media_info(destination_path, (stream(0, "video", "h264"),)))
    window.state.sources.append(source)
    window.state.destinations.append(destination)
    window.render_table()
    audio_combo = window.table.cellWidget(0, 2).findChild(QComboBox)
    subtitle_combo = window.table.cellWidget(0, 3).findChild(QComboBox)
    assert audio_combo is not None
    assert subtitle_combo is not None
    cantonese = TrackSelection("source", 1)
    mandarin = TrackSelection("source", 2)
    english_subtitle = TrackSelection("source", 3)

    assert audio_combo.currentData() == mandarin.to_token()
    assert "Mandarin" in audio_combo.currentText()
    audio_combo.setCurrentIndex(audio_combo.findData(cantonese.to_token()))
    subtitle_combo.setCurrentIndex(0)
    assert "Cantonese" in audio_combo.currentText()

    # Simulate the deployed 1.2.0 failure: signal-time state remains on the
    # source defaults even though the visible combo says Cantonese/no subtitle.
    pair_key = (source.id, destination.id)
    window._audio_default_choices[pair_key] = mandarin
    window._subtitle_default_choices[pair_key] = english_subtitle
    source.selected_audio_index = mandarin.stream_index
    source.selected_subtitle_index = english_subtitle.stream_index

    jobs, skipped = window._build_jobs()
    assert skipped == []
    assert jobs[0].selected_audio == cantonese
    assert jobs[0].selected_subtitle is None
    assert jobs[0].output.primary.suffix == ".mp4"
    assert jobs[0].output.compatibility_reason is None
    command = " ".join(
        build_ffmpeg_command(tmp_path / "ffmpeg.exe", jobs[0], tmp_path / "output.mp4")
    )
    assert "-disposition:a:0 default" in command
    assert "-disposition:a:1 0" in command

    window.reliable_defaults_check.setChecked(True)
    [compatibility_job], skipped = window._build_jobs()
    assert skipped == []
    assert compatibility_job.output.primary.suffix == ".mkv"
    assert compatibility_job.output.compatibility_reason is not None


def test_unreadable_live_combo_payload_stops_instead_of_using_source_default(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)
    audio_combo = window.table.cellWidget(0, 2).findChild(QComboBox)
    subtitle_combo = window.table.cellWidget(0, 3).findChild(QComboBox)
    assert audio_combo is not None
    assert subtitle_combo is not None

    audio_combo.setItemData(audio_combo.currentIndex(), "malformed")
    with pytest.raises(PlanError, match="displayed default audio choice"):
        window._build_jobs()

    audio_combo.setItemData(0, TrackSelection("source", 0).to_token())
    subtitle_combo.setItemData(0, None)
    with pytest.raises(PlanError, match="displayed default subtitle choice"):
        window._build_jobs()


def test_selection_self_test_exercises_live_readback(qtbot: object, tmp_path: Path) -> None:
    window = MainWindow(toolchain=fake_toolchain(tmp_path))
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    assert _selection_self_test(window)


def test_batch_log_records_captured_live_default_choices(
    qtbot: object, tmp_path: Path, monkeypatch: object
) -> None:
    window = populated_window(qtbot, tmp_path)
    monkeypatch.setattr(window, "_open_log", lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(window, "_start_worker", lambda _jobs: None)  # type: ignore[attr-defined]

    window._prepare_and_start()

    log = window.log_view.toPlainText()
    assert "captured default audio: Source - Audio 1" in log
    assert "Shared track" in log
    assert "captured default subtitle: No default subtitle" in log


def test_table_routes_drops_only_to_first_two_columns(qtbot: object, tmp_path: Path) -> None:
    window = populated_window(qtbot, tmp_path)
    first_x = window.table.columnViewportPosition(0) + 10
    second_x = window.table.columnViewportPosition(1) + 10
    third_x = window.table.columnViewportPosition(2) + 10
    assert window.table.drop_column_for_x(first_x) == 0
    assert window.table.drop_column_for_x(second_x) == 1
    assert window.table.drop_column_for_x(third_x) == -1


def test_render_supports_120_rows(qtbot: object, tmp_path: Path) -> None:
    window = MainWindow(toolchain=fake_toolchain(tmp_path))
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    for index in range(MAX_FILES_PER_SIDE):
        path = touch_media(tmp_path / f"source-{index}.mkv", bytes([index % 256]))
        entry = SourceEntry(path)
        entry.apply_probe(media_info(path, (stream(0, "audio", "aac"),)))
        window.state.sources.append(entry)
    window.render_table()
    assert window.table.rowCount() == MAX_FILES_PER_SIDE


def test_file_headers_sort_each_side_independently(qtbot: object, tmp_path: Path) -> None:
    window = MainWindow(toolchain=fake_toolchain(tmp_path))
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    for source_name in ("source10.mkv", "source2.mkv"):
        path = touch_media(tmp_path / source_name, source_name.encode())
        entry = SourceEntry(path)
        entry.apply_probe(media_info(path, (stream(0, "audio", "aac"),)))
        window.state.sources.append(entry)
    for destination_name in ("B.mp4", "A.mp4"):
        path = touch_media(tmp_path / destination_name, destination_name.encode())
        entry = DestinationEntry(path)
        entry.apply_probe(media_info(path, (stream(0, "video", "h264"),)))
        window.state.destinations.append(entry)

    window._header_clicked(0)
    assert [entry.path.name for entry in window.state.sources] == ["source2.mkv", "source10.mkv"]
    assert [entry.path.name for entry in window.state.destinations] == ["B.mp4", "A.mp4"]
    window._header_clicked(1)
    assert [entry.path.name for entry in window.state.destinations] == ["A.mp4", "B.mp4"]
    assert [entry.path.name for entry in window.state.sources] == ["source2.mkv", "source10.mkv"]


def test_processing_state_prevents_contradictory_edits(qtbot: object, tmp_path: Path) -> None:
    window = populated_window(qtbot, tmp_path)
    window._set_controls_for_processing(True)
    assert not window.table.isEnabled()
    assert not window.suffix_edit.isEnabled()
    assert not window.fresh_output_paths_check.isEnabled()
    assert not window.reliable_defaults_check.isEnabled()
    assert not window.process_button.isEnabled()
    assert window.cancel_button.isEnabled()
    window._set_controls_for_processing(False)
    assert window.table.isEnabled()
    assert window.suffix_edit.isEnabled()
    assert window.fresh_output_paths_check.isEnabled()
    assert window.reliable_defaults_check.isEnabled()
    assert window.process_button.isEnabled()
    assert not window.cancel_button.isEnabled()


def test_output_folder_mode_and_overwrite_dialog_safety_defaults(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)
    window.destination_folders_check.setChecked(False)
    assert window.output_folder_button.isEnabled()
    window.destination_folders_check.setChecked(True)
    assert not window.output_folder_button.isEnabled()
    dialog = OverwriteDialog(
        window,
        row_number=1,
        path=tmp_path / "destination.mp4",
        replaces_destination=True,
        fallback=False,
    )
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    assert dialog.skip_button.isDefault()
    assert not dialog.apply_all.isChecked()
    assert "Apply this choice to all" in dialog.apply_all.text()


def _contrast_ratio(foreground: QColor, background: QColor) -> float:
    def luminance(color: QColor) -> float:
        channels = []
        for value in (color.redF(), color.greenF(), color.blueF()):
            channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_explicit_light_palette_and_selected_track_cells_remain_legible(
    qtbot: object, tmp_path: Path
) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.ColorRole.Window, QColor("#202124"))
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor("#ffffff"))
    dark_palette.setColor(QPalette.ColorRole.Base, QColor("#202124"))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff"))
    application.setPalette(dark_palette)
    _install_light_palette(application)
    palette = application.palette()

    assert (
        _contrast_ratio(
            palette.color(QPalette.ColorRole.WindowText),
            palette.color(QPalette.ColorRole.Window),
        )
        >= 7
    )
    assert (
        _contrast_ratio(
            palette.color(QPalette.ColorRole.Text), palette.color(QPalette.ColorRole.Base)
        )
        >= 7
    )
    assert palette.color(QPalette.ColorRole.Text).name() == "#172033"
    assert palette.color(QPalette.ColorRole.Base).name() == "#ffffff"

    window = populated_window(qtbot, tmp_path)
    window.table.selectRow(0)
    QApplication.processEvents()
    assert window.table.item(0, 0).foreground().color().name() == "#172033"
    assert window.table.cellWidget(0, 2).property("selected") is True
    assert window.table.cellWidget(0, 3).property("selected") is True
    assert 'QWidget#trackCell[selected="true"]' in window.styleSheet()
    assert "QGroupBox::title { color: #172033" in window.styleSheet()


def test_completed_result_is_compact_in_cells_and_detailed_in_tooltip(
    qtbot: object, tmp_path: Path
) -> None:
    window = populated_window(qtbot, tmp_path)
    output = tmp_path / "destination_copied_audio.mp4"
    window._row_results[1] = JobResult(1, "success", output, f"Saved {output}")
    window.render_table()
    visible_text = window.table.item(0, 0).text()
    assert "Completed · saved destination_copied_audio.mp4" in visible_text
    assert str(output) not in visible_text
    assert str(output) in window.table.item(0, 0).toolTip()


def test_manual_reorder_moves_only_requested_side_and_resets_sort_indicator(
    qtbot: object, tmp_path: Path
) -> None:
    window = MainWindow(toolchain=fake_toolchain(tmp_path))
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    for index in range(3):
        source_path = touch_media(tmp_path / f"source-{index}.mkv")
        source = SourceEntry(source_path)
        source.apply_probe(media_info(source_path, (stream(index, "audio", "aac"),)))
        destination_path = touch_media(tmp_path / f"destination-{index}.mp4")
        destination = DestinationEntry(destination_path)
        destination.apply_probe(media_info(destination_path, (stream(index, "video", "h264"),)))
        window.state.sources.append(source)
        window.state.destinations.append(destination)
    window.state.sources[0].copy_audio = False
    window.state.destinations[2].keep_audio = True
    window.render_table()
    destination_order = list(window.state.destinations)
    moved_source = window.state.sources[0]
    window._header_clicked(0)
    assert window.table.horizontalHeader().isSortIndicatorShown()

    window._reorder_entry(0, moved_source.id, 3)
    assert window.state.sources[2] is moved_source
    assert not window.state.sources[2].copy_audio
    assert window.state.destinations == destination_order
    assert not window.table.horizontalHeader().isSortIndicatorShown()

    source_order = list(window.state.sources)
    moved_destination = window.state.destinations[2]
    window._reorder_entry(1, moved_destination.id, 0)
    assert window.state.destinations[0] is moved_destination
    assert window.state.destinations[0].keep_audio
    assert window.state.sources == source_order

    before = list(window.state.sources)
    window._processing = True
    window._reorder_entry(0, before[0].id, 3)
    assert window.state.sources == before


def test_internal_drag_drop_reorders_own_column_and_rejects_cross_column(
    qtbot: object, tmp_path: Path
) -> None:
    window = MainWindow(toolchain=fake_toolchain(tmp_path))
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    for index in range(3):
        path = touch_media(tmp_path / f"source-{index}.mkv")
        entry = SourceEntry(path)
        entry.apply_probe(media_info(path, (stream(0, "audio", "aac"),)))
        window.state.sources.append(entry)
    window.render_table()
    window.show()
    QApplication.processEvents()

    moved_id = window.state.sources[0].id
    mime_data = QMimeData()
    mime_data.setData(window.table.INTERNAL_REORDER_MIME, f"0\n{moved_id}".encode())
    source_x = window.table.columnViewportPosition(0) + 12
    last_row_rect = window.table.visualRect(window.table.model().index(2, 0))
    drop_event = QDropEvent(
        QPointF(source_x, last_row_rect.bottom()),
        Qt.DropAction.MoveAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.table.dropEvent(drop_event)
    assert drop_event.isAccepted()
    assert window.state.sources[-1].id == moved_id

    current_order = list(window.state.sources)
    cross_mime = QMimeData()
    cross_mime.setData(
        window.table.INTERNAL_REORDER_MIME,
        f"0\n{window.state.sources[0].id}".encode(),
    )
    destination_x = window.table.columnViewportPosition(1) + 12
    cross_event = QDropEvent(
        QPointF(destination_x, last_row_rect.center().y()),
        Qt.DropAction.MoveAction,
        cross_mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.table.dropEvent(cross_event)
    assert not cross_event.isAccepted()
    assert window.state.sources == current_order


def test_open_output_folder_resolves_selected_or_custom_folder(
    qtbot: object, tmp_path: Path, monkeypatch: object
) -> None:
    window = populated_window(qtbot, tmp_path)
    second_folder = tmp_path / "second"
    second_folder.mkdir()
    second_path = touch_media(second_folder / "second.mp4")
    second = DestinationEntry(second_path)
    second.apply_probe(media_info(second_path, (stream(0, "video", "h264"),)))
    window.state.destinations.append(second)
    window.render_table()
    window.table.selectRow(1)
    assert window._resolved_output_folder() == second_folder
    assert window.open_output_folder_button.isEnabled()

    custom_folder = tmp_path / "custom-output"
    custom_folder.mkdir()
    window.destination_folders_check.setChecked(False)
    window._output_directory = custom_folder
    window._update_open_output_folder_button()
    assert window._resolved_output_folder() == custom_folder

    opened: list[Path] = []

    def record_open(url: QUrl) -> bool:
        opened.append(Path(url.toLocalFile()))
        return True

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "audio_subtitle_batch_copy.ui.QDesktopServices.openUrl", record_open
    )
    window._open_output_folder()
    assert opened == [custom_folder]
    assert "Opened output folder" in window.log_view.toPlainText()

    empty_window = MainWindow(toolchain=fake_toolchain(tmp_path))
    qtbot.addWidget(empty_window)  # type: ignore[attr-defined]
    assert not empty_window.open_output_folder_button.isEnabled()


def test_open_output_folder_reports_missing_folder_and_shell_failure(
    qtbot: object, tmp_path: Path, monkeypatch: object
) -> None:
    window = populated_window(qtbot, tmp_path)
    warnings: list[tuple[str, str]] = []

    def record_warning(_parent: object, title: str, message: str) -> None:
        warnings.append((title, message))

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "audio_subtitle_batch_copy.ui.QMessageBox.warning", record_warning
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "audio_subtitle_batch_copy.ui.QDesktopServices.openUrl", lambda _url: False
    )
    window._open_output_folder()
    assert warnings[-1][0] == "Could not open output folder"

    missing_folder = tmp_path / "deleted-output-folder"
    window.destination_folders_check.setChecked(False)
    window._output_directory = missing_folder
    window._update_open_output_folder_button()
    assert window.open_output_folder_button.isEnabled()
    window._open_output_folder()
    assert warnings[-1][0] == "Output folder is unavailable"
    assert str(missing_folder) in warnings[-1][1]
