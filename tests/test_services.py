import json
import wave
from pathlib import Path

from mutagen.wave import WAVE

from app.services import lyrics, musicbrainz
from app.services.library import safe_component, track_directory, write_assets
from app.services.tagging import tag_audio
from app.services import processor


def test_musicbrainz_selects_unique_strong_candidate(monkeypatch):
    monkeypatch.setattr(
        musicbrainz,
        "_request_json",
        lambda *_args, **_kwargs: {
            "recordings": [
                {
                    "id": "recording-one",
                    "title": "Jaiye Sajana",
                    "artist-credit": [{"name": "Jasmine Sandlas"}],
                    "releases": [
                        {
                            "id": "release-one",
                            "title": "Dhurandhar: The Revenge",
                            "date": "2026-01-01",
                        }
                    ],
                    "length": 211000,
                },
                {
                    "id": "recording-two",
                    "title": "Jaiye Sajana (Live)",
                    "artist-credit": [{"name": "Different Artist"}],
                    "releases": [],
                },
            ]
        },
    )
    result = musicbrainz.find_recordings(
        {
            "song_title": "Jaiye Sajana",
            "artist": "Jasmine Sandlas",
            "featured_artists": [],
            "movie_name": "Dhurandhar: The Revenge",
        }
    )
    assert result["status"] == "matched"
    assert result["selected"]["recording_id"] == "recording-one"
    assert result["selected"]["year"] == "2026"


def test_musicbrainz_does_not_guess_when_multiple_candidates_are_close(monkeypatch):
    monkeypatch.setattr(
        musicbrainz,
        "_request_json",
        lambda *_args, **_kwargs: {
            "recordings": [
                {
                    "id": f"recording-{index}",
                    "title": "Same Song",
                    "artist-credit": [{"name": "Same Artist"}],
                    "releases": [{"id": f"release-{index}", "title": "Same Album"}],
                }
                for index in range(2)
            ]
        },
    )
    result = musicbrainz.find_recordings(
        {
            "song_title": "Same Song",
            "artist": "Same Artist",
            "album_name": "Same Album",
        }
    )
    assert result["status"] == "needs_selection"
    assert result["selected"] is None
    assert len(result["candidates"]) == 2


def test_musicbrainz_uses_explicit_work_composer_and_lyricist_credits(monkeypatch):
    monkeypatch.setattr(
        musicbrainz,
        "work_details",
        lambda _work_id: {
            "relations": [
                {"type": "composer", "artist": {"name": "Verified Composer"}},
                {"type": "lyricist", "artist": {"name": "Verified Lyricist"}},
            ]
        },
    )
    metadata = musicbrainz.metadata_from_recording(
        {"song_title": "Song"},
        {"recording_id": "recording-a", "release_id": "release-a"},
        {
            "id": "recording-a",
            "title": "Song",
            "artist-credit": [{"name": "Artist"}],
            "selected_release": {
                "media": [
                    {
                        "position": 1,
                        "track-count": 2,
                        "tracks": [
                            {"recording": {"id": "recording-other"}, "number": "1"},
                            {"recording": {"id": "recording-a"}, "number": "2"},
                        ],
                    }
                ],
                "label-info": [{"label": {"name": "Example Label"}}],
            },
            "relations": [
                {
                    "type": "performance",
                    "work": {"id": "work-a"},
                }
            ],
        },
    )
    assert metadata["fields"]["composer"] == "Verified Composer"
    assert metadata["fields"]["lyricist"] == "Verified Lyricist"
    assert metadata["sources"]["composer"] == "musicbrainz"
    assert metadata["fields"]["track_number"] == "2"
    assert metadata["fields"]["disc_number"] == "1"
    assert metadata["fields"]["total_tracks"] == 2
    assert metadata["fields"]["label"] == "Example Label"


def test_lrclib_accepts_only_matching_recording(monkeypatch):
    def fake_get(path, params):
        assert path == "get"
        assert params["duration"] == 211
        return {
            "trackName": "Jaiye Sajana",
            "artistName": "Jasmine Sandlas",
            "albumName": "Dhurandhar: The Revenge",
            "syncedLyrics": "[00:01.00]Example",
            "plainLyrics": "Example",
        }

    monkeypatch.setattr(lyrics, "_get_json", fake_get)
    result = lyrics.find_lyrics(
        {
            "title": "Jaiye Sajana",
            "artist": "Jasmine Sandlas",
            "release_name": "Dhurandhar: The Revenge",
            "duration_ms": 211000,
        }
    )
    assert result["status"] == "available"
    assert result["synced"].startswith("[00:01.00]")


def test_lrclib_rejects_same_title_for_different_recording(monkeypatch):
    monkeypatch.setattr(
        lyrics,
        "_get_json",
        lambda path, _params: (
            None
            if path == "get"
            else [
                {
                    "trackName": "Jaiye Sajana",
                    "artistName": "Unrelated Artist",
                    "albumName": "Unrelated Album",
                    "plainLyrics": "Do not associate this",
                }
            ]
        ),
    )
    result = lyrics.find_lyrics(
        {
            "title": "Jaiye Sajana",
            "artist": "Jasmine Sandlas",
            "release_name": "Dhurandhar: The Revenge",
        }
    )
    assert result == {"status": "unavailable", "synced": None, "plain": None}


def test_library_sanitizes_and_writes_only_available_assets(tmp_path):
    fields = {"song_title": "../Track", "artist": "Artist", "movie_name": "Film"}
    destination = track_directory(tmp_path, fields)
    metadata = {"fields": fields}
    written = write_assets(
        destination,
        metadata,
        {"status": "available", "synced": "[00:01.00]Line", "plain": None},
        {"status": "available", "extension": ".jpg", "data": b"original"},
    )
    assert ".." not in destination.name
    assert (destination / "metadata.json").is_file()
    assert (destination / "_Track.lrc").is_file()
    assert len(written) == 3
    assert json.loads((destination / "metadata.json").read_text()) == metadata
    assert safe_component("../one", "fallback") == "_one"
    write_assets(
        destination,
        metadata,
        {"status": "available", "synced": "[00:02.00]Replacement"},
        {"status": "available", "extension": ".jpg", "data": b"replacement"},
    )
    assert (destination / "_Track.lrc").read_text() == "[00:01.00]Line"
    assert (destination / "cover.jpg").read_bytes() == b"original"


def test_wav_tagging_fills_empty_metadata_without_overwriting_existing(tmp_path):
    audio_path = tmp_path / "input.wav"
    with wave.open(str(audio_path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\x00\x00" * 800)
    audio = WAVE(audio_path)
    audio.add_tags()
    from mutagen.id3 import TIT2

    audio.tags.add(TIT2(encoding=3, text=["Existing title"]))
    audio.save()
    tag_audio(
        audio_path,
        {"song_title": "New title", "artist": "Jasmine Sandlas"},
    )
    tagged = WAVE(audio_path)
    assert str(tagged.tags.get("TIT2")) == "Existing title"
    assert str(tagged.tags.get("TPE1")) == "Jasmine Sandlas"


def test_processing_pipeline_saves_verified_assets_but_keeps_audio_pending(
    tmp_path, monkeypatch
):
    candidate = {
        "recording_id": "recording-a",
        "title": "Jaiye Sajana",
        "artist": "Jasmine Sandlas, Satinder Sartaaj",
        "artist_names": ["Jasmine Sandlas", "Satinder Sartaaj"],
        "album_artist": "Shashwat Sachdev",
        "release_id": "release-a",
        "release_name": "Dhurandhar: The Revenge",
        "duration_ms": 179300,
        "score": 1.0,
    }
    monkeypatch.setattr(processor, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(
        processor.musicbrainz,
        "find_recordings",
        lambda _request: {
            "status": "matched",
            "selected": candidate,
            "candidates": [candidate],
        },
    )
    monkeypatch.setattr(processor.musicbrainz, "recording_details", lambda _id: {})
    monkeypatch.setattr(
        processor.musicbrainz,
        "metadata_from_recording",
        lambda _request, _candidate, _details: {
            "fields": {
                "song_title": "Jaiye Sajana",
                "artist": "Jasmine Sandlas, Satinder Sartaaj",
                "album_name": "Dhurandhar: The Revenge",
                "musicbrainz_recording_id": "recording-a",
            },
            "sources": {"musicbrainz": "https://musicbrainz.org/recording/recording-a"},
        },
    )
    monkeypatch.setattr(
        processor.lyrics,
        "find_lyrics",
        lambda _candidate: {
            "status": "available",
            "synced": "[00:01.00]Test line",
            "plain": "Test line",
            "provider": "LRCLIB",
        },
    )
    monkeypatch.setattr(
        processor.artwork,
        "find_artwork",
        lambda _candidate: {
            "status": "available",
            "data": b"\xff\xd8\xfftest",
            "mime_type": "image/jpeg",
            "extension": ".jpg",
            "provider": "Cover Art Archive",
        },
    )
    result = processor.process_song({"song_title": "Jaiye Sajana"}, download_audio=False)
    output = Path(result["output_directory"])
    assert result["status"] == "partially_completed"
    assert result["lyrics"]["status"] == "success"
    assert result["artwork"]["status"] == "success"
    assert result["audio"]["status"] == "pending"
    assert (output / "Jaiye Sajana.lrc").exists()
    assert (output / "Jaiye Sajana.txt").exists()
    assert (output / "cover.jpg").read_bytes() == b"\xff\xd8\xfftest"


def test_processing_pipeline_downloads_and_tags_song(tmp_path, monkeypatch):
    candidate = {
        "recording_id": "recording-a",
        "title": "Jaiye Sajana",
        "artist": "Jasmine Sandlas, Satinder Sartaaj",
        "artist_names": ["Jasmine Sandlas", "Satinder Sartaaj"],
        "album_artist": "Shashwat Sachdev",
        "release_id": "release-a",
        "release_name": "Dhurandhar: The Revenge",
        "duration_ms": 179300,
        "score": 1.0,
    }
    monkeypatch.setattr(processor, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(
        processor.musicbrainz,
        "find_recordings",
        lambda _request: {
            "status": "matched",
            "selected": candidate,
            "candidates": [candidate],
        },
    )
    monkeypatch.setattr(processor.musicbrainz, "recording_details", lambda _id: {})
    monkeypatch.setattr(
        processor.musicbrainz,
        "metadata_from_recording",
        lambda _request, _candidate, _details: {
            "fields": {
                "song_title": "Jaiye Sajana",
                "artist": "Jasmine Sandlas, Satinder Sartaaj",
                "album_name": "Dhurandhar: The Revenge",
                "musicbrainz_recording_id": "recording-a",
            },
            "sources": {"musicbrainz": "https://musicbrainz.org/recording/recording-a"},
        },
    )
    monkeypatch.setattr(
        processor.lyrics,
        "find_lyrics",
        lambda _candidate: {
            "status": "available",
            "synced": "[00:01.00]Test line",
            "plain": "Test line",
            "provider": "LRCLIB",
        },
    )
    monkeypatch.setattr(
        processor.artwork,
        "find_artwork",
        lambda _candidate: {
            "status": "available",
            "data": b"\xff\xd8\xfftest",
            "mime_type": "image/jpeg",
            "extension": ".jpg",
            "provider": "Cover Art Archive",
        },
    )

    def fake_download_audio(query, dest, stem, preferred_codec="m4a"):
        file_path = dest / f"{stem}.wav"
        with wave.open(str(file_path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(8000)
            f.writeframes(b"\x00\x00" * 800)
        return file_path

    monkeypatch.setattr(processor.downloader, "download_audio", fake_download_audio)

    result = processor.process_song({"song_title": "Jaiye Sajana"}, download_audio=True)
    output = Path(result["output_directory"])
    assert result["status"] == "completed"
    assert result["lyrics"]["status"] == "success"
    assert result["artwork"]["status"] == "success"
    assert result["audio"]["status"] == "success"
    assert (output / "Jaiye Sajana.lrc").exists()
    assert (output / "Jaiye Sajana.txt").exists()
    assert (output / "cover.jpg").exists()
    assert (output / "Jaiye Sajana.wav").exists()
