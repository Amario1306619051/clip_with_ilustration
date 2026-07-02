#!/usr/bin/env bash
# Deploy the ILLUSTRATOR render worker to the GPU boxes — mirrors how clipper-render
# is set up, but on its OWN port (8871) and its OWN service (illustrator-render).
#
# Run this FROM a machine that has SSH access to the boxes (the dev laptop's key
# wasn't authorized, so this can't run from there). It will, on each box:
#   1. rsync the illustrator backend + .env (needs PEXELS_API_KEY — the box
#      downloads the picked Pexels images itself).
#   2. create a small venv + install the render-worker deps (no whisper/torch/yt-dlp;
#      the box only renders).
#   3. install + start a systemd unit `illustrator-render` running uvicorn on :8871.
# Then it prints the ILLUSTRATOR_RENDER_REMOTE_URLS line to paste into the MAIN
# app's illustrator/.env (and restart the main app).
#
# Prereqs on each box: ffmpeg + ffprobe on PATH, python3, and your SSH key authorized.
set -euo pipefail

# ── config ───────────────────────────────────────────────────────────────
BOX_USER="${BOX_USER:-rnd}"
PORT="${PORT:-8871}"
REMOTE_DIR="${REMOTE_DIR:-/opt/illustrator-render}"
BOXES=(
  10.17.103.17
  10.17.103.28
  10.17.103.186
  10.17.103.164
  10.17.103.206
)
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -f "$HERE/.env" ]]; then
  echo "!! $HERE/.env not found — the box needs PEXELS_API_KEY in it. Aborting." >&2
  exit 1
fi

deps="fastapi 'uvicorn[standard]' requests pydantic python-multipart"

for box in "${BOXES[@]}"; do
  host="$BOX_USER@$box"
  echo "==== $box ===="

  # 1. code + env (only what the worker needs; assets/ holds the caption fonts)
  ssh "$host" "mkdir -p $REMOTE_DIR/backend $REMOTE_DIR/assets $REMOTE_DIR/temp $REMOTE_DIR/output"
  rsync -az --delete "$HERE/backend/" "$host:$REMOTE_DIR/backend/"
  rsync -az "$HERE/assets/" "$host:$REMOTE_DIR/assets/" 2>/dev/null || true
  rsync -az "$HERE/.env"    "$host:$REMOTE_DIR/.env"

  # 2. venv + deps
  ssh "$host" "cd $REMOTE_DIR && (python3 -m venv venv 2>/dev/null || true) && \
               ./venv/bin/pip -q install --upgrade pip && \
               ./venv/bin/pip -q install $deps"

  # 3. systemd unit
  ssh "$host" "sudo tee /etc/systemd/system/illustrator-render.service >/dev/null" <<UNIT
[Unit]
Description=Illustrator render worker (GPU box)
After=network-online.target

[Service]
User=$BOX_USER
WorkingDirectory=$REMOTE_DIR/backend
ExecStart=$REMOTE_DIR/venv/bin/uvicorn render_service:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

  ssh "$host" "sudo systemctl daemon-reload && \
               sudo systemctl enable --now illustrator-render && \
               sleep 1 && curl -s http://127.0.0.1:$PORT/health && echo"
done

echo
echo "==== ALL BOXES DONE — paste this into illustrator/.env on the MAIN app, then restart it ===="
printf 'ILLUSTRATOR_RENDER_REMOTE_URLS='
printf '%s' "http://${BOXES[0]}:$PORT"
for box in "${BOXES[@]:1}"; do printf ',%s' "http://$box:$PORT"; done
echo
