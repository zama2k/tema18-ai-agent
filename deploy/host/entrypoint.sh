#!/usr/bin/env bash
# Container entrypoint: install the authorised key, start nginx and sshd.
set -euo pipefail

: "${AUTHORIZED_KEY:?AUTHORIZED_KEY (public key of the deploy user) is required}"
printf '%s\n' "$AUTHORIZED_KEY" >/home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

nginx
exec /usr/sbin/sshd -D -e
