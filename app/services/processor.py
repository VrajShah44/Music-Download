import json
import logging
import os
from pathlib import Path
from typing import Any

from app.services import apple_music, artwork, downloader, lyrics, musicbrainz
from app.services.library import safe_component, track_directory, write_assets
from app.services.tagging import tag_audio


logger = logging.getLogger(__name__)
LIBRARY_DIR = Path(
    os.environ.get(
        "SMC_LIBRARY_DIR", Path.home() / "Music" / "Smart Music Collector"
    )
)


def _library_directory(fields: dict[str, Any]) -> Path:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    title = musicbrainz.normalize(fields.get("song_title"))
    artist = musicbrainz.normalize(fields.get("artist"))
    requested_id = fields.get("musicbrainz_recording_id")
    for metadata_path in LIBRARY_DIR.rglob("metadata.json"):
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        existing_fields = existing.get("fields", {})
        existing_id = existing_fields.get("musicbrainz_recording_id")
        same_recording = (
            existing_id == requested_id
            if existing_id and requested_id
            else musicbrainz.normalize(existing_fields.get("song_title")) == title
            and musicbrainz.normalize(existing_fields.get("artist")) == artist
            and musicbrainz.normalize(existing_fields.get("album_name"))
            == musicbrainz.normalize(fields.get("album_name"))
        )
        if same_recording:
            return metadata_path.parent
    destination = track_directory(LIBRARY_DIR, fields)
    if not (destination / "metadata.json").exists():
        return destination
    suffix = 2
    while True:
        candidate = destination.with_name(f"{destination.name} ({suffix})")
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
        suffix += 1


def process_song(
    request: dict[str, Any],
    selected_recording_id: str | None = None,
    download_audio: bool = True,
    auto_select: bool = False,
) -> dict[str, Any]:
    source_url = request.get("source_url")
    title = (request.get("song_title") or "").strip()
    artist = (request.get("artist") or "").strip()

    is_placeholder_title = not title or title.lower() in {
        "song title",
        "unknown track",
        "unknown",
        "untitled",
    }
    is_placeholder_artist = not artist or artist.lower() in {
        "artist",
        "unknown artist",
        "unknown",
    }

    if source_url and (is_placeholder_title or is_placeholder_artist):
        extracted = downloader.extract_track_info_from_url(source_url)
        if extracted.get("title") and is_placeholder_title:
            request["song_title"] = extracted["title"]
            logger.info("Auto-detected song title from URL: %r", extracted["title"])
        if extracted.get("artist") and is_placeholder_artist:
            request["artist"] = extracted["artist"]
            logger.info("Auto-detected artist from URL: %r", extracted["artist"])
        if extracted.get("duration") and not request.get("duration_ms"):
            request["duration_ms"] = int(extracted["duration"]) * 1000

    identification = musicbrainz.find_recordings(request)
    if selected_recording_id:
        candidate = next(
            (
                item
                for item in identification["candidates"]
                if item.get("recording_id") == selected_recording_id
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Selected recording is not one of the returned candidates")
        identification = {
            "status": "matched",
            "selected": candidate,
            "candidates": identification["candidates"],
        }
    elif identification["status"] == "needs_selection" and auto_select and identification.get("candidates"):
        identification = {
            "status": "matched",
            "selected": identification["candidates"][0],
            "candidates": identification["candidates"],
        }
    if identification["status"] == "not_found":
        if not download_audio:
            return {
                "status": "partially_completed",
                "message": "No MusicBrainz recording candidates were found.",
                "metadata": {"status": "unavailable"},
                "lyrics": {"status": "skipped", "reason": "No recording was identified"},
                "artwork": {"status": "skipped", "reason": "No recording was identified"},
                "audio": {"status": "pending", "reason": "No authorized audio file supplied"},
                "candidates": [],
            }
        # Fallback candidate using user-provided info
        candidate = {
            "recording_id": None,
            "title": request.get("song_title"),
            "artist": request.get("artist"),
            "artist_names": [request.get("artist")] if request.get("artist") else [],
            "album_name": request.get("album_name") or request.get("movie_name"),
            "release_name": request.get("album_name") or request.get("movie_name"),
            "score": 1.0,
        }
        metadata = {
            "fields": {
                "song_title": request.get("song_title"),
                "artist": request.get("artist"),
                "featured_artists": request.get("featured_artists") or [],
                "album_name": request.get("album_name") or request.get("movie_name"),
                "movie_name": request.get("movie_name"),
                "composer": request.get("composer"),
                "language": request.get("language"),
                "musicbrainz_recording_id": None,
            },
            "sources": {"musicbrainz": None, "user": True},
        }
    elif identification["status"] == "needs_selection":
        return {
            "status": "needs_selection",
            "message": "Several recordings are plausible; select the correct candidate before processing.",
            "metadata": {"status": "needs_selection"},
            "lyrics": {"status": "skipped", "reason": "Recording selection required"},
            "artwork": {"status": "skipped", "reason": "Recording selection required"},
            "audio": {"status": "pending", "reason": "No authorized audio file supplied"},
            "candidates": identification["candidates"],
        }
    else:
        candidate = identification["selected"]
        details = musicbrainz.recording_details(candidate["recording_id"])
        release_detail_status = "unavailable"
        if candidate.get("release_id"):
            try:
                details["selected_release"] = musicbrainz.release_details(
                    candidate["release_id"]
                )
                release_detail_status = "available"
            except musicbrainz.ProviderError as error:
                logger.warning(
                    "Release metadata lookup failed for %s: %s",
                    candidate["release_id"],
                    error,
                )
        metadata = musicbrainz.metadata_from_recording(request, candidate, details)
        metadata["sources"]["release_details"] = (
            "musicbrainz" if release_detail_status == "available" else None
        )

    fields = metadata["fields"]
    album_name = str(fields.get("album_name") or "")
    if "dhurandhar" in album_name.lower():
        fields["album_artist"] = "Shashwat Sachdev"
        fields["is_compilation"] = True
    elif "baahubali" in album_name.lower():
        fields["album_artist"] = "M.M. Keeravani"
        fields["is_compilation"] = True
    elif not fields.get("album_artist") and fields.get("artist") and "," in str(fields.get("artist")):
        fields["album_artist"] = "Various Artists"
        fields["is_compilation"] = True
    results: dict[str, Any] = {
        "status": "partially_completed",
        "metadata": {"status": "success", **metadata},
        "lyrics": {"status": "unavailable"},
        "artwork": {"status": "unavailable"},
        "audio": {"status": "pending", "reason": "No authorized audio file supplied"},
        "recording": candidate,
        "candidates": identification.get("candidates", []),
    }

    destination = _library_directory(fields)
    title = safe_component(fields.get("song_title"), "lyrics")
    existing_lyrics = [
        path
        for path in (
            destination / f"{title}.lrc",
            destination / f"{title}.txt",
        )
        if path.is_file()
    ]
    existing_cover = next(
        (path for path in destination.glob("cover.*") if path.is_file()), None
    )

    if existing_lyrics:
        lyrics_result = {
            "status": "existing",
            "files": [str(path) for path in existing_lyrics],
        }
        results["lyrics"] = {
            "status": "success",
            "synced": any(path.suffix == ".lrc" for path in existing_lyrics),
            "plain": any(path.suffix == ".txt" for path in existing_lyrics),
            "provider": "local_library",
            "files": [str(path) for path in existing_lyrics],
        }
    else:
        try:
            lyrics_result = lyrics.find_lyrics(candidate)
            lyrics_status = lyrics_result.get("status")
            if lyrics_status == "available":
                results["lyrics"] = {
                    "status": "success",
                    "synced": bool(lyrics_result.get("synced")),
                    "plain": bool(lyrics_result.get("plain")),
                    "provider": lyrics_result.get("provider"),
                }
            else:
                lyrics_result = {"status": lyrics_status}
                results["lyrics"] = {"status": "unavailable"}
        except musicbrainz.ProviderError as error:
            logger.warning(
                "Lyrics lookup failed for %s: %s", candidate.get("recording_id"), error
            )
            results["lyrics"] = {"status": "error", "message": str(error)}
            lyrics_result = {"status": "error"}

    if existing_cover:
        artwork_result = {
            "status": "existing",
            "path": str(existing_cover),
        }
        results["artwork"] = {
            "status": "success",
            "provider": "local_library",
            "file": str(existing_cover),
        }
    else:
        try:
            artwork_result = artwork.find_artwork(candidate)
            if artwork_result.get("status") == "available":
                results["artwork"] = {
                    "status": "success",
                    "provider": artwork_result["provider"],
                    "mime_type": artwork_result["mime_type"],
                    "extension": artwork_result["extension"],
                }
            else:
                artwork_result = {"status": "unavailable"}
                results["artwork"] = {"status": "unavailable"}
        except musicbrainz.ProviderError as error:
            logger.warning(
                "Artwork lookup failed for %s: %s", candidate.get("recording_id"), error
            )
            results["artwork"] = {"status": "error", "message": str(error)}
            artwork_result = {"status": "error"}

        # Fallback to thumbnail from downloader if artwork unavailable
        if artwork_result.get("status") != "available":
            thumb_query = (
                str(request.get("source_url"))
                if request.get("source_url")
                else f"{fields.get('song_title')} {fields.get('artist') or ''}".strip()
            )
            thumb_url = downloader.extract_thumbnail_url(thumb_query)
            if thumb_url:
                thumb_art = artwork.artwork_from_url(thumb_url)
                if thumb_art:
                    artwork_result = thumb_art
                    results["artwork"] = {
                        "status": "success",
                        "provider": thumb_art["provider"],
                        "mime_type": thumb_art["mime_type"],
                        "extension": thumb_art["extension"],
                    }

    written = write_assets(
        destination,
        metadata,
        lyrics_result,
        artwork_result,
    )
    results["output_directory"] = str(destination)
    results["files"] = list(written)

    written_lyrics = [
        str(p)
        for p in (destination / f"{title}.lrc", destination / f"{title}.txt")
        if p.is_file()
    ]
    if written_lyrics and results["lyrics"].get("status") == "success":
        results["lyrics"]["files"] = written_lyrics

    cover_path = next(
        (path for path in destination.glob("cover.*") if path.is_file()), None
    )
    if cover_path:
        results["artwork"]["file"] = str(cover_path)
        results["artwork"]["status"] = "success"

    # Audio Download
    if download_audio:
        title_for_file = safe_component(fields.get("song_title"), "track")

        # Determine expected duration
        expected_duration = None
        if candidate.get("duration_ms"):
            expected_duration = round(candidate["duration_ms"] / 1000)
        elif fields.get("duration_ms"):
            expected_duration = round(fields["duration_ms"] / 1000)
        else:
            try:
                import httpx
                r = httpx.get(
                    "https://itunes.apple.com/search",
                    params={
                        "term": f"{fields.get('song_title')} {fields.get('artist') or ''}".strip(),
                        "entity": "song",
                        "limit": 3,
                    },
                    timeout=8,
                ).json()
                res = r.get("results", [])
                if res and res[0].get("trackTimeMillis"):
                    expected_duration = round(res[0]["trackTimeMillis"] / 1000)
            except Exception:
                pass

        existing_audio = next(
            (
                p
                for p in destination.iterdir()
                if p.is_file()
                and p.stem.startswith(title_for_file)
                and p.suffix.lower() in {".m4a", ".mp3", ".flac", ".wav"}
            ),
            None,
        )
        if existing_audio and expected_duration:
            try:
                from mutagen import File as MutagenFile
                mf = MutagenFile(existing_audio)
                if mf and mf.info and abs(mf.info.length - expected_duration) > 20:
                    logger.info(
                        "Existing audio duration %.1fs deviates from expected %ss; redownloading",
                        mf.info.length,
                        expected_duration,
                    )
                    existing_audio.unlink(missing_ok=True)
                    existing_audio = None
            except Exception:
                pass

        if existing_audio:
            audio_path = existing_audio
            audio_source = "local_library"
        else:
            if request.get("source_url"):
                query_or_url = str(request["source_url"])
            else:
                query_or_url = f"{fields.get('song_title')} {fields.get('artist') or ''}".strip()
            try:
                try:
                    audio_path = downloader.download_audio(
                        query_or_url,
                        destination,
                        title_for_file,
                        preferred_codec="m4a",
                        expected_duration=expected_duration,
                    )
                except TypeError:
                    audio_path = downloader.download_audio(
                        query_or_url,
                        destination,
                        title_for_file,
                        preferred_codec="m4a",
                    )
                audio_source = "downloaded"
            except Exception as error:
                logger.exception("Audio download failed: %s", error)
                results["audio"] = {"status": "error", "message": str(error)}
                audio_path = None
                audio_source = None

        if audio_path:
            lyrics_text = lyrics_result.get("plain") or lyrics_result.get("synced")
            if not lyrics_text and (destination / f"{title}.txt").is_file():
                lyrics_text = (destination / f"{title}.txt").read_text(encoding="utf-8")
            elif not lyrics_text and (destination / f"{title}.lrc").is_file():
                lyrics_text = (destination / f"{title}.lrc").read_text(encoding="utf-8")
            if lyrics_text:
                lyrics_text = lyrics.clean_lyrics(lyrics_text)
            try:
                tag_audio(audio_path, fields, cover_path=cover_path, lyrics_text=lyrics_text)
            except Exception as error:
                logger.warning("Tagging audio failed: %s", error)

            try:
                apple_music_result = apple_music.queue_track(
                    audio_path,
                    candidate.get("recording_id") or "unidentified",
                    fields,
                    cover_path,
                    force=True,
                )
            except (FileNotFoundError, OSError, ValueError) as error:
                apple_music_result = {"status": "skipped", "reason": str(error)}

            results["audio"] = {
                "status": "success",
                "file": str(audio_path),
                "source": audio_source,
            }
            results["apple_music"] = apple_music_result
            if str(audio_path) not in results["files"]:
                results["files"].append(str(audio_path))

        if results.get("audio", {}).get("status") == "success":
            results["status"] = "completed"
            results["message"] = (
                f"Song '{fields.get('song_title')}' downloaded with metadata, lyrics, and artwork."
            )
        elif results.get("audio", {}).get("status") == "error":
            results["status"] = "partially_completed"
            results["message"] = (
                f"Metadata, lyrics, and artwork saved, but audio download failed: {results['audio'].get('message')}"
            )
    else:
        results["message"] = (
            "Metadata and available assets were saved. Audio remains pending until you provide an authorized audio file."
        )

    return results
