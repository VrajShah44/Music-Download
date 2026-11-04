import wave

from app.services import apple_music
from app.services.tagging import tag_audio


def _tagged_audio(path, recording_id="recording-123"):
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\x00\x00" * 800)
    tag_audio(
        path,
        {
            "song_title": "Track",
            "artist": "Artist",
            "musicbrainz_recording_id": recording_id,
        },
    )


def test_audio_is_atomically_queued_in_automatically_add_folder(tmp_path, monkeypatch):
    source_directory = tmp_path / "library"
    source_directory.mkdir()
    audio = source_directory / "track.m4a"
    _tagged_audio(audio)
    cover = source_directory / "cover.jpg"
    cover.write_bytes(b"art")
    auto_add = tmp_path / "Automatically Add to Music.localized"
    auto_add.mkdir()
    monkeypatch.setenv("SMC_AUTO_ADD_DIR", str(auto_add))

    result = apple_music.queue_track(
        audio,
        "job-123",
        {
            "song_title": "Track",
            "artist": "Artist",
            "musicbrainz_recording_id": "recording-123",
        },
        cover,
    )
    assert result["status"] == "queued"
    assert result["file"] == str(auto_add / "Track - Artist.m4a")
    assert (auto_add / "Track - Artist.m4a").is_file()
    assert not list(auto_add.glob("*.partial"))
    assert result["automatic_add_folder"] == str(auto_add)


def test_repeat_queue_is_idempotent_for_same_recording(tmp_path, monkeypatch):
    source = tmp_path / "library"
    auto_add = tmp_path / "auto-add"
    source.mkdir()
    auto_add.mkdir()
    monkeypatch.setenv("SMC_AUTO_ADD_DIR", str(auto_add))
    audio = source / "track.m4a"
    _tagged_audio(audio)
    fields = {"song_title": "Track", "artist": "Artist", "musicbrainz_recording_id": "recording-123"}

    first = apple_music.queue_track(audio, "job-1", fields)
    second = apple_music.queue_track(audio, "job-2", fields)
    assert first["status"] == "queued"
    assert second["status"] == "already_queued"


def test_audio_already_in_local_library_is_not_queued_again(tmp_path, monkeypatch):
    media = tmp_path / "Media"
    library = media / "Music" / "Artist" / "Album"
    auto_add = media / "Automatically Add to Music.localized"
    library.mkdir(parents=True)
    auto_add.mkdir()
    monkeypatch.setenv("SMC_AUTO_ADD_DIR", str(auto_add))
    audio = library / "track.m4a"
    _tagged_audio(audio)
    source = tmp_path / "uploaded.m4a"
    _tagged_audio(source)
    result = apple_music.queue_track(
        source,
        "job",
        {"song_title": "Track", "musicbrainz_recording_id": "recording-123"},
    )
    assert result["status"] == "already_in_library"
    assert not list(auto_add.iterdir())


def test_flac_is_preserved_without_auto_import(tmp_path, monkeypatch):
    auto_add = tmp_path / "auto-add"
    auto_add.mkdir()
    monkeypatch.setenv("SMC_AUTO_ADD_DIR", str(auto_add))
    audio = tmp_path / "track.flac"
    audio.touch()
    result = apple_music.queue_track(audio, "job", {"song_title": "Track"})
    assert result["status"] == "unsupported_format"
    assert result["file"] == str(audio)
    assert not list(auto_add.iterdir())


def test_wav_is_preserved_without_import_to_avoid_losing_tags(tmp_path, monkeypatch):
    auto_add = tmp_path / "auto-add"
    auto_add.mkdir()
    monkeypatch.setenv("SMC_AUTO_ADD_DIR", str(auto_add))
    audio = tmp_path / "track.wav"
    _tagged_audio(audio)
    result = apple_music.queue_track(audio, "job", {"song_title": "Track"})
    assert result["status"] == "unsupported_format"
    assert "MP3 and M4A" in result["message"]
    assert audio.is_file()
    assert not list(auto_add.iterdir())


def test_missing_auto_add_directory_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SMC_AUTO_ADD_DIR", str(tmp_path / "missing"))
    try:
        apple_music.queue_track(
            tmp_path / "track.m4a", "job", {"song_title": "Track"}
        )
    except FileNotFoundError as error:
        assert "does not exist" in str(error)
    else:
        raise AssertionError("Expected missing auto-add folder error")
