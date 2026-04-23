#!/usr/bin/env bash
# Deploy argus-dashboard to CoreServices (192.168.0.102)
# Run from Argus: ~/argus-dashboard/deploy.sh
set -e

REMOTE="johan@192.168.0.102"
REMOTE_DIR="~/argus-dashboard"

echo "Syncing files..."
rsync -av --exclude='.env' --exclude='__pycache__' --exclude='*.pyc' \
  ~/argus-dashboard/ "$REMOTE:$REMOTE_DIR/"

echo "Rebuilding and restarting container..."
# Note: always use --build, not restart — code is baked into the image, not volume-mounted
ssh "$REMOTE" "cd $REMOTE_DIR && sudo docker compose up -d --build"

echo "Done."
