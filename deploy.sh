#!/usr/bin/env bash
# Deploy + install tumpukan DJI-STB (Python) ke HG680P via SSH — satu
# perintah: push stb/ -> install_services.sh -> smoke otomatis.
#
#   ./deploy.sh root@<stb-ip>            # push + install + smoke
#   SKIP_INSTALL=1 ./deploy.sh root@ip   # push saja
#   SKIP_SMOKE=1   ./deploy.sh root@ip   # tanpa gerbang regresi
set -e
TARGET="${1:?usage: ./deploy.sh <user@host> [dest-dir]}"
DEST="${2:-~/dji-stb}"

echo ">> pushing stb/ + pc/ to $TARGET:$DEST"
ssh "$TARGET" "mkdir -p $DEST"
scp -r "$(dirname "$0")/stb" "$(dirname "$0")/pc" "$TARGET:$DEST/"
ssh "$TARGET" "chmod +x $DEST/stb/*.sh $DEST/stb/bin/* \
    $DEST/stb/bridge.py $DEST/pc/session.py $DEST/pc/host_aoa_test.py \
    2>/dev/null; ls $DEST/stb | head -5"

if [ -z "$SKIP_INSTALL" ]; then
  # Di box yang menjalankan varian *-rs, installer py `enable --now`
  # unit Python -> Conflicts membunuh *-rs yang sedang hidup. Guard:
  # dji-display-rs berjalan = push file saja, install dilewati.
  if ssh "$TARGET" 'systemctl is-active dji-display-rs >/dev/null 2>&1'; then
    echo ">> dji-display-rs AKTIF di $TARGET — varian lain sedang berjalan."
    echo ">> Installer py dilewati (Conflicts akan membunuh display yang hidup)."
    echo ">> Paksa manual: ssh $TARGET 'cd $DEST/stb && bash install_services.sh'"
  else
    echo ">> install_services.sh di $TARGET"
    # Copy /root/ ikut segar hanya lewat installer — bridge+gadget jalan
    # in-place dari clone ($DEST/stb), sisanya dari copy /root/.
    ssh "$TARGET" "cd $DEST/stb && bash install_services.sh"
  fi
else
  echo ">> SKIP_INSTALL — jalankan manual:"
  echo "   ssh $TARGET 'cd $DEST/stb && bash install_services.sh'"
fi

# Gerbang regresi otomatis: deploy tanpa verifikasi = celah. Merah di
# sini = fix yang kembali mundur atau deploy yang tak lengkap.
if [ -z "$SKIP_SMOKE" ]; then
  echo ">> smoke test di $TARGET"
  ssh "$TARGET" 'sh /root/smoke_test.sh' || \
    echo ">> SMOKE MERAH — cek REGRESSIONS.md untuk invariant yang gagal"
fi
