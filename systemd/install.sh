#!/usr/bin/env bash
# Install Agentarium systemd units. Enables them but does NOT start them —
# the coordinator starts everything at assembly time.
set -euo pipefail

cd "$(dirname "$0")"

sudo cp agentarium.service \
        agentarium-logika7.service \
        agentarium-kacaubalau.service \
        agentarium-dataneng.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable agentarium.service \
                         agentarium-logika7.service \
                         agentarium-kacaubalau.service \
                         agentarium-dataneng.service

echo "Installed & enabled (not started — coordinator starts them at assembly)."
