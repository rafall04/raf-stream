#!/bin/sh
# Gerbang regresi DJI-STB — jalankan di STB:  sh /root/smoke_test.sh
# Tiap cek memetakan bug NYATA yang pernah ada (lihat AGENTS.md).
# Output: PASS/SKIP/FAIL per cek; exit code = jumlah FAIL.
# Dipakai: baseline -> ubah -> deploy -> smoke lagi. Merah = regresi.

F=0; say(){ printf "%-4s %s\n" "$1" "$2"; [ "$1" = FAIL ] && F=$((F+1)) || :; }
UDC=$(cat /sys/class/udc/*/state 2>/dev/null | head -1)

# Implementasi aktif per peran (rs menang selama berjalan ATAU enabled —
# pasca-boot marker tmpfs hilang dan rs bisa sedang start; enablement =
# intent persisten). Jurnal/is-active dicek terhadap unit yang hidup.
pick(){ { systemctl is-active "dji-$1-rs" >/dev/null 2>&1 || \
        systemctl is-enabled "dji-$1-rs" >/dev/null 2>&1; } \
    && echo "dji-$1-rs" || echo "dji-$1"; }
BRIDGE_SVC=$(pick bridge); STREAMER_SVC=$(pick streamer); MITM_SVC=$(pick mitm)
DISPLAY_SVC=$(pick display)
# Log display mengikuti impl aktif: rs menulis display-rs.log.
case "$DISPLAY_SVC" in *-rs) DISPLAY_LOG=/root/display-rs.log;;
                        *) DISPLAY_LOG=/root/display.log;; esac

# --- L1 UDC ---
if [ "$UDC" = configured ]; then say PASS "L1 UDC configured"
elif [ "$UDC" = "not attached" ]; then say SKIP "L1 RC tercabut (wajar)"
else say FAIL "L1 UDC state=$UDC"; fi

# --- L2 bridge listen + churn (bug: phantom restart 135s, n=14 rebind) ---
ss -tln 2>/dev/null | grep -q ':9910 ' && say PASS "L2 :9910 LISTEN" \
    || say FAIL "L2 :9910 hilang"
BR=$(journalctl -u "$BRIDGE_SVC" --since "-10 min" -o cat 2>/dev/null | grep -c "Started dji-bridge")
[ "$BR" -le 4 ] && say PASS "L2 bridge restart $BR/10m" \
    || say FAIL "L2 bridge churn $BR/10m (>4 = loop restart)"

# --- L3 streamer counters (bug: seq reset, verbatim dedup, UDC-wait churn) ---
if [ "$UDC" = configured ]; then
  # Stats streamer print tiap 15s — bandingkan DUA baris stats terakhir,
  # bukan "baris baru dalam 5s" (flake: ~2/3 window kosong di sistem sehat).
  # Nilai sama antar dua print = pipa mati >=15s; beda = mengalir.
  RX2=$(journalctl -u "$STREAMER_SVC" -o cat --since "-60s" | grep -oE "duml_rx=[0-9]+" | tail -2)
  if [ "$(printf '%s\n' "$RX2" | grep -c duml_rx)" -ge 2 ]; then
    L1X=$(printf '%s\n' "$RX2" | head -1); L2X=$(printf '%s\n' "$RX2" | tail -1)
    [ "$L1X" != "$L2X" ] && say PASS "L3 duml_rx maju $L1X->$L2X" \
      || say FAIL "L3 duml_rx beku ($L1X == $L2X antar dua stats) — rule-6 harusnya fire"
  else say SKIP "L3 duml_rx (<2 sampel stats dalam 60dtk)"; fi
else say SKIP "L3 counters (RC lepas)"; fi
# seq-persist (fix c102f6a): file dibuat di loop sesi -> code-level check
# selalu bisa diverifikasi; file-presence hanya bermakna saat sesi jalan.
grep -q "dji_stream_seq" /root/streamer.py 2>/dev/null \
    && say PASS "L3 seq/ctr persist ada di kode" \
    || say FAIL "L3 seq-persist hilang (regresi c102f6a)"
if [ "$UDC" = configured ]; then
  [ -f /tmp/dji_stream_seq ] && [ -f /tmp/dji_stream_ctr ] \
      && say PASS "L3 seq/ctr file ada" \
      || say FAIL "L3 seq file tak dibuat saat sesi"
fi
[ -f /root/sess_trace.jsonl ] && say PASS "L3 sess_trace ada" \
    || say FAIL "L3 sess_trace hilang (fix 66bce52 regresi)"
# (file bisa 0 byte saat RC lepas — replay belum jalan, itu SAH)

# reseq aktif di semua pass (verbatim-dedup fix 34ec75f) — cek level kode
grep -q "reseq_record(bytes.fromhex" /root/streamer.py 2>/dev/null \
    && say PASS "L3 reseq di semua pass" \
    || say FAIL "L3 replay verbatim kembali (regresi 34ec75f)"
# --- L4 :9990 clients + CLOSE-WAIT (bug: zombie reap, rule-2 churn) ---
if ss -tln 2>/dev/null | grep -q ':9990 '; then
  CW=$(ss -tn state close-wait '( sport = :9990 )' 2>/dev/null | tail -n +2 | wc -l)
  [ "$CW" -le 3 ] && say PASS "L4 :9990 LISTEN, CLOSE-WAIT=$CW" \
      || say FAIL "L4 CLOSE-WAIT=$CW menumpuk (>3)"
else say FAIL "L4 :9990 hilang"; fi

# --- L5 display (bug: photo timer, EOS prematur, wedge) ---
grep -q "show=" "$DISPLAY_LOG" 2>/dev/null && say PASS "L5 display stats ada" \
    || say FAIL "L5 $DISPLAY_LOG tanpa stats"
# L5b — dji-display-rs: denyut main-loop + fd log tak boleh ke inode
# terhapus (rotasi mv memutus fd append = diagnosa buta, terukur 2.5jam).
case "$DISPLAY_SVC" in *-rs)
    # Kesegaran via up= (monotonic) — mtime wall terkena langkah NTP.
    HB_UP=$(tr ' ' '\n' < /tmp/dji_display_hb 2>/dev/null \
            | sed -n 's/^up=\([0-9][0-9]*\)$/\1/p' | head -1)
    UP_NOW=$(cut -d. -f1 /proc/uptime)
    if [ -n "$HB_UP" ] && [ $(( ${UP_NOW:-0} - HB_UP )) -lt 20 ]; then
        say PASS "L5b heartbeat rs segar"
    else say FAIL "L5b /tmp/dji_display_hb basi/hilang"; fi
    DPID=$(pgrep -x dji-display-rs | head -1)
    if [ -n "$DPID" ] && readlink "/proc/$DPID/fd/1" 2>/dev/null | grep -q deleted; then
        say FAIL "L5b fd log ke inode terhapus"
    else say PASS "L5b fd log utuh"; fi;;
esac
PS=$(python3 -c "import json;print(json.load(open('/root/display_cfg.json')).get('photo_secs',5))" 2>/dev/null)
python3 - "$PS" "$DISPLAY_LOG" <<'PY' 2>/dev/null
import re,sys,datetime as d
ps=float(sys.argv[1] or 5)
try: L=open(sys.argv[2],errors="replace").readlines()[-400:]
except OSError: sys.exit(2)
ev=[]
for ln in L:
    m=re.search(r"(\d+:\d+:\d+).*loop #\d+ -> (\S+)",ln)
    if m: ev.append((m.group(1),m.group(2)))
foto=[(ev[i][0],ev[i+1][0]) for i in range(len(ev)-1) if ev[i][1].lower().endswith((".jpg",".jpeg",".png"))]
if not foto: print("SKIP L5 leg foto (tak ada di log)"); sys.exit(0)
def ts(s): h,m,ss=map(int,s.split(":")); return h*3600+m*60+ss
gap=ts(foto[-1][1])-ts(foto[-1][0])
lo,hi=ps-0.8,ps+4
print(f"{'PASS' if lo<=gap<=hi else 'FAIL'} L5 leg foto {gap}s (target {ps}s)")
PY

# --- Services ---
for u in "$BRIDGE_SVC" "$STREAMER_SVC" "$DISPLAY_SVC" dji-watchdog dji-mediamtx; do
  systemctl is-active -q "$u" && say PASS "svc $u" || say FAIL "svc $u TIDAK aktif"
done

echo "----"; [ $F -eq 0 ] && echo "SMOKE: SEMUA HIJAU" || echo "SMOKE: $F GAGAL"
exit $F
