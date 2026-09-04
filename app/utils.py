"""Shared helpers for audio preprocessing, chunking and merging."""

from typing import Dict, List


def normalize_audio(audio_path: str) -> float:
    """Normalize an audio file in-place to 16kHz mono PCM16 WAV.

    Args:
        audio_path: Path to the audio file to normalize.

    Returns:
        The duration of the normalized audio in seconds.
    """
    from pydub import AudioSegment

    audio = AudioSegment.from_file(audio_path)
    audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
    audio.export(audio_path, format="wav")
    return len(audio) / 1000.0


def build_chunks(
    total_duration: float,
    chunk_duration: float = 30.0,
    overlap: float = 5.0,
) -> List[Dict[str, float]]:
    """Build a sliding-window list of ``(start, end)`` chunk boundaries.

    Consecutive chunks overlap by ``overlap`` seconds so the merge step can
    de-duplicate speech that straddles a chunk boundary.

    Args:
        total_duration: Total audio duration in seconds.
        chunk_duration: Length of a full chunk in seconds.
        overlap: Overlap between consecutive chunks in seconds.

    Returns:
        A list of dicts with ``start`` and ``end`` keys (seconds).
    """
    if total_duration <= chunk_duration:
        return [{"start": 0.0, "end": total_duration}]

    chunks: List[Dict[str, float]] = []
    step = chunk_duration - overlap
    start = 0.0
    while start < total_duration:
        end = min(start + chunk_duration, total_duration)
        chunks.append({"start": start, "end": end})
        # Guard against an infinite loop on the final tiny remainder.
        if end >= total_duration:
            break
        start += step
    return chunks


def merge_segments(
    chunk_results: List[Dict],
    chunk_duration: float = 30.0,
    overlap: float = 5.0,
) -> List[Dict]:
    """Merge per-chunk transcript segments into one de-duplicated transcript.

    Args:
        chunk_results: List of dicts, each with ``chunk_index`` (int) and
            ``segments`` (list). Each segment is a dict with ``start``, ``end``,
            ``text`` and ``confidence`` in *chunk-local* time.
        chunk_duration: Length of a full chunk in seconds.
        overlap: Overlap between consecutive chunks in seconds.

    Returns:
        A list of merged segment dicts in absolute timeline order.
    """
    step = chunk_duration - overlap

    # 1. Offset every segment onto the absolute timeline.
    absolute: List[Dict] = []
    for chunk in chunk_results:
        offset = chunk["chunk_index"] * step
        for seg in chunk["segments"]:
            absolute.append(
                {
                    "start": round(seg["start"] + offset, 3),
                    "end": round(seg["end"] + offset, 3),
                    "text": seg["text"],
                    "confidence": seg.get("confidence", 0.0),
                }
            )

    if not absolute:
        return []

    # 2. Sort by start time; on ties keep the higher-confidence segment first.
    absolute.sort(key=lambda s: (s["start"], -s["confidence"]))

    # 3. De-duplicate overlapping regions by keeping the higher-confidence
    #    segment and trimming boundaries to avoid duplicated audio.
    merged: List[Dict] = []
    for seg in absolute:
        if not merged or seg["start"] >= merged[-1]["end"]:
            merged.append(seg)
            continue

        last = merged[-1]
        if seg["confidence"] > last["confidence"]:
            # New segment wins: trim the previous segment's overlap.
            last["end"] = seg["start"]
            if last["end"] <= last["start"]:
                merged.pop()
            merged.append(seg)
        else:
            # Previous segment wins: trim the new segment's overlap.
            seg["start"] = last["end"]
            if seg["end"] > seg["start"]:
                merged.append(seg)

    merged.sort(key=lambda s: s["start"])
    return merged
