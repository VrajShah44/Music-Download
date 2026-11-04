from unittest.mock import MagicMock
from app import cli


def test_cli_requires_title_or_url(capsys):
    code = cli.download_cli(None, url=None)
    assert code == 1
    captured = capsys.readouterr()
    assert "Error: You must provide either a song title or a source URL" in captured.err


def test_cli_successful_flow(monkeypatch, capsys):
    fake_result = {
        "status": "completed",
        "message": "Song downloaded successfully",
        "output_directory": "/tmp/music",
        "metadata": {"fields": {"song_title": "Test Title", "artist": "Test Artist"}},
        "lyrics": {"status": "success", "provider": "LRCLIB", "files": ["/tmp/music/track.lrc"]},
        "artwork": {"status": "success", "provider": "Cover Art Archive", "file": "/tmp/music/cover.jpg"},
        "audio": {"status": "success", "source": "downloaded", "file": "/tmp/music/track.m4a"},
        "apple_music": {"status": "queued", "message": "Import queued"},
    }
    monkeypatch.setattr(cli.processor, "process_song", lambda *args, **kwargs: fake_result)

    code = cli.download_cli("Test Title", artist="Test Artist")
    assert code == 0
    captured = capsys.readouterr()
    assert "Title:    Test Title" in captured.out
    assert "Lyrics:   success" in captured.out
    assert "Artwork:  success" in captured.out
    assert "Audio:    success" in captured.out
