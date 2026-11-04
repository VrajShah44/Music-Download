import io
import wave

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "jobs.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_process_song_creates_persistent_job(client, monkeypatch):
    from app.services import processor

    def fake_process(request, selected_recording_id=None):
        return {
            "status": "partially_completed",
            "metadata": {"status": "success"},
            "lyrics": {"status": "unavailable"},
            "artwork": {"status": "unavailable"},
            "audio": {"status": "pending"},
        }

    monkeypatch.setattr(processor, "process_song", fake_process)
    response = client.post(
        "/api/v1/songs/process",
        json={
            "song_title": "Example track",
            "artist": None,
            "featured_artists": [],
            "album_name": None,
            "movie_name": None,
            "composer": None,
            "language": None,
            "source_url": None,
        },
    )
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "pending"
    assert accepted["song_title"] == "Example track"

    job_response = client.get(f"/api/v1/jobs/{accepted['job_id']}")
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["song"]["song_title"] == "Example track"
    assert job["status"] == "partially_completed"
    assert job["result"]["metadata"]["status"] == "success"


def test_process_song_rejects_missing_title(client):
    response = client.post(
        "/api/v1/songs/process",
        json={"artist": "An artist"},
    )
    assert response.status_code == 422


def test_process_song_normalizes_shortcuts_false_for_missing_optional_fields(
    client, monkeypatch
):
    from app.services import processor

    monkeypatch.setattr(processor, "process_song", lambda request, selected_recording_id=None: {
        "status": "partially_completed",
        "metadata": {"status": "success"},
        "lyrics": {"status": "unavailable"},
        "artwork": {"status": "unavailable"},
        "audio": {"status": "pending"},
    })
    response = client.post(
        "/api/v1/songs/process",
        json={
            "song_title": "Jaiye Sajana",
            "artist": "Jasmine Sandlas",
            "featured_artists": ["Satinder Sartaj"],
            "movie_name": "Dhurandhar: The Revenge",
            "album_name": False,
            "composer": False,
            "language": False,
            "source_url": False,
        },
    )
    assert response.status_code == 202
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert job["song"]["album_name"] is None
    assert job["song"]["composer"] is None
    assert job["song"]["language"] is None
    assert job["song"]["source_url"] is None


def test_ambiguous_job_is_terminal_for_shortcut_but_candidates_can_be_selected(
    client, monkeypatch
):
    from app.services import processor

    monkeypatch.setattr(processor, "process_song", lambda request, selected_recording_id=None: {
        "status": "needs_selection",
        "candidates": [{"recording_id": "recording-a"}],
        "message": "Select a recording",
    })
    response = client.post(
        "/api/v1/songs/process",
        json={"song_title": "Ambiguous song"},
    )
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "partially_completed"
    assert job["result"]["status"] == "needs_selection"
    assert job["result"]["candidates"][0]["recording_id"] == "recording-a"

    monkeypatch.setattr(processor, "process_song", lambda request, selected_recording_id=None: {
        "status": "partially_completed",
        "metadata": {"status": "success"},
        "lyrics": {"status": "unavailable"},
        "artwork": {"status": "unavailable"},
        "audio": {"status": "pending"},
        "selected_recording_id": selected_recording_id,
    })
    selected = client.post(
        f"/api/v1/jobs/{job['job_id']}/select",
        json={"recording_id": "recording-a"},
    )
    assert selected.status_code == 202
    final_job = client.get(f"/api/v1/jobs/{job['job_id']}").json()
    assert final_job["result"]["selected_recording_id"] == "recording-a"


def test_unknown_job_returns_404(client):
    response = client.get("/api/v1/jobs/not-a-job")
    assert response.status_code == 404


def test_audio_upload_requires_user_confirmation_and_tags_audio(
    client, monkeypatch, tmp_path
):
    from app.services import apple_music
    from app.services import processor

    library = tmp_path / "library"
    monkeypatch.setattr(processor, "LIBRARY_DIR", library)
    output_directory = library / "Unknown Language" / "Album" / "Track - Artist"
    fields = {
        "song_title": "Track",
        "artist": "Artist",
        "album_name": "Album",
    }
    monkeypatch.setattr(
        processor,
        "process_song",
        lambda _request, selected_recording_id=None: {
            "status": "partially_completed",
            "metadata": {"status": "success", "fields": fields},
            "lyrics": {"status": "unavailable"},
            "artwork": {"status": "unavailable"},
            "audio": {"status": "pending"},
            "output_directory": str(output_directory),
        },
    )
    monkeypatch.setattr(
        apple_music,
        "queue_track",
        lambda audio_path, job_id, fields, cover_path: {
            "status": "queued",
            "file": str(audio_path),
        },
    )
    accepted = client.post(
        "/api/v1/songs/process",
        json={"song_title": "Track", "artist": "Artist"},
    ).json()

    wave_data = io.BytesIO()
    with wave.open(wave_data, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\x00\x00" * 800)
    audio_bytes = wave_data.getvalue()
    files = {"audio_file": ("track.wav", audio_bytes, "audio/wav")}
    route = f"/api/v1/jobs/{accepted['job_id']}/audio"

    rejected = client.post(
        route,
        data={"confirm_recording": "false"},
        files=files,
    )
    assert rejected.status_code == 422

    uploaded = client.post(
        route,
        data={"confirm_recording": "true"},
        files=files,
    )
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["verification"] == "user_confirmed"
    assert body["source"] == "user_provided"
    assert body["apple_music"]["status"] == "queued"
    assert body["status"] == "partially_completed"
    assert (output_directory / "Track.wav").is_file()
    final_job = client.get(f"/api/v1/jobs/{accepted['job_id']}").json()
    assert final_job["status"] == "partially_completed"
    assert final_job["result"]["audio"]["status"] == "success"
    assert final_job["result"]["apple_music"]["status"] == "queued"
