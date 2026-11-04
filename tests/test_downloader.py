from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import downloader


def test_find_ffmpeg():
    path = downloader._find_ffmpeg()
    assert path is not None
    assert Path(path).exists()


def test_download_audio_invokes_yt_dlp(tmp_path, monkeypatch):
    mock_ydl = MagicMock()
    mock_ydl_class = MagicMock(return_value=mock_ydl)
    mock_ydl.__enter__.return_value = mock_ydl

    def fake_download(urls):
        temp_file = tmp_path / ".download-12345.m4a"
        temp_file.write_bytes(b"dummy audio content")
        return 0

    mock_ydl.download.side_effect = fake_download
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", mock_ydl_class)
    mock_uuid = MagicMock()
    mock_uuid.hex = "12345"
    monkeypatch.setattr(downloader.uuid, "uuid4", lambda: mock_uuid)

    final_path = downloader.download_audio("test song query", tmp_path, "test_track")
    assert final_path.is_file()
    assert final_path.name == "test_track.m4a"
    assert final_path.read_bytes() == b"dummy audio content"


def test_extract_thumbnail_url(monkeypatch):
    mock_ydl = MagicMock()
    mock_ydl_class = MagicMock(return_value=mock_ydl)
    mock_ydl.__enter__.return_value = mock_ydl

    mock_ydl.extract_info.return_value = {
        "entries": [{"thumbnail": "https://example.com/thumb.jpg"}]
    }
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", mock_ydl_class)

    url = downloader.extract_thumbnail_url("query")
    assert url == "https://example.com/thumb.jpg"
