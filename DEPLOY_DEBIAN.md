# Debian Headless Deployment Guide

This guide deploys the YouTube-to-Jellyfin music ingest app as a plain `uvicorn` service on a headless Debian server. It does not add Docker, Jellyfin API integration, a database, authentication, or a tunnel.

The app writes music files directly to the configured `MUSIC_ROOT` and uses `STAGING_DIR` only while a download is waiting to be saved.

## 1. Supported baseline

Recommended server baseline:

- Debian 12 (Bookworm), amd64 or arm64
- Python 3.11 or newer
- A normal Linux user dedicated to this app, for example `songdl`
- `ffmpeg` installed from Debian packages
- Network access to YouTube, MusicBrainz, and the configured local browser/client
- Enough disk space for the music library plus temporary staging downloads

Do not use the system Python environment for application packages. Use a project-local virtual environment.

The application does not require a browser on the Debian server. It invokes the `yt-dlp` executable found on `PATH`; if that executable is unavailable, it falls back to `python -m yt_dlp` inside the virtual environment.

## 2. Install operating-system packages

Log in over SSH as an account with `sudo` access:

```bash
ssh your-admin@your-server
```

Update package metadata and install Python, virtual-environment support, ffmpeg, and basic build/runtime tools:

```bash
sudo apt update
sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  ffmpeg \
  ca-certificates \
  curl \
  git
```

Verify the important versions:

```bash
python3 --version
python3 -m venv --help >/dev/null && echo "venv support: OK"
ffmpeg -version | head -n 1
```

Python must be 3.11 or newer. If Debian reports an older Python, install Python 3.11+ using your approved Debian repository or a separately managed Python installation. Do not replace `/usr/bin/python3` blindly.

Verify the codec support used by this app:

```bash
ffmpeg -hide_banner -encoders | grep -E 'aac|alac' || true
```

The downloader obtains the best available audio and converts it to AAC in an `.m4a` container.

## 3. Create the service user and directories

Create a system user with no interactive login. Choose a storage location with enough capacity. The example below uses `/srv/song-downloader` for the application and `/srv/media/music` for the library.

```bash
sudo useradd \
  --system \
  --home-dir /srv/song-downloader \
  --create-home \
  --shell /usr/sbin/nologin \
  songdl

sudo mkdir -p /srv/song-downloader
sudo mkdir -p /srv/media/music/structured-music
sudo mkdir -p /srv/media/music/staging
sudo chown -R songdl:songdl /srv/song-downloader /srv/media/music
sudo chmod 750 /srv/song-downloader /srv/media/music
```

The service user needs:

- Read/write permission on the application virtual environment and application directory.
- Read/write/execute permission on `MUSIC_ROOT`.
- Read/write/execute permission on `STAGING_DIR`.

If Jellyfin reads the same music tree, grant Jellyfin read access without granting it write access. One common approach is a shared group:

```bash
sudo groupadd --system media-readers 2>/dev/null || true
sudo usermod -a -G media-readers jellyfin
sudo usermod -a -G media-readers songdl
sudo chown -R songdl:media-readers /srv/media/music/structured-music
sudo find /srv/media/music/structured-music -type d -exec chmod 2750 {} \;
sudo find /srv/media/music/structured-music -type f -exec chmod 0640 {} \;
```

Restart Jellyfin or start a new login/session after changing group membership.

## 4. Install the application

Copy the repository to the server. From your development machine, one possible command is:

```bash
rsync -av --exclude '.venv' --exclude 'dev-music' --exclude '__pycache__' \
  ./song-downloader/ your-admin@your-server:/tmp/song-downloader/
```

Then, on the server:

```bash
sudo rsync -a /tmp/song-downloader/ /srv/song-downloader/
sudo chown -R songdl:songdl /srv/song-downloader
sudo -u songdl bash -c 'cd /srv/song-downloader && python3 -m venv .venv'
```

Activate the virtual environment for installation:

```bash
sudo -u songdl /srv/song-downloader/.venv/bin/python -m pip install --upgrade pip setuptools wheel
sudo -u songdl /srv/song-downloader/.venv/bin/python -m pip install -r /srv/song-downloader/requirements.txt
```

Install and verify the command-line yt-dlp executable inside the same environment:

```bash
sudo -u songdl /srv/song-downloader/.venv/bin/python -m pip install --upgrade yt-dlp
sudo -u songdl /srv/song-downloader/.venv/bin/yt-dlp --version
```

The current development machine was tested with these package versions. They are reference versions, not a promise that YouTube will behave identically forever:

| Package | Tested version |
| --- | ---: |
| `fastapi` | 0.128.8 |
| `uvicorn` | 0.39.0 |
| `python-multipart` | 0.0.20 |
| `yt-dlp` | 2026.08.19 CLI; the older Python package may differ if installed from an old environment |
| `mutagen` | 1.47.0 |
| `musicbrainzngs` | 0.7.1 |
| `python-dotenv` | 1.2.1 |

For a reproducible production installation, record the exact versions installed on the server after a successful deployment:

```bash
sudo -u songdl /srv/song-downloader/.venv/bin/python -m pip freeze \
  | sudo tee /srv/song-downloader/requirements.lock.txt >/dev/null
sudo chown songdl:songdl /srv/song-downloader/requirements.lock.txt
```

Do not blindly upgrade all packages during normal operation. Upgrade yt-dlp deliberately because YouTube extractor compatibility changes frequently, then rerun the smoke tests below.

## 5. Configure `.env`

Create the production environment file:

```bash
sudo -u songdl cp /srv/song-downloader/.env.example /srv/song-downloader/.env
sudo -u songdl nano /srv/song-downloader/.env
```

Use absolute paths in production:

```dotenv
MUSIC_ROOT=/srv/media/music/structured-music
STAGING_DIR=/srv/media/music/staging
PORT=8099
```

The app resolves relative paths relative to the directory containing `app.py`, but absolute paths are clearer and avoid surprises under systemd.

Secure the file:

```bash
sudo chown songdl:songdl /srv/song-downloader/.env
sudo chmod 600 /srv/song-downloader/.env
```

There is no browser-cookie setting in the current application. The app uses the `yt-dlp` executable on `PATH` and does not require a browser on the headless server.

## 6. Preflight checks as the service user

Run the checks as `songdl`, because a command that works as your administrator may fail due to permissions or a different `PATH`:

```bash
sudo -u songdl bash -lc '
  cd /srv/song-downloader
  .venv/bin/python -m py_compile app.py
  .venv/bin/python -c "import app; print(app.app.title)"
  .venv/bin/yt-dlp --version
  ffmpeg -version | head -n 1
  test -w /srv/media/music/structured-music
  test -w /srv/media/music/staging
'
```

Test the web app in the foreground before creating a service:

```bash
sudo -u songdl bash -lc \
  'cd /srv/song-downloader && .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8099'
```

From a second SSH session on the server:

```bash
curl -fsS http://127.0.0.1:8099/ | grep -o '<title>[^<]*'
curl -fsS http://127.0.0.1:8099/api/artists
```

Stop the foreground process with `Ctrl+C`. Do not use `Ctrl+Z`; that suspends the process while it continues to hold the port.

## 7. Probe and download smoke test

Use the browser UI for the normal workflow, but first test the downloader directly as the service user:

```bash
sudo -u songdl bash -lc \
  'cd /srv/song-downloader && .venv/bin/yt-dlp \
    -f "bestaudio/best" \
    --extract-audio \
    --audio-format m4a \
    --no-playlist \
    --paths /srv/media/music/staging \
    "https://www.youtube.com/watch?v=F01UTYg79KY"'
```

This is only a connectivity/extractor check. Remove the test output after verifying it:

```bash
sudo -u songdl find /srv/media/music/staging -maxdepth 1 -type f -delete
```

For the application test, open the service through an SSH tunnel from your development computer:

```bash
ssh -N -L 8099:127.0.0.1:8099 your-admin@your-server
```

Then open `http://127.0.0.1:8099` locally. Probe a video, confirm the existing artist and album dropdowns, download, edit metadata, and save.

The app should produce this shape:

```text
/srv/media/music/structured-music/
└── Artist/
    ├── artist.nfo
    └── Album/
        ├── album.nfo
        ├── cover.jpg
        └── Song Title.m4a
```

The final filename is the song title only. Artist, album, year, genre, and track numbering are stored in the M4A tags. Staged audio, thumbnails, and metadata are removed after a successful save. A skipped duplicate remains staged so it can be reviewed or retried.

## 8. systemd service

Create a service unit:

```bash
sudo nano /etc/systemd/system/song-downloader.service
```

Use:

```ini
[Unit]
Description=YouTube to Jellyfin Music Ingest
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=songdl
Group=songdl
WorkingDirectory=/srv/song-downloader
EnvironmentFile=/srv/song-downloader/.env
ExecStart=/srv/song-downloader/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port ${PORT}
Restart=on-failure
RestartSec=5

# Keep the service from writing outside its configured application paths.
ReadWritePaths=/srv/song-downloader /srv/media/music
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Some systemd versions do not expand `${PORT}` inside `ExecStart`. If startup fails with an argument error, use the known deployment port directly:

```ini
ExecStart=/srv/song-downloader/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8099
```

The app still reads `PORT` from `.env`; the explicit service argument only controls uvicorn’s listener. Keep both values aligned.

Load and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now song-downloader.service
sudo systemctl status song-downloader.service --no-pager
```

Check the live endpoint:

```bash
curl -fsS http://127.0.0.1:8099/ | grep -o '<title>[^<]*'
```

View logs:

```bash
sudo journalctl -u song-downloader.service -f
```

Stop, start, or restart it:

```bash
sudo systemctl stop song-downloader.service
sudo systemctl start song-downloader.service
sudo systemctl restart song-downloader.service
```

After changing `.env` or the service unit, restart is required:

```bash
sudo systemctl daemon-reload
sudo systemctl restart song-downloader.service
```

## 9. Optional LAN access

The recommended default binds only to `127.0.0.1`, which means the app is reachable locally on the server or through an SSH tunnel.

For a trusted private LAN, change the service command to:

```ini
ExecStart=/srv/song-downloader/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8099
```

Then allow only the trusted network in the firewall. Example for a known private subnet:

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 8099 proto tcp
sudo ufw enable
sudo ufw status verbose
```

This application has no authentication. Do not expose it directly to the public internet. If remote access is required, place it behind an authenticated VPN or a properly configured reverse proxy and add authentication before exposing it beyond a trusted network.

## 10. Updating the application

Stop the service before replacing application files:

```bash
sudo systemctl stop song-downloader.service
sudo rsync -a --delete \
  --exclude '.env' \
  --exclude '.venv' \
  --exclude 'dev-music' \
  ./song-downloader/ your-admin@your-server:/tmp/song-downloader/
sudo rsync -a /tmp/song-downloader/ /srv/song-downloader/
sudo chown -R songdl:songdl /srv/song-downloader
```

Never use `--delete` against the music library. The application source tree and music tree must remain separate.

Update dependencies deliberately:

```bash
sudo -u songdl /srv/song-downloader/.venv/bin/python -m pip install -r /srv/song-downloader/requirements.txt
sudo -u songdl /srv/song-downloader/.venv/bin/python -m pip install --upgrade yt-dlp
sudo -u songdl /srv/song-downloader/.venv/bin/python -m py_compile /srv/song-downloader/app.py
sudo systemctl start song-downloader.service
sudo systemctl status song-downloader.service --no-pager
```

If a new yt-dlp version breaks extraction, install the previous known-good version from the lock file or pip history, then restart the service.

## 11. Backups and data safety

Back up the library, not just the application source:

```bash
sudo rsync -aHAX --delete \
  /srv/media/music/structured-music/ \
  /srv/backup/song-library/structured-music/
```

Back up the following separately if needed:

- `/srv/media/music/structured-music/` for finished audio and metadata.
- `/srv/song-downloader/.env` for deployment paths and port configuration.
- `/srv/song-downloader/requirements.lock.txt` for installed package versions.

Do not back up incomplete staging files as if they were finished library files. Staging can be cleared safely from the UI or with the API only after confirming no download is in progress.

Check disk usage regularly:

```bash
df -h /srv/media/music
du -sh /srv/media/music/staging /srv/media/music/structured-music
```

## 12. Troubleshooting

### Port already in use

Find the listener:

```bash
sudo ss -ltnp 'sport = :8099'
```

Or:

```bash
sudo lsof -nP -iTCP:8099 -sTCP:LISTEN
```

Stop the old service/process, or choose another port in `.env` and the systemd unit. Use `Ctrl+C` for a foreground uvicorn process; `Ctrl+Z` suspends it and leaves the port occupied.

### `yt-dlp: command not found`

The app first searches `PATH`, then falls back to the virtual environment’s Python module. Verify both:

```bash
sudo -u songdl /srv/song-downloader/.venv/bin/yt-dlp --version
sudo -u songdl /srv/song-downloader/.venv/bin/python -m yt_dlp --version
```

If the CLI is installed elsewhere, add its directory to the systemd service `Environment=PATH=...` or install yt-dlp into the virtual environment.

### YouTube returns HTTP 403 or asks to reload

First update yt-dlp and restart the service:

```bash
sudo -u songdl /srv/song-downloader/.venv/bin/python -m pip install --upgrade yt-dlp
sudo systemctl restart song-downloader.service
```

The app intentionally does not depend on a browser or browser cookies. YouTube extractor behavior can change independently of this application, so a future yt-dlp release may be required.

### ffmpeg is missing

Check the executable available to the service user:

```bash
sudo -u songdl sh -c 'command -v ffmpeg && ffmpeg -version | head -n 1'
```

Install it with:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

### Permission denied while saving

Check ownership and write permissions:

```bash
namei -l /srv/media/music/structured-music
namei -l /srv/media/music/staging
sudo -u songdl test -w /srv/media/music/structured-music && echo writable
sudo -u songdl test -w /srv/media/music/staging && echo writable
```

Fix ownership only on the configured app/library paths:

```bash
sudo chown -R songdl:songdl /srv/media/music/staging
sudo chown -R songdl:media-readers /srv/media/music/structured-music
```

### The server starts but the browser cannot connect

If the service binds to `127.0.0.1`, use an SSH tunnel:

```bash
ssh -N -L 8099:127.0.0.1:8099 your-admin@your-server
```

Then browse to `http://127.0.0.1:8099` on your local computer. If binding to the LAN, check `ss`, firewall rules, and the server’s IP address.

### A duplicate file is skipped

This is intentional. The default conflict mode is `Skip`. The UI reports conflicts before/after saving. Select overwrite only when replacing that exact song is intentional. A skipped staged file is retained so it is not lost.

### A staged job is left behind

A staged job remains when saving fails or a duplicate is skipped. Review it first. The UI’s **Clear staging** action removes all staging jobs. To inspect them manually:

```bash
find /srv/media/music/staging -maxdepth 2 -type f -print
```

## 13. Operational checklist

After deployment, confirm:

- `python3 --version` is 3.11+.
- `.venv/bin/python -m pip install -r requirements.txt` succeeds.
- `.venv/bin/yt-dlp --version` works as `songdl`.
- `ffmpeg -version` works as `songdl`.
- `.env` uses absolute `MUSIC_ROOT`, `STAGING_DIR`, and the intended `PORT`.
- `systemctl status song-downloader` is active.
- `curl http://127.0.0.1:8099/` returns the app page.
- The artist dropdown lists existing library folders.
- Selecting an artist loads its album folders.
- A test download produces staged `.m4a`, `.jpg`, and metadata.
- Saving produces `Artist/Album/Song Title.m4a` with tags, cover art, and NFO files.
- Successful saves remove the corresponding staging job.
- Duplicate saves skip by default without deleting existing library files.
- The library backup job can read the configured music root.
