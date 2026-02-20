#!/bin/bash
set -e

# Create config (same as web entrypoint)
mkdir -p /etc/birdnet
cp /app/dev/birdnet.dev.conf /etc/birdnet/birdnet.conf

source /etc/birdnet/birdnet.conf

if [[ "$MODEL" == "Perch_v2" ]]; then
  SAMPLERATE=32000
else
  SAMPLERATE=48000
fi

mkdir -p "$RECS_DIR/StreamData" "$RECS_DIR/Processed" "$RECS_DIR/Extracted/By_Date" "$RECS_DIR/Extracted/Charts"
mkdir -p "$HOME/BirdNET-Pi"
ln -sf /app/model "$HOME/BirdNET-Pi/model"
ln -sf /app/target_score_species_list.txt "$HOME/BirdNET-Pi/target_score_species_list.txt"
touch "$HOME/BirdNET-Pi/IdentifiedSoFar.txt"

# Feed test audio in a loop (no mic on this machine)
feed_test_audio() {
  local src="/app/tests/testdata/Pica pica_30s.wav"
  echo "=== Feeding test audio on loop (${RECORDING_LENGTH}s segments, ${SAMPLERATE}Hz) ==="
  while true; do
    local ts=$(date +%F-birdnet-%H:%M:%S)
    local out="${RECS_DIR}/StreamData/${ts}.wav"
    ffmpeg -hide_banner -loglevel error -y \
      -i "$src" -acodec pcm_s16le -ac 1 -ar ${SAMPLERATE} \
      -t ${RECORDING_LENGTH} "$out"
    echo "[feed] wrote $out"
    sleep ${RECORDING_LENGTH}
  done
}
feed_test_audio &

# Generate daily charts periodically in the background
generate_charts() {
  echo "=== Chart generator starting (every 60s) ==="
  sleep 30  # wait for some detections first
  while true; do
    MPLBACKEND=Agg python3 /app/scripts/daily_plot.py 2>&1 | sed 's/^/[charts] /' || true
    sleep 60
  done
}
generate_charts &

echo "=== Starting BirdNET analysis service ==="
echo "=== Score logging: TARGET_SCORE_LOGGING=$TARGET_SCORE_LOGGING ==="
cd /app/scripts
exec python3 birdnet_analysis.py
