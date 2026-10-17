# Needle Drop

A local FastAPI web app for turning YouTube videos and playlists into tagged AAC `.m4a` files laid out for Jellyfin. It does not call Jellyfin and has no authentication, database, tunnel, or deployment layer.

For a headless Debian server deployment, see [DEPLOY_DEBIAN.md](DEPLOY_DEBIAN.md).

## macOS quickstart

1. Install ffmpeg: `brew install ffmpeg`
2. Create an environment: `python3.11 -m venv .venv && source .venv/bin/activate`
3. Install Python dependencies: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and adjust paths if needed.
5. Start the app: `python3 -m uvicorn app:app --host 127.0.0.1 --port ${PORT:-8099}`
6. Open http://127.0.0.1:8099

The default folders are `./dev-music/structured-music` and `./dev-music/staging`. They are created automatically and ignored by git. The application never writes outside those configured roots.

## Workflow

Probe a link, review the detected tracks, download to staging, match or create the artist and album folders, edit metadata, optionally request MusicBrainz suggestions, then save. Existing artist folder spelling is preserved and duplicate files are skipped by default. YouTube thumbnails become the album `cover.jpg`; existing NFO files are never overwritten.

The grouped mode currently applies one artist and album selection to all staged tracks. The API accepts `mode: per_track` and item arrays for per-track workflows; the compact UI focuses on the default grouped workflow.

## Verification

Run `python -m py_compile app.py` for a dependency-free syntax check. A full end-to-end save requires network access to YouTube, a working `ffmpeg`, and installed requirements.
