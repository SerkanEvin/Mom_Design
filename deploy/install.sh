#!/usr/bin/env bash
# Install / update Mom_Design on the server (run as root, e.g. via aicp repair exec.shell).
#   SRC = File Bridge upload of this repository; DST = live folder used by docker compose.
set -euo pipefail
SRC=${SRC:-/srv/ai-files/projects/mom-design/src}
DST=${DST:-/Serkan/MomDesign}
OUT=/srv/ai-files/outgoing/mom-design

test -f "$SRC/Dockerfile" || { echo "source missing at $SRC"; exit 1; }
mkdir -p "$DST/data" "$DST/secrets" "$DST/src"
chmod 700 "$DST/secrets"
rm -rf "$DST/src.new" && cp -a "$SRC" "$DST/src.new" && rm -rf "$DST/src" && mv "$DST/src.new" "$DST/src"
cp "$DST/src/deploy/docker-compose.yml" "$DST/docker-compose.yml"
test -r /home/Seko-Claude/.config/aicp/providers/synthetic.key || { echo "synthetic key file missing"; exit 1; }

cd "$DST"
docker compose build --pull
docker compose up -d
for i in $(seq 1 30); do
  if docker exec mom-design python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" 2>/dev/null; then break; fi
  sleep 2
done
docker exec mom-design python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"

# First install only: create Serkan + Aslı and hand Serkan his login link through File Bridge.
if ! docker exec mom-design python -m studio.cli balance | grep -q '^serkan '; then
  mkdir -p "$OUT"
  docker exec mom-design python -m studio.cli init > "$DST/secrets/first_login_links.txt"
  chmod 600 "$DST/secrets/first_login_links.txt"
  grep '^serkan:' "$DST/secrets/first_login_links.txt" > "$OUT/serkan_giris_baglantisi.txt"
  chown --reference=/srv/ai-files/outgoing "$OUT" "$OUT/serkan_giris_baglantisi.txt"
  chmod 750 "$OUT"; chmod 640 "$OUT/serkan_giris_baglantisi.txt"
  echo "users created; Serkan's link written to $OUT/serkan_giris_baglantisi.txt (value not printed)"
fi
docker exec mom-design /opt/blender/blender -b --factory-startup --version | head -1
echo "done"
