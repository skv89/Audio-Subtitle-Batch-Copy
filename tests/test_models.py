from __future__ import annotations

from pathlib import Path

import pytest

from audio_subtitle_batch_copy.constants import MAX_FILES_PER_SIDE
from audio_subtitle_batch_copy.models import BatchState, SourceEntry, TrackSelection

from .helpers import media_info, stream, touch_media


@pytest.mark.parametrize(
    ("selection", "token"),
    [
        (TrackSelection("source", 0), "source:0"),
        (TrackSelection("source", 125), "source:125"),
        (TrackSelection("destination", 7), "destination:7"),
    ],
)
def test_track_selection_scalar_token_round_trip(
    selection: TrackSelection, token: str
) -> None:
    assert selection.to_token() == token
    assert TrackSelection.from_token(token) == selection


@pytest.mark.parametrize("token", ["", "source", "other:1", "source:x", "source:-1"])
def test_invalid_track_selection_tokens_fail_closed(token: str) -> None:
    with pytest.raises(ValueError, match="Invalid track-selection token"):
        TrackSelection.from_token(token)


def test_source_defaults_use_marked_audio_and_subtitle(tmp_path: Path) -> None:
    path = touch_media(tmp_path / "source.mkv")
    info = media_info(
        path,
        (
            stream(0, "audio", "aac", language="eng"),
            stream(1, "audio", "aac", language="jpn", default=True),
            stream(2, "subtitle", "subrip", language="eng"),
            stream(3, "subtitle", "subrip", language="jpn", default=True),
        ),
    )
    entry = SourceEntry(path)
    entry.apply_probe(info)
    assert entry.selected_audio_index == 1
    assert entry.selected_subtitle_index == 3


def test_source_defaults_first_audio_and_no_subtitle_when_unmarked(tmp_path: Path) -> None:
    path = touch_media(tmp_path / "source.mkv")
    info = media_info(
        path,
        (
            stream(4, "audio", "flac"),
            stream(7, "subtitle", "ass"),
        ),
    )
    entry = SourceEntry(path)
    entry.apply_probe(info)
    assert entry.selected_audio_index == 4
    assert entry.selected_subtitle_index is None


def test_independent_natural_sort_preserves_side_specific_state(tmp_path: Path) -> None:
    source_10 = touch_media(tmp_path / "source10.mkv", b"10")
    source_2 = touch_media(tmp_path / "source2.mkv", b"2")
    destination_b = touch_media(tmp_path / "B.mp4", b"b")
    destination_a = touch_media(tmp_path / "A.mp4", b"a")
    state = BatchState()
    state.add_paths("source", [source_10, source_2])
    state.add_paths("destination", [destination_b, destination_a])
    state.sources[0].copy_audio = False
    state.destinations[0].keep_audio = True

    state.sort_sources(ascending=True)
    assert [entry.path.name for entry in state.sources] == ["source2.mkv", "source10.mkv"]
    assert state.sources[1].copy_audio is False
    assert [entry.path.name for entry in state.destinations] == ["B.mp4", "A.mp4"]

    state.sort_destinations(ascending=True)
    assert [entry.path.name for entry in state.destinations] == ["A.mp4", "B.mp4"]
    assert state.destinations[1].keep_audio is True
    assert [entry.path.name for entry in state.sources] == ["source2.mkv", "source10.mkv"]


def test_file_limit_is_exact_and_excess_is_reported(tmp_path: Path) -> None:
    paths = [
        touch_media(tmp_path / f"source-{index}.mkv", str(index).encode()) for index in range(121)
    ]
    state = BatchState()
    result = state.add_paths("source", paths)
    assert result.added == MAX_FILES_PER_SIDE
    assert len(state.sources) == MAX_FILES_PER_SIDE
    assert result.over_limit == (paths[-1],)


def test_manual_moves_preserve_side_specific_state_and_other_side_order(tmp_path: Path) -> None:
    state = BatchState()
    source_paths = [touch_media(tmp_path / f"source-{index}.mkv") for index in range(3)]
    destination_paths = [touch_media(tmp_path / f"destination-{index}.mp4") for index in range(3)]
    state.add_paths("source", source_paths)
    state.add_paths("destination", destination_paths)
    moved_source = state.sources[0]
    moved_source.copy_audio = False
    moved_source.copy_subtitles = False
    moved_source.selected_audio_index = 7
    moved_destination = state.destinations[2]
    moved_destination.keep_audio = True
    moved_destination.keep_subtitles = True
    original_destinations = list(state.destinations)

    assert state.move_entry("source", moved_source.id, 3) == 2
    assert state.sources[2] is moved_source
    assert not state.sources[2].copy_audio
    assert not state.sources[2].copy_subtitles
    assert state.sources[2].selected_audio_index == 7
    assert state.destinations == original_destinations

    original_sources = list(state.sources)
    assert state.move_entry("destination", moved_destination.id, 0) == 0
    assert state.destinations[0] is moved_destination
    assert state.destinations[0].keep_audio
    assert state.destinations[0].keep_subtitles
    assert state.sources == original_sources


def test_manual_move_handles_noop_bounds_and_missing_id(tmp_path: Path) -> None:
    state = BatchState()
    paths = [touch_media(tmp_path / f"source-{index}.mkv") for index in range(3)]
    state.add_paths("source", paths)
    middle_id = state.sources[1].id
    original = list(state.sources)

    assert state.move_entry("source", middle_id, 2) == 1
    assert state.sources == original
    assert state.move_entry("source", "missing-entry", 0) is None
    assert state.sources == original

    last_id = state.sources[-1].id
    assert state.move_entry("source", last_id, -100) == 0
    assert state.sources[0].id == last_id
    assert state.move_entry("source", last_id, 100) == 2
    assert state.sources[-1].id == last_id
