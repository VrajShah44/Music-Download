import json
import logging
import time
from pathlib import Path
from mutagen import File as MutagenFile

from app.services import processor


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SONGS = [
    {
        "song_title": "Jaiye Sajana",
        "artist": "Jasmine Sandlas, Satinder Sartaaj",
        "album_name": "Dhurandhar The Revenge",
    },
    {
        "song_title": "Hurts So Good",
        "artist": "Astrid S",
        "album_name": "Leave It Beautiful (Complete)",
    },
    {
        "song_title": "Naal Nachna",
        "artist": "Shashwat Sachdev, Afsana Khan, Irshad Kamil, Reble",
        "album_name": "Dhurandhar",
    },
    {
        "song_title": "Gehra Hua",
        "artist": "Shashwat Sachdev, Arijit Singh, Irshad Kamil, Armaan Khan",
        "album_name": "Dhurandhar",
    },
    {
        "song_title": "Ishq Jalakar - Karvaan",
        "artist": "Shashwat Sachdev, Shahzad Ali, Subhadeep Das Chowdhury, Arsh Mohammed",
        "album_name": "Dhurandhar",
    },
    {
        "song_title": "Dhurandhar - Title Track",
        "artist": "Shashwat Sachdev, Hanumankind, Jasmine Sandlas, Sudhir Yaduvanshi",
        "album_name": "Dhurandhar",
    },
    {
        "song_title": "Khoya Hain",
        "artist": "Neeti Mohan, Manoj Muntashir, Kala Bhairava",
        "album_name": "Baahubali - The Beginning",
    },
    {
        "song_title": "Safari",
        "artist": "Serena",
        "album_name": "Safari",
    },
    {
        "song_title": "Where We Started",
        "artist": "Lost Sky, Jex",
        "album_name": "Where We Started",
    },
    {
        "song_title": "Lush Life",
        "artist": "Zara Larsson",
        "album_name": "So Good",
    },
]


def run_batch():
    results = []
    print(f"\n=======================================================")
    print(f"🚀 Starting High-Fidelity Batch Download of {len(SONGS)} Songs...")
    print(f"=======================================================\n")

    for index, song in enumerate(SONGS, 1):
        print(f"\n[{index}/{len(SONGS)}] 🎵 Downloading: {song['song_title']} - {song['artist']}...")
        start_time = time.time()
        try:
            res = processor.process_song(
                song,
                download_audio=True,
                auto_select=True,
            )
            elapsed = time.time() - start_time
            status = res.get("status")
            audio_file = res.get("audio", {}).get("file")
            audio_status = res.get("audio", {}).get("status")
            lyrics_status = res.get("lyrics", {}).get("status")
            art_status = res.get("artwork", {}).get("status")
            art_file = res.get("artwork", {}).get("file")
            apple_status = res.get("apple_music", {}).get("status")

            dur_str = "N/A"
            if audio_file and Path(audio_file).is_file():
                try:
                    mf = MutagenFile(audio_file)
                    if mf and mf.info:
                        sec = round(mf.info.length)
                        dur_str = f"{sec//60}:{sec%60:02d} ({sec}s)"
                except Exception:
                    pass

            item = {
                "index": index,
                "song_title": song["song_title"],
                "artist": song["artist"],
                "album": song["album_name"],
                "status": status,
                "duration": dur_str,
                "audio": audio_file,
                "audio_status": audio_status,
                "lyrics_status": lyrics_status,
                "lyrics_files": res.get("lyrics", {}).get("files", []),
                "artwork": art_file,
                "artwork_status": art_status,
                "apple_music": apple_status,
                "output_dir": res.get("output_directory"),
                "elapsed": round(elapsed, 2),
            }
            results.append(item)
            print(f"  ✅ Completed in {elapsed:.1f}s | Duration: {dur_str} | Lyrics: {lyrics_status} | Art: {art_status} ({Path(art_file).name if art_file else 'none'}) | Apple Music: {apple_status}")
        except Exception as err:
            elapsed = time.time() - start_time
            print(f"  ❌ Failed after {elapsed:.1f}s: {err}")
            results.append({
                "index": index,
                "song_title": song["song_title"],
                "artist": song["artist"],
                "status": "failed",
                "error": str(err),
                "elapsed": round(elapsed, 2),
            })

    output_summary = Path("data/batch_results.json")
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n=======================================================")
    print(f"🎉 Batch Download Complete! Summary written to {output_summary}")
    print(f"=======================================================\n")


if __name__ == "__main__":
    run_batch()
