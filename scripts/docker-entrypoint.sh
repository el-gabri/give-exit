#!/bin/sh
# Fresh named volumes and host bind mounts can come up owned by root, which the
# API user could not write: the model cache (HF_HOME) and, on Linux hosts, the
# embedding-generation directory. The API image therefore starts as root only
# long enough to fix that ownership, then drops to appuser (uid 10001) before
# running the real command. A read-only mount is left untouched. setpriv execs
# the command in place, so no root process stays behind as PID 1.
set -eu

if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data /models/huggingface
  chown -R appuser:appuser /models || true
  if [ -w /app/data ]; then
    chown appuser:appuser /app/data || true
    for path in /app/data/*; do
      [ -e "$path" ] || continue
      if [ -w "$path" ]; then
        chown -R appuser:appuser "$path" || true
      fi
    done
  fi
  exec setpriv --reuid=appuser --regid=appuser --init-groups -- "$@"
fi

exec "$@"
