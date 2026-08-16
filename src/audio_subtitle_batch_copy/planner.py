from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from .constants import (
    DISPOSITION_ORDER,
    INVALID_WINDOWS_FILENAME_CHARS,
    WINDOWS_RESERVED_BASENAMES,
)
from .isobmff import is_iso_bmff_path
from .models import MediaInfo, MediaStream, TrackSelection


class PlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OutputPlan:
    primary: Path
    fallback: Path | None
    overwrites_destination: bool
    freshened: bool = False
    compatibility_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedJob:
    row_number: int
    source: MediaInfo
    destination: MediaInfo
    selected_audio: TrackSelection | None
    selected_subtitle: TrackSelection | None
    copy_audio: bool
    copy_subtitles: bool
    keep_destination_audio: bool
    keep_destination_subtitles: bool
    output: OutputPlan
    replace_primary: bool = False
    replace_fallback: bool = False

    def with_fresh_media(self, source: MediaInfo, destination: MediaInfo) -> PreparedJob:
        return replace(self, source=source, destination=destination)


@dataclass(frozen=True, slots=True)
class MappedStream:
    input_index: int
    stream: MediaStream


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def same_path(left: Path, right: Path) -> bool:
    return normalized_path(left) == normalized_path(right)


def validate_suffix(suffix: str) -> str | None:
    if any(character in INVALID_WINDOWS_FILENAME_CHARS for character in suffix):
        bad = "".join(
            sorted(
                {character for character in suffix if character in INVALID_WINDOWS_FILENAME_CHARS}
            )
        )
        return f"The suffix contains invalid Windows filename character(s): {bad}"
    if any(ord(character) < 32 for character in suffix):
        return "The suffix contains a control character that Windows filenames do not allow."
    if suffix.endswith((" ", ".")):
        return "The suffix cannot end with a space or period on Windows."
    return None


def plan_output(destination: Path, suffix: str, output_directory: Path | None) -> OutputPlan:
    suffix_error = validate_suffix(suffix)
    if suffix_error:
        raise PlanError(suffix_error)
    extension = destination.suffix
    if not extension:
        raise PlanError(f"Destination has no container extension: {destination.name}")
    directory = output_directory if output_directory is not None else destination.parent
    output_stem = f"{destination.stem}{suffix}"
    output_name = f"{output_stem}{extension}"
    if not output_stem:
        raise PlanError("The destination name and suffix produce an empty output filename.")
    if output_stem.rstrip(" .").upper() in WINDOWS_RESERVED_BASENAMES:
        raise PlanError(f"The output filename is reserved by Windows: {output_name}")
    windows_code_units = len(output_name.encode("utf-16-le")) // 2
    if windows_code_units > 255:
        raise PlanError(
            f"The output filename uses {windows_code_units} UTF-16 code units; Windows file "
            "components are limited to 255. Shorten the suffix."
        )
    primary = directory / output_name
    fallback = None if extension.casefold() == ".mkv" else primary.with_suffix(".mkv")
    return OutputPlan(
        primary=primary,
        fallback=fallback,
        overwrites_destination=same_path(primary, destination),
    )


def _windows_code_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _path_with_fresh_identity(path: Path, identity: str, counter: int) -> Path:
    marker = f"~fresh-{identity}" if counter == 1 else f"~fresh-{identity}-{counter}"
    available_stem_units = 255 - _windows_code_units(path.suffix) - _windows_code_units(marker)
    stem = path.stem
    while stem and _windows_code_units(stem) > available_stem_units:
        stem = stem[:-1]
    if not stem:
        raise PlanError("The output filename is too long to add a fresh playback identity.")
    return path.with_name(f"{stem}{marker}{path.suffix}")


def _output_paths(plan: OutputPlan) -> tuple[Path, ...]:
    return (plan.primary,) if plan.fallback is None else (plan.primary, plan.fallback)


def assign_fresh_output_paths(
    jobs: list[PreparedJob],
    identity: str,
) -> list[PreparedJob]:
    """Give colliding non-destination outputs a pathname that media players have not cached."""
    if not identity or not identity.isascii() or not identity.isalnum():
        raise PlanError("The internal fresh-output identity is invalid.")

    input_paths = {
        normalized_path(path)
        for job in jobs
        for path in (job.source.path, job.destination.path)
    }
    base_output_paths = {
        normalized_path(path) for job in jobs for path in _output_paths(job.output)
    }
    assigned_paths: set[str] = set()
    resolved: list[PreparedJob] = []

    for job in jobs:
        plan = job.output
        own_paths = {normalized_path(path) for path in _output_paths(plan)}
        blocked = input_paths | (base_output_paths - own_paths) | assigned_paths
        needs_fresh_path = not plan.overwrites_destination and any(
            path.exists() or normalized_path(path) in blocked for path in _output_paths(plan)
        )
        if needs_fresh_path:
            for counter in range(1, 10_001):
                primary = _path_with_fresh_identity(plan.primary, identity, counter)
                fallback = (
                    _path_with_fresh_identity(plan.fallback, identity, counter)
                    if plan.fallback is not None
                    else None
                )
                candidates = (primary,) if fallback is None else (primary, fallback)
                if all(
                    not path.exists() and normalized_path(path) not in blocked
                    for path in candidates
                ):
                    plan = replace(
                        plan,
                        primary=primary,
                        fallback=fallback,
                        overwrites_destination=False,
                        freshened=True,
                    )
                    break
            else:
                raise PlanError("Could not allocate a fresh output filename after 10,000 tries.")

        assigned_paths.update(normalized_path(path) for path in _output_paths(plan))
        resolved.append(replace(job, output=plan))
    return resolved


def validate_job(job: PreparedJob) -> None:
    if not job.destination.video_streams:
        raise PlanError(f"Row {job.row_number}: destination contains no video stream.")
    audio_is_mapped = bool(
        (job.copy_audio and job.source.audio_streams)
        or (job.keep_destination_audio and job.destination.audio_streams)
    )
    if (audio_is_mapped or job.selected_audio is not None) and not _selection_is_mapped(
        job, job.selected_audio, "audio"
    ):
        raise PlanError(f"Row {job.row_number}: select a valid output audio track.")
    if job.selected_subtitle is not None and not _selection_is_mapped(
        job, job.selected_subtitle, "subtitle"
    ):
        raise PlanError(f"Row {job.row_number}: select a valid output subtitle track.")


def _selection_is_mapped(
    job: PreparedJob,
    selection: TrackSelection | None,
    codec_type: str,
) -> bool:
    if selection is None:
        return False
    if selection.origin == "source":
        if codec_type == "audio" and not job.copy_audio:
            return False
        if codec_type == "subtitle" and not job.copy_subtitles:
            return False
        info = job.source
    elif selection.origin == "destination":
        if codec_type == "audio" and not job.keep_destination_audio:
            return False
        if codec_type == "subtitle" and not job.keep_destination_subtitles:
            return False
        info = job.destination
    else:
        return False
    stream = info.stream_by_index(selection.stream_index)
    return bool(stream and stream.codec_type == codec_type)


def _mapped_matches_selection(mapped: MappedStream, selection: TrackSelection | None) -> bool:
    if selection is None:
        return False
    expected_input = 1 if selection.origin == "source" else 0
    return mapped.input_index == expected_input and mapped.stream.index == selection.stream_index


def mapped_streams(job: PreparedJob) -> tuple[MappedStream, ...]:
    validate_job(job)
    base = [
        MappedStream(0, stream)
        for stream in job.destination.streams
        if stream.codec_type not in {"audio", "subtitle"}
    ]
    audio: list[MappedStream] = []
    if job.keep_destination_audio:
        audio.extend(MappedStream(0, stream) for stream in job.destination.audio_streams)
    if job.copy_audio:
        audio.extend(MappedStream(1, stream) for stream in job.source.audio_streams)
    audio = _selected_stream_first(audio, job.selected_audio)
    subtitles: list[MappedStream] = []
    if job.keep_destination_subtitles:
        subtitles.extend(MappedStream(0, stream) for stream in job.destination.subtitle_streams)
    if job.copy_subtitles:
        subtitles.extend(MappedStream(1, stream) for stream in job.source.subtitle_streams)
    subtitles = _selected_stream_first(subtitles, job.selected_subtitle)
    return tuple([*base, *audio, *subtitles])


def apply_reliable_default_container(job: PreparedJob) -> PreparedJob:
    """Use MKV when LAV cannot reliably distinguish a selected ISO-BMFF audio default.

    Some LAV Splitter versions do not expose MP4/MOV ``tkhd`` Enabled audio flags as
    default dispositions. When multiple mapped audio tracks share the selected track's
    language, LAV may choose another track using its quality heuristic. Matroska carries
    a default flag that LAV does expose, so plan the existing direct-copy fallback first.
    """
    plan = job.output
    if (
        plan.compatibility_reason is not None
        or plan.fallback is None
        or not is_iso_bmff_path(plan.primary)
        or job.selected_audio is None
    ):
        return job

    audio = [mapped for mapped in mapped_streams(job) if mapped.stream.codec_type == "audio"]
    selected = next(
        (mapped for mapped in audio if _mapped_matches_selection(mapped, job.selected_audio)),
        None,
    )
    if selected is None:
        return job
    selected_language = selected.stream.language.casefold()
    same_language_count = sum(
        mapped.stream.language.casefold() == selected_language for mapped in audio
    )
    if same_language_count < 2:
        return job

    requested_extension = plan.primary.suffix.upper().lstrip(".") or "ISO-BMFF"
    reason = (
        f"using MKV for reliable default-track playback because {same_language_count} output "
        f"audio tracks share language '{selected.stream.language}' and MPC-HC/LAV may ignore "
        f"the selected audio default in {requested_extension}"
    )
    mkv_path = plan.fallback
    compatible_plan = replace(
        plan,
        primary=mkv_path,
        fallback=None,
        overwrites_destination=same_path(mkv_path, job.destination.path),
        compatibility_reason=reason,
    )
    return replace(job, output=compatible_plan)


def _selected_stream_first(
    streams: list[MappedStream], selection: TrackSelection | None
) -> list[MappedStream]:
    selected_index = next(
        (
            index
            for index, mapped in enumerate(streams)
            if _mapped_matches_selection(mapped, selection)
        ),
        None,
    )
    if selected_index is None or selected_index == 0:
        return streams
    selected = streams[selected_index]
    return [selected, *streams[:selected_index], *streams[selected_index + 1 :]]


def _disposition_expression(dispositions: set[str]) -> str:
    if not dispositions:
        return "0"
    known = [name for name in DISPOSITION_ORDER if name in dispositions]
    unknown = sorted(dispositions.difference(known))
    return "+".join([*known, *unknown])


def _disposition_arguments(job: PreparedJob, streams: tuple[MappedStream, ...]) -> list[str]:
    arguments: list[str] = []
    audio = [mapped for mapped in streams if mapped.stream.codec_type == "audio"]
    subtitles = [mapped for mapped in streams if mapped.stream.codec_type == "subtitle"]

    enforce_audio_default = bool(audio and job.selected_audio is not None)
    for ordinal, mapped in enumerate(audio):
        dispositions = set(mapped.stream.dispositions)
        if enforce_audio_default:
            dispositions.discard("default")
            if _mapped_matches_selection(mapped, job.selected_audio):
                dispositions.add("default")
        arguments.extend([f"-disposition:a:{ordinal}", _disposition_expression(dispositions)])

    for ordinal, mapped in enumerate(subtitles):
        dispositions = set(mapped.stream.dispositions)
        # "No default subtitle" applies to the entire output, including retained
        # destination tracks. A selected track becomes default only when that
        # exact origin/index identity is present in the output mapping.
        dispositions.discard("default")
        if (
            job.selected_subtitle is not None
            and _mapped_matches_selection(mapped, job.selected_subtitle)
        ):
            dispositions.add("default")
        arguments.extend([f"-disposition:s:{ordinal}", _disposition_expression(dispositions)])
    return arguments


def build_ffmpeg_command(ffmpeg: Path, job: PreparedJob, temporary_output: Path) -> list[str]:
    streams = mapped_streams(job)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(job.destination.path),
        "-i",
        str(job.source.path),
    ]
    for mapped in streams:
        command.extend(["-map", f"{mapped.input_index}:{mapped.stream.index}"])
    command.extend(
        [
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-copy_unknown",
            "-c",
            "copy",
            *_disposition_arguments(job, streams),
            "-progress",
            "pipe:1",
            "-nostats",
            str(temporary_output),
        ]
    )
    return command


def expected_default_indices(job: PreparedJob) -> tuple[set[int], set[int]]:
    streams = mapped_streams(job)
    audio = [mapped for mapped in streams if mapped.stream.codec_type == "audio"]
    subtitles = [mapped for mapped in streams if mapped.stream.codec_type == "subtitle"]

    if job.selected_audio is not None:
        audio_defaults = {
            ordinal
            for ordinal, mapped in enumerate(audio)
            if _mapped_matches_selection(mapped, job.selected_audio)
        }
    else:
        audio_defaults = {
            ordinal
            for ordinal, mapped in enumerate(audio)
            if "default" in mapped.stream.dispositions
        }

    subtitle_defaults = {
        ordinal
        for ordinal, mapped in enumerate(subtitles)
        if job.selected_subtitle is not None
        and _mapped_matches_selection(mapped, job.selected_subtitle)
    }
    return audio_defaults, subtitle_defaults
