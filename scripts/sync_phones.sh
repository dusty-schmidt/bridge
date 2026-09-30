#!/bin/sh
# Pull the latest main into each phone's ~/bridge clone over Tailscale SSH.
# Best-effort: an offline phone, or one where Termux/sshd isn't running,
# is skipped with a warning rather than failing the whole run.
set -u

phones="p10 p8"
[ $# -gt 0 ] && phones="$*"

status=0
for host in $phones; do
  if ssh -o ConnectTimeout=4 -o BatchMode=yes "$host" \
      'cd ~/bridge && git pull --ff-only' 2>&1; then
    echo "$host: synced"
  else
    echo "$host: skipped (unreachable or no clone yet)" >&2
    status=1
  fi
done
exit $status
