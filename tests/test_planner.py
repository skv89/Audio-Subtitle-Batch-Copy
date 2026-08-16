from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from audio_subtitle_batch_copy.models import TrackSelection
from audio_subtitle_batch_copy.planner import (
    PlanError,
    PreparedJob,
    apply_reliable_default_container,
    assign_fresh_output_paths,
    build_ffmpeg_command,
    expected_default_indices,
    mapped_streams,
    plan_output,
    validate_suffix,
)

from .helpers import media_info, stream, touch_media


def make_job(tmp_path: Path, *, copy_subtitles: bool = True) -> PreparedJob:
    source_path = touch_media(tmp_path / "source.mkv")
    destination_path = touch_media(tmp_path / "destination.mp4")
    source = media_info(
        source_path,
        (
            stream(0, "audio", "aac", language="eng", default=True),
            stream(1, "audio", "aac", language="jpn"),
            stream(2, "subtitle", "subrip", language="eng", default=True),
            stream(3, "subtitle", "ass", language="spa", dispositions=frozenset({"forced"})),
        ),
    )
    destination = media_info(
        destination_path,
        (
            stream(0, "video", "h264"),
            stream(1, "audio", "aac", language="fra", default=True),
            stream(2, "subtitle", "mov_text", language="fra", default=True),
            stream(3, "data", "bin_data"),
        ),
    )
    return PreparedJob(
        row_number=1,
        source=source,
        destination=destination,
        selected_audio=TrackSelection("source", 1),
        selected_subtitle=None,
        copy_audio=True,
        copy_subtitles=copy_subtitles,
        keep_destination_audio=True,
        keep_destination_subtitles=True,
        output=plan_output(destination_path, "_copied_audio", None),
    )


def test_output_plan_preserves_extension_and_uses_mkv_fallback(tmp_path: Path) -> None:
    destination = tmp_path / "Movie.MP4"
    plan = plan_output(destination, "_copied_audio", None)
    assert plan.primary == tmp_path / "Movie_copied_audio.MP4"
    assert plan.fallback == tmp_path / "Movie_copied_audio.mkv"
    assert not plan.overwrites_destination


def duplicate_language_audio_job(tmp_path: Path, extension: str = ".mp4") -> PreparedJob:
    source_path = touch_media(tmp_path / "source-duplicate-language.mkv")
    destination_path = touch_media(tmp_path / f"destination{extension}")
    source = media_info(
        source_path,
        (
            stream(1, "audio", "aac", language="chi", title="Cantonese", channels=1),
            stream(
                2,
                "audio",
                "aac",
                language="chi",
                title="Mandarin",
                default=True,
                channels=2,
            ),
        ),
    )
    destination = media_info(destination_path, (stream(0, "video", "h264"),))
    return PreparedJob(
        row_number=1,
        source=source,
        destination=destination,
        selected_audio=TrackSelection("source", 1),
        selected_subtitle=None,
        copy_audio=True,
        copy_subtitles=False,
        keep_destination_audio=False,
        keep_destination_subtitles=False,
        output=plan_output(destination_path, "_copied_audio", None),
    )


def test_duplicate_language_iso_bmff_audio_uses_reliable_mkv_plan(tmp_path: Path) -> None:
    original = duplicate_language_audio_job(tmp_path)
    compatible = apply_reliable_default_container(original)

    assert original.output.primary.suffix == ".mp4"
    assert compatible.output.primary == tmp_path / "destination_copied_audio.mkv"
    assert compatible.output.fallback is None
    assert not compatible.output.overwrites_destination
    assert compatible.output.compatibility_reason is not None
    assert "2 output audio tracks share language 'chi'" in compatible.output.compatibility_reason
    assert "MPC-HC/LAV" in compatible.output.compatibility_reason


def test_reliable_mkv_plan_is_selective_and_can_be_skipped(tmp_path: Path) -> None:
    duplicate = duplicate_language_audio_job(tmp_path)
    assert duplicate.output.primary.suffix == ".mp4"  # UI opt-out leaves the plan unchanged.

    distinct_source = replace(
        duplicate.source,
        streams=(
            replace(duplicate.source.audio_streams[0], tags={"language": "yue"}),
            replace(duplicate.source.audio_streams[1], tags={"language": "cmn"}),
        ),
    )
    distinct = replace(duplicate, source=distinct_source)
    assert apply_reliable_default_container(distinct).output == distinct.output

    avi = duplicate_language_audio_job(tmp_path, ".avi")
    assert apply_reliable_default_container(avi).output == avi.output


def test_reliable_mkv_plan_receives_fresh_identity_without_reserving_mp4(
    tmp_path: Path,
) -> None:
    compatible = apply_reliable_default_container(duplicate_language_audio_job(tmp_path))
    compatible.output.primary.write_bytes(b"protected MKV")

    [fresh] = assign_fresh_output_paths([compatible], "ABC123")

    assert fresh.output.primary.name == "destination_copied_audio~fresh-ABC123.mkv"
    assert fresh.output.fallback is None
    assert fresh.output.freshened
    assert fresh.output.compatibility_reason == compatible.output.compatibility_reason


def test_blank_suffix_same_folder_is_classified_as_destination_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "Movie.mkv"
    plan = plan_output(destination, "", None)
    assert plan.primary == destination
    assert plan.fallback is None
    assert plan.overwrites_destination


@pytest.mark.parametrize("suffix", ["bad/name", "bad:name", "trailing.", "trailing "])
def test_invalid_windows_suffix_is_rejected(suffix: str) -> None:
    assert validate_suffix(suffix) is not None


def test_windows_control_character_suffix_is_rejected() -> None:
    assert validate_suffix("_bad\nname") is not None


def test_destination_without_extension_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="no container extension"):
        plan_output(tmp_path / "movie", "_x", None)


def test_reserved_or_overlong_windows_output_names_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="reserved by Windows"):
        plan_output(tmp_path / "C.mp4", "ON", None)
    with pytest.raises(PlanError, match="limited to 255"):
        plan_output(tmp_path / "movie.mp4", "x" * 250, None)
    with pytest.raises(PlanError, match="UTF-16 code units"):
        plan_output(tmp_path / "movie.mp4", "😀" * 125, None)


def test_stream_map_keeps_destination_non_av_and_requested_tracks(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    result = [(item.input_index, item.stream.index) for item in mapped_streams(job)]
    assert result == [(0, 0), (0, 3), (1, 1), (0, 1), (1, 0), (0, 2), (1, 2), (1, 3)]


def test_command_is_direct_copy_and_assigns_selected_defaults(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    command = build_ffmpeg_command(Path("ffmpeg.exe"), job, tmp_path / "temporary.mp4")
    joined = " ".join(command)
    assert "-c copy" in joined
    assert "libx264" not in joined
    assert "-map_metadata 0" in joined
    assert "-map_chapters 0" in joined
    assert "-map 1:1" in joined
    assert "-disposition:a:0 default" in joined
    assert "-disposition:a:1 0" in joined
    assert "-disposition:a:2 0" in joined
    assert "-disposition:s:0 0" in joined
    assert "-disposition:s:1 0" in joined
    assert "-disposition:s:2 forced" in joined
    assert expected_default_indices(job) == ({0}, set())


def test_no_default_subtitle_applies_to_retained_destination_tracks(tmp_path: Path) -> None:
    job = make_job(tmp_path, copy_subtitles=False)
    assert expected_default_indices(job) == ({0}, set())


def test_selected_subtitle_is_first_while_no_default_preserves_relative_order(
    tmp_path: Path,
) -> None:
    job = make_job(tmp_path)
    selected = replace(job, selected_subtitle=TrackSelection("source", 3))
    selected_subtitles = [
        (item.input_index, item.stream.index)
        for item in mapped_streams(selected)
        if item.stream.codec_type == "subtitle"
    ]
    assert selected_subtitles == [(1, 3), (0, 2), (1, 2)]
    assert expected_default_indices(selected) == ({0}, {0})

    no_default_subtitles = [
        (item.input_index, item.stream.index)
        for item in mapped_streams(job)
        if item.stream.codec_type == "subtitle"
    ]
    assert no_default_subtitles == [(0, 2), (1, 2), (1, 3)]


def test_destination_tracks_can_be_the_unambiguous_output_defaults(tmp_path: Path) -> None:
    job = replace(
        make_job(tmp_path),
        selected_audio=TrackSelection("destination", 1),
        selected_subtitle=TrackSelection("destination", 2),
    )
    command = build_ffmpeg_command(Path("ffmpeg.exe"), job, tmp_path / "temporary.mkv")
    joined = " ".join(command)

    assert "-disposition:a:0 default" in joined
    assert "-disposition:a:1 0" in joined
    assert "-disposition:a:2 0" in joined
    assert "-disposition:s:0 default" in joined
    assert "-disposition:s:1 0" in joined
    assert "-disposition:s:2 forced" in joined
    assert expected_default_indices(job) == ({0}, {0})


def test_selection_origin_must_be_retained_even_when_track_indices_overlap(
    tmp_path: Path,
) -> None:
    job = make_job(tmp_path)
    destination_choice = TrackSelection("destination", 1)
    assert expected_default_indices(replace(job, selected_audio=destination_choice)) == ({0}, set())

    with pytest.raises(PlanError, match="valid output audio track"):
        mapped_streams(
            replace(
                job,
                selected_audio=destination_choice,
                keep_destination_audio=False,
            )
        )


def test_existing_non_destination_output_receives_fresh_conflict_checked_path(
    tmp_path: Path,
) -> None:
    job = make_job(tmp_path)
    job.output.primary.write_bytes(b"protected old output")
    first_candidate = job.output.primary.with_name(
        f"{job.output.primary.stem}~fresh-ABC123{job.output.primary.suffix}"
    )
    first_candidate.write_bytes(b"protected prior fresh output")

    [resolved] = assign_fresh_output_paths([job], "ABC123")

    assert resolved.output.freshened
    assert resolved.output.primary.name == "destination_copied_audio~fresh-ABC123-2.mp4"
    assert resolved.output.fallback is not None
    assert resolved.output.fallback.name == "destination_copied_audio~fresh-ABC123-2.mkv"
    assert job.output.primary.read_bytes() == b"protected old output"
    assert first_candidate.read_bytes() == b"protected prior fresh output"


def test_fresh_output_mode_never_renames_destination_overwrite(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    overwrite = replace(
        job,
        output=plan_output(job.destination.path, "", None),
    )

    [resolved] = assign_fresh_output_paths([overwrite], "ABC123")

    assert resolved.output.primary == job.destination.path
    assert resolved.output.overwrites_destination
    assert not resolved.output.freshened


def test_fresh_identity_trims_near_limit_name_to_valid_windows_component(
    tmp_path: Path,
) -> None:
    destination = touch_media(tmp_path / f"{'x' * 235}.mp4")
    job = replace(make_job(tmp_path), output=plan_output(destination, "_copy", None))
    job.output.primary.write_bytes(b"existing")

    [resolved] = assign_fresh_output_paths([job], "ABC123")

    assert resolved.output.freshened
    assert len(resolved.output.primary.name.encode("utf-16-le")) // 2 <= 255
    assert resolved.output.primary.name.endswith("~fresh-ABC123.mp4")
