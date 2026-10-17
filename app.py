from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import musicbrainzngs
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from mutagen.mp4 import MP4, MP4Cover
from pydantic import BaseModel, Field

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
MUSIC_ROOT = (BASE_DIR / os.getenv("MUSIC_ROOT", "./dev-music/structured-music")).resolve()
STAGING_DIR = (BASE_DIR / os.getenv("STAGING_DIR", "./dev-music/staging")).resolve()
PORT = int(os.getenv("PORT", "8099"))
MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
STAGING_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="YouTube to Jellyfin Music Ingest")

INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
SPACE_RE = re.compile(r"\s+")


def clean_name(value: str, fallback: str = "Untitled") -> str:
    cleaned = INVALID_CHARS.sub("", str(value or "")).strip().rstrip(".")
    return cleaned or fallback


def normalize(value: str) -> str:
    return re.sub(r'[".\-_]', "", SPACE_RE.sub(" ", value or "").strip().lower())


def safe_child(parent: Path, name: str) -> Path:
    candidate = (parent / clean_name(name)).resolve()
    if candidate.parent != parent.resolve() and parent.resolve() not in candidate.parents:
        raise HTTPException(400, "Invalid path component")
    return candidate


def folders(parent: Path) -> list[str]:
    if not parent.exists():
        return []
    return sorted(item.name for item in parent.iterdir() if item.is_dir() and not item.name.startswith("."))


def text_value(info: dict[str, Any], key: str) -> str:
    value = info.get(key)
    return str(value).strip() if value else ""


def track_from_info(info: dict[str, Any]) -> dict[str, Any]:
    title = text_value(info, "title") or "Untitled"
    artist = text_value(info, "artist")
    if not artist and " - " in title:
        artist, title = (part.strip() for part in title.split(" - ", 1))
    artist = artist or text_value(info, "uploader") or "Unknown Artist"
    album = text_value(info, "album") or text_value(info, "playlist_title") or title
    return {
        "yt_id": text_value(info, "id"),
        "title": title,
        "uploader": text_value(info, "uploader"),
        "artist": artist,
        "album": album,
        "thumb": text_value(info, "thumbnail"),
        "year": text_value(info, "release_year") or text_value(info, "upload_date")[:4],
    }


def yt_dlp_command() -> list[str]:
    executable = shutil.which("yt-dlp")
    return [executable] if executable else [sys.executable, "-m", "yt_dlp"]


def extract_info(url: str) -> dict[str, Any]:
    command = yt_dlp_command() + ["--flat-playlist", "--dump-single-json", "--skip-download", "--no-warnings", url]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return json.loads(result.stdout)


class URLRequest(BaseModel):
    url: str


class SuggestRequest(BaseModel):
    artist: str = ""
    album: str = ""
    title: str = ""


class SaveRequest(BaseModel):
    mode: str = "grouped"
    items: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/artists")
def artists(q: str = "") -> dict[str, Any]:
    names = folders(MUSIC_ROOT)
    query = normalize(q)
    return {"artists": [name for name in names if not query or query in normalize(name)]}


@app.get("/api/albums")
def albums(artist: str) -> dict[str, Any]:
    artist_path = next((MUSIC_ROOT / name for name in folders(MUSIC_ROOT) if normalize(name) == normalize(artist)), None)
    return {"albums": folders(artist_path) if artist_path else []}


@app.post("/api/probe")
def probe(request: URLRequest) -> dict[str, Any]:
    try:
        info = extract_info(request.url)
    except Exception as exc:
        raise HTTPException(400, f"Could not probe link: {exc}") from exc
    entries = info.get("entries") or [info]
    tracks = [track_from_info(entry) for entry in entries if entry]
    return {"type": "playlist" if info.get("_type") == "playlist" or len(tracks) > 1 else "single", "tracks": tracks}


@app.post("/api/download")
def download(request: URLRequest) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job_dir = STAGING_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        info = extract_info(request.url)
        command = yt_dlp_command() + [
            "-f", "bestaudio/best",
            "--extract-audio",
            "--audio-format", "m4a",
            "--audio-quality", "256K",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "--no-warnings",
            "-o", str(job_dir / "%(autonumber)02d-%(id)s.%(ext)s"),
            request.url,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
    except FileNotFoundError as exc:
        raise HTTPException(400, "ffmpeg is missing. Install it with: brew install ffmpeg") from exc
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f"Download failed: {exc}") from exc
    entries = info.get("entries") or [info]
    staged: list[dict[str, Any]] = []
    audio_files = sorted(job_dir.glob("*.m4a"))
    for index, entry in enumerate(entries):
        if not entry or index >= len(audio_files):
            continue
        track = track_from_info(entry)
        metadata = {**track, "staging_id": f"{job_id}:{audio_files[index].name}", "audio": audio_files[index].name}
        (audio_files[index].with_suffix(".json")).write_text(json.dumps(metadata), encoding="utf-8")
        staged.append({"staging_id": metadata["staging_id"], "yt_id": track["yt_id"], "title": track["title"], "thumb_path": track["thumb"]})
    return {"staged": staged}


@app.post("/api/suggest")
def suggest(request: SuggestRequest) -> dict[str, Any]:
    musicbrainzngs.set_useragent("song-downloader", "1.0", "local")
    suggestions: list[dict[str, str]] = []
    try:
        release_results = musicbrainzngs.search_releases(artist=request.artist, release=request.album, limit=5).get("release-list", [])
        for release in release_results:
            suggestions.append({"artist": release.get("artist-credit-phrase", request.artist), "album": release.get("title", request.album), "title": request.title, "year": release.get("date", "")[:4]})
        if not suggestions and request.title:
            recordings = musicbrainzngs.search_recordings(artist=request.artist, recording=request.title, limit=5).get("recording-list", [])
            suggestions = [{"artist": item.get("artist-credit-phrase", request.artist), "album": request.album, "title": item.get("title", request.title), "year": ""} for item in recordings]
    except Exception as exc:
        raise HTTPException(502, f"MusicBrainz lookup failed: {exc}") from exc
    return {"suggestions": suggestions}


def find_staged(staging_id: str) -> tuple[Path, dict[str, Any]]:
    if ":" not in staging_id:
        raise HTTPException(400, "Invalid staging id")
    job_id, filename = staging_id.split(":", 1)
    job_dir = (STAGING_DIR / job_id).resolve()
    audio = (job_dir / filename).resolve()
    metadata_path = audio.with_suffix(".json")
    if STAGING_DIR not in job_dir.parents or not audio.is_file() or not metadata_path.is_file():
        raise HTTPException(404, "Staged track not found")
    return audio, json.loads(metadata_path.read_text(encoding="utf-8"))


def clear_staged_file(audio: Path) -> None:
    for staged_file in audio.parent.glob(f"{audio.stem}.*"):
        staged_file.unlink(missing_ok=True)
    try:
        audio.parent.rmdir()
    except OSError:
        pass


def write_nfo(path: Path, kind: str, artist: str, album: str, year: str) -> None:
    if path.exists():
        return
    if kind == "artist":
        body = f"<artist><name>{html.escape(artist)}</name></artist>"
    else:
        body = f"<album><title>{html.escape(album)}</title><artist>{html.escape(artist)}</artist>"
        if year:
            body += f"<year>{html.escape(year)}</year>"
        body += "</album>"
    path.write_text(f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{body}\n", encoding="utf-8")


def tag_audio(path: Path, artist: str, album: str, title: str, number: int, total: int, year: str, genre: str) -> None:
    audio = MP4(path)
    audio["\xa9ART"] = [artist]
    audio["\xa9alb"] = [album]
    audio["\xa9nam"] = [title]
    audio["trkn"] = [(number, total)]
    if year:
        audio["\xa9day"] = [year]
    if genre:
        audio["\xa9gen"] = [genre]
    cover = next((path.with_name(candidate) for candidate in ("cover.jpg", "cover.jpeg", "cover.png") if path.with_name(candidate).exists()), None)
    if cover:
        audio["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG if cover.suffix == ".jpg" else MP4Cover.FORMAT_PNG)]
    audio.save()


@app.post("/api/save")
def save(request: SaveRequest) -> dict[str, Any]:
    written, skipped, conflicts, target_dirs = [], [], [], []
    for item in request.items:
        artist_input = str(item.get("artist", "")).strip()
        album_input = str(item.get("album", "")).strip()
        if item.get("new_artist"):
            artist_input = str(item.get("new_artist_name") or artist_input)
        else:
            match = next((name for name in folders(MUSIC_ROOT) if normalize(name) == normalize(artist_input)), None)
            artist_input = match or artist_input
        if not artist_input:
            raise HTTPException(400, "Artist is required")
        artist_dir = safe_child(MUSIC_ROOT, artist_input)
        if item.get("new_album"):
            album_input = str(item.get("new_album_name") or album_input)
        if not album_input:
            raise HTTPException(400, "Album is required")
        album_dir = safe_child(artist_dir, album_input)
        album_dir.mkdir(parents=True, exist_ok=True)
        artist_dir.mkdir(parents=True, exist_ok=True)
        write_nfo(artist_dir / "artist.nfo", "artist", artist_input, album_input, str(item.get("year", "")))
        write_nfo(album_dir / "album.nfo", "album", artist_input, album_input, str(item.get("year", "")))
        target_dirs.append(str(album_dir.relative_to(MUSIC_ROOT)))
        tracks = item.get("tracks", [])
        total = len(tracks)
        for number, track in enumerate(tracks, 1):
            staged_path, staged_meta = find_staged(str(track.get("staging_id", "")))
            title = str(track.get("title") or staged_meta.get("title") or "Untitled").strip()
            final = album_dir / f"{clean_name(title)}.m4a"
            if final.exists():
                conflict_mode = str(item.get("on_conflict", "skip")).lower()
                conflicts.append(str(final.relative_to(MUSIC_ROOT)))
                if conflict_mode != "overwrite":
                    skipped.append(str(final.relative_to(MUSIC_ROOT)))
                    continue
            shutil.copy2(staged_path, final)
            thumbnail = next((candidate for candidate in staged_path.parent.iterdir() if candidate.suffix.lower() in {".jpg", ".jpeg", ".png"} and candidate.stem.startswith(staged_path.stem.split(".")[0])), None)
            if thumbnail and not (album_dir / "cover.jpg").exists():
                shutil.copy2(thumbnail, album_dir / "cover.jpg")
            tag_audio(final, artist_input, album_input, title, number, total, str(item.get("year") or staged_meta.get("year") or ""), str(item.get("genre", "")))
            written.append(str(final.relative_to(MUSIC_ROOT)))
            clear_staged_file(staged_path)
    return {"written": written, "skipped": skipped, "conflicts": conflicts, "target_dirs": target_dirs}


@app.delete("/api/staging")
def clear_staging() -> dict[str, str]:
    for child in STAGING_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    return {"message": "Staging cleared"}
