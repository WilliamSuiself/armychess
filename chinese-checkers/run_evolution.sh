#!/bin/bash
# Nightly self-evolution run, invoked by evolve-checkers.timer. Restarts the
# live service only if a new champion was actually promoted this run, so a
# no-op night doesn't bounce the production process for nothing.
set -euo pipefail
cd "$(dirname "$0")"

CHAMPION_FILE="weights_champion.json"
before=""
[ -f "$CHAMPION_FILE" ] && before=$(stat -c %Y "$CHAMPION_FILE" 2>/dev/null || stat -f %m "$CHAMPION_FILE")

python3 self_evolve.py --generations "${EVOLVE_GENERATIONS:-8}" \
                        --candidates "${EVOLVE_CANDIDATES:-5}" \
                        --games "${EVOLVE_GAMES:-24}"

after=""
[ -f "$CHAMPION_FILE" ] && after=$(stat -c %Y "$CHAMPION_FILE" 2>/dev/null || stat -f %m "$CHAMPION_FILE")

if [ -n "$after" ] && [ "$after" != "$before" ]; then
    echo "[run_evolution] champion promoted this run — reloading live service"
    systemctl restart chinese-checkers || echo "[run_evolution] restart failed (not running as root / not systemd?)"
else
    echo "[run_evolution] no promotion this run — service left untouched"
fi
