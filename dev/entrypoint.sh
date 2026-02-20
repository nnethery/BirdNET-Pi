#!/bin/bash
set -e

# Set up birdnet.conf from shared dev config
mkdir -p /etc/birdnet
cp /app/dev/birdnet.dev.conf /etc/birdnet/birdnet.conf
chmod 666 /etc/birdnet/birdnet.conf

# Set up directory structure mimicking the Pi
SRC=/app
EXTRACTED=/srv/extracted
HOME_DIR=/home/birdnet

mkdir -p $EXTRACTED
mkdir -p $HOME_DIR/BirdSongs/Processed $HOME_DIR/BirdSongs/Extracted/By_Date $HOME_DIR/BirdSongs/Extracted/Charts $HOME_DIR/BirdSongs/StreamData
mkdir -p $HOME_DIR/BirdNET-Pi

# Symlink shared volume dirs so Caddy can serve charts and extracted audio
ln -sf $HOME_DIR/BirdSongs/Extracted/By_Date $EXTRACTED/By_Date
ln -sf $HOME_DIR/BirdSongs/Extracted/Charts $EXTRACTED/Charts

# Symlinks to make PHP pages work (mirrors install_services.sh)
ln -sf $SRC/scripts $EXTRACTED/scripts
ln -sf $SRC/homepage/views.php $EXTRACTED/views.php
ln -sf $SRC/homepage/style.css $EXTRACTED/style.css
ln -sf $SRC/homepage/static $EXTRACTED/static
ln -sf $SRC/homepage/images $EXTRACTED/images
ln -sf $SRC/homepage/index.php $EXTRACTED/index.php
# PHP view pages that views.php includes bare (without scripts/ prefix)
for php in overview.php play.php spectrogram.php stats.php \
           todays_detections.php history.php weekly_report.php; do
  ln -sf $SRC/scripts/$php $EXTRACTED/$php
done
ln -sf $SRC/model $HOME_DIR/BirdNET-Pi/model

# Species list symlinks (production uses install_services.sh for these)
for f in include_species_list.txt exclude_species_list.txt whitelist_species_list.txt \
         confirmed_species_list.txt target_score_species_list.txt; do
  touch $SRC/$f 2>/dev/null || true
  ln -sf $SRC/$f $SRC/scripts/$f
done
# Generate model/labels.txt (normally done by set_label_file() at runtime)
if [ ! -f $SRC/model/labels.txt ]; then
  source /etc/birdnet/birdnet.conf
  python3 -c "
import sys, json, os
model = os.environ.get('MODEL', '$MODEL')
base = '$SRC/model'
with open(f'{base}/{model}_Labels.txt') as f:
    labels = [l.strip() for l in f if l.strip()]
lang_file = f'{base}/l18n/labels_en_perch.json' if model == 'Perch_v2' else f'{base}/l18n/labels_en.json'
with open(lang_file) as f:
    lang = json.load(f)
with open(f'{base}/labels.txt', 'w') as f:
    for l in labels:
        f.write(f'{l}_{lang.get(l, l)}\n')
print(f'Generated labels.txt with {len(labels)} entries for {model}')
" 2>&1 || echo "Warning: could not generate labels.txt"
fi
ln -sf $SRC/model/labels.txt $SRC/scripts/labels.txt

# Stub timedatectl (common.php calls it for timezone)
cat > /usr/local/bin/timedatectl << 'STUB'
#!/bin/sh
echo "UTC"
STUB
chmod +x /usr/local/bin/timedatectl

# Create empty birds.db if it doesn't exist
if [ ! -f $SRC/scripts/birds.db ]; then
    sqlite3 $SRC/scripts/birds.db << 'SQL'
CREATE TABLE IF NOT EXISTS detections (
  Date DATE, Time TIME,
  Sci_Name VARCHAR(100) NOT NULL, Com_Name VARCHAR(100) NOT NULL,
  Confidence FLOAT, Lat FLOAT, Lon FLOAT, Cutoff FLOAT,
  Week INT, Sens FLOAT, Overlap FLOAT, File_Name VARCHAR(100) NOT NULL);
CREATE INDEX IF NOT EXISTS "detections_Com_Name" ON "detections" ("Com_Name");
CREATE INDEX IF NOT EXISTS "detections_Sci_Name" ON "detections" ("Sci_Name");
CREATE INDEX IF NOT EXISTS "detections_Date_Time" ON "detections" ("Date" DESC, "Time" DESC);
SQL
fi
chmod 666 $SRC/scripts/birds.db

# Configure PHP-FPM to run as root (dev only) so it can read bind-mounted files
cat > /usr/local/etc/php-fpm.d/zz-docker.conf << 'FPM'
[global]
daemonize = no

[www]
listen = /var/run/php-fpm.sock
listen.mode = 0666
user = root
group = root
chdir = /srv/extracted
FPM

# Set include_path so scripts can find common.php relative to doc root
echo 'include_path=".:/srv/extracted:/app/homepage:/usr/local/lib/php"' > /usr/local/etc/php/conf.d/dev-paths.ini

# Start PHP-FPM in background (-R allows root, dev only)
php-fpm -R &

# Start Caddy in foreground
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
