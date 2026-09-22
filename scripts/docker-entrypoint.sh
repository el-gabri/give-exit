#!/bin/sh
# Named volumes mount as root:root. The API image starts as root so this
# script can fix ownership, then drops to appuser (uid 10001).
# On the API service, embedding_generations is mounted read-only Ã¢â‚¬â€ never
# chown that tree (legal-index / indexer already wrote it as appuser).
set -eu

if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data /models/huggingface
  chown -R appuser:appuser /models || true
  if [ -w /app/data ]; then
    chown appuser:appuser /app/data || true
    for path in /app/data/*; do
      [ -e "$path" ] || continue
      case "$path" in
        */embedding_generations)
          if [ -w "$path" ]; then
            chown -R appuser:appuser "$path" || true
          fi
          ;;
        *)
          chown -R appuser:appuser "$path" || true
          ;;
      esac
    done
  fi
  exec runuser -u appuser -- "$@"
fi

exec "$@"
