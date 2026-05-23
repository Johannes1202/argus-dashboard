#!/usr/bin/env bash
# Deploy Argus to a remote server.
# Usage: REMOTE=user@your-server REMOTE_DIR=~/argus ./deploy.sh
set -e

REMOTE="${REMOTE:?Set REMOTE=user@your-server}"
REMOTE_DIR="${REMOTE_DIR:-~/argus}"

echo "Syncing files to $REMOTE:$REMOTE_DIR ..."
rsync -av --exclude='.env' --exclude='config.yml' --exclude='data/' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.git/' \
  ./ "$REMOTE:$REMOTE_DIR/"

echo "Rebuilding and restarting..."
# Always --build — code is baked into the image, not volume-mounted
ssh "$REMOTE" "cd $REMOTE_DIR && docker compose up -d --build"

echo "Done."
