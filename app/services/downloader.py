import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import yt_dlp


logger = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    pass


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def score_candidate(entry: dict[str, Any], expected_duration: int | None, title: str | None = None) -> float:
    dur = entry.get("duration") or 0
    t = (entry.get("title") or "").lower()
    ch = (entry.get("channel") or entry.get("uploader") or "").lower()
    score = 100.0

    # Negative keyword filtering
    bad_keywords = [
        "full movie", "scene", "dialogue", "reaction", "cover", "dance",
        "slowed", "reverb", "8d", "bass boosted", "1 hour", "loop",
        "teaser", "trailer", "shorts", "status"
    ]
    for kw in bad_keywords:
        if kw in t:
            score -= 120.0

    # Positive channel & title indicators
    if ch.endswith("- topic"):
        score += 50.0
    if "official audio" in t or "eclipsa audio" in t or "(audio)" in t or " - audio" in t:
        score += 40.0
    elif "audio" in t:
        score += 25.0
    if "lyric" in t:
        score += 20.0
    if any(label in ch for label in ["t-series", "saregama", "zee music", "universal", "ncs", "sony", "yrf"]):
        score += 25.0

    # Duration matching
    if expected_duration and dur:
        diff = abs(dur - expected_duration)
        if diff <= 4:
            score += 60.0
        elif diff <= 8:
            score += 35.0
        elif diff <= 15:
            score += 15.0
        elif diff > 60:
            score -= 100.0
        elif diff > 30:
            score -= 50.0

    return score


def download_audio(
    query_or_url: str,
    destination: Path,
    filename_stem: str,
    preferred_codec: str = "m4a",
    expected_duration: int | None = None,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    temp_prefix = f".download-{uuid.uuid4().hex}"
    temp_template = destination / f"{temp_prefix}.%(ext)s"

    ffmpeg_path = _find_ffmpeg()
    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(temp_template),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": preferred_codec,
                "preferredquality": "0",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "noplaylist": True,
    }
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

    raw_query = query_or_url.strip()
    is_url = raw_query.startswith("http://") or raw_query.startswith("https://")

    target_url = None
    if is_url:
        target_url = raw_query
    else:
        # Search multiple candidates to find the best match based on duration & quality
        candidate_queries = [
            f"ytsearch10:{raw_query} audio",
            f"ytsearch8:{raw_query}",
        ]
        if "," in raw_query:
            simplified = raw_query.split(",")[0].strip()
            candidate_queries.append(f"ytsearch8:{simplified} audio")

        search_ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "noplaylist": True,
        }

        best_candidate = None
        best_score = -999.0

        for sq in candidate_queries:
            try:
                with yt_dlp.YoutubeDL(search_ydl_opts) as ydl:
                    info = ydl.extract_info(sq, download=False)
                    entries = info.get("entries") or []
                    for e in entries:
                        if not e.get("id"):
                            continue
                        score = score_candidate(e, expected_duration, filename_stem)
                        if score > best_score:
                            best_score = score
                            best_candidate = e
                # If we found a strong match within tolerance, stop searching
                if best_candidate and expected_duration and best_candidate.get("duration"):
                    if abs(best_candidate["duration"] - expected_duration) <= 6:
                        break
            except Exception as err:
                logger.debug("Search query %s failed: %s", sq, err)
                continue

        if best_candidate:
            entry_id = best_candidate.get("id")
            target_url = best_candidate.get("url") or f"https://www.youtube.com/watch?v={entry_id}"
            logger.info(
                "Selected best audio candidate for %r (score: %.1f, dur: %ss, exp: %ss): %s (%s)",
                filename_stem,
                best_score,
                best_candidate.get("duration"),
                expected_duration,
                best_candidate.get("title"),
                target_url,
            )
        else:
            target_url = f"ytsearch1:{raw_query}"

    # Download selected target
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            exit_code = ydl.download([target_url])
            if exit_code != 0:
                raise DownloadError(f"yt-dlp exited with code {exit_code}")
    except Exception as error:
        for partial in destination.glob(f"{temp_prefix}.*"):
            partial.unlink(missing_ok=True)
        raise DownloadError(f"Audio download failed for {query_or_url!r}: {error}") from error

    downloaded_files = list(destination.glob(f"{temp_prefix}.*"))
    if not downloaded_files:
        raise DownloadError(f"No audio file was produced for {query_or_url!r}")

    temp_file = downloaded_files[0]
    final_ext = temp_file.suffix.lower()
    final_path = destination / f"{filename_stem}{final_ext}"
    suffix = 2
    while final_path.exists():
        final_path = destination / f"{filename_stem} ({suffix}){final_ext}"
        suffix += 1

    shutil.move(str(temp_file), final_path)
    return final_path


def extract_thumbnail_url(query_or_url: str) -> str | None:
    target_query = query_or_url.strip()
    if not (target_query.startswith("http://") or target_query.startswith("https://")):
        target_query = f"ytsearch1:{target_query}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_query, download=False)
            if not info:
                return None
            entries = info.get("entries")
            entry = entries[0] if entries else info
            if entry.get("thumbnail"):
                return entry["thumbnail"]
            thumbnails = entry.get("thumbnails") or []
            if thumbnails:
                return thumbnails[-1].get("url")
    except Exception as error:
        logger.debug("Failed to extract thumbnail URL: %s", error)
    return None
