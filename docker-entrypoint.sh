#!/bin/sh
# Started as root (the default): make the data volume writable for the app user, then drop to it.
# PUID/PGID choose that user (default 1000:1000), e.g. to match the owner of ./data on the host.
# Started as another user (docker run --user / compose "user:"): run as that user, unchanged.
set -e
DATA=${KDS_DATA_DIR:-/data}
if [ "$(id -u)" = 0 ]; then
  PUID=${PUID:-1000}
  PGID=${PGID:-1000}
  mkdir -p "$DATA"
  if [ "$(stat -c %u:%g "$DATA")" != "$PUID:$PGID" ]; then
    echo "kindle-dash-server: giving $DATA to $PUID:$PGID"
    chown -R "$PUID:$PGID" "$DATA"
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
