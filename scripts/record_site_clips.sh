#!/usr/bin/env bash
# Record and encode the site's limits clips (dark/light, desktop 90x24 and mobile 60x30),
# the posters, and the README GIF. Run from the repository root; takes about 20 minutes.
# Afterwards run: python3 docs/site/check_site.py
set -euo pipefail
cd "$(dirname "$0")/.."
REC="${1:-$(mktemp -d)}"
mkdir -p "$REC"
A=docs/site/assets
for theme in dark light; do
  python3 scripts/bridge_demo.py --storyboard limits --theme "$theme" --out "$REC/d-$theme.gif" --frames-out "$REC/d-$theme.json"
  python3 scripts/bridge_demo.py --storyboard limits --theme "$theme" --size 60x30 --out "$REC/m-$theme.gif" --frames-out "$REC/m-$theme.json"
done
pids=()
for theme in dark light; do
  gif_d="$REC/enc-d-$theme.gif"
  [ "$theme" = dark ] && gif_d=docs/img/llm-bridge-demo.gif
  python3 scripts/bridge_demo.py --frames-in "$REC/d-$theme.json" --theme "$theme" --out "$gif_d" \
    --mp4 "$A/demo-limits-$theme.mp4" --webm "$A/demo-limits-$theme.webm" --poster "$A/demo-limits-$theme.webp" &
  pids+=($!)
  python3 scripts/bridge_demo.py --frames-in "$REC/m-$theme.json" --theme "$theme" --out "$REC/enc-m-$theme.gif" \
    --mp4 "$A/demo-limits-mobile-$theme.mp4" --webm "$A/demo-limits-mobile-$theme.webm" --poster "$A/demo-limits-mobile-$theme.webp" &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid"; done
echo "recorded captures kept in $REC"
