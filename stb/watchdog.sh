#!/bin/sh
# dji-watchdog — самовосстановление всего стека. Каждые ~8 с проверяет
# известные режимы отказа и чинит без участия человека:
#   1. EP0-deadlock aoa-worker  (dmesg "already queued" + rc>phone=0) -> restart bridge
#   2. зомби-сокеты :9910      (несколько CLOSE-WAIT на порту)         -> restart bridge
#   3. мёртвый USB-1           (нет ни одного устройства >45 с)       -> rebind xhci
#   4. mitm/mpv мертвы         (нет процесса)                          -> перезапуск
#   5. UDC not attached        (RC воткнут, гаджет отвалился)          -> bounce gadget
# Пишет в /root/dji-watchdog.log — BUKAN /var/log: cron armbian-
# truncate-logs menghapus isi /var/log tiap 15 mnt (terukur: log
# selalu 0 byte, diagnosa restart-churn buta). state di /tmp/wdg_*.
LOG=/root/dji-watchdog.log
say() { echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }
UDC_STATE=/sys/class/udc/c9100000.usb/state

no_dev_since=0
slow_tick=0
while :; do
    sleep 8

    # Implementasi aktif per peran: unit -rs menang selama berjalan.
    # Semua restart/pgrep/journal di bawah menargetkan
    # impl yang hidup — tanpa ini watchdog me-restart unit Python yang
    # sudah di-stop Conflicts= dan membunuh data plane lain.
    # /tmp/dji_impl_mode (ditulis switch-rs/switch-py) = intent otoritatif:
    # sampling is-active saja salah-kunci saat rs restart sejenak —
    # restart Python lalu membunuh rs via Conflicts= (flip permanen).
    # mitm tetap is-active: switcher tidak memilih impl mitm.
    IMPL_MODE=$(cat /tmp/dji_impl_mode 2>/dev/null)
    BRIDGE_SVC=dji-bridge;     BRIDGE_PAT='bridge.py'
    STREAMER_SVC=dji-streamer; STREAMER_PAT='streamer.py'
    MITM_SVC=dji-mitm;         MITM_PAT='mitm.py'
    DISPLAY_SVC=dji-display;   DISPLAY_PAT='run_display.sh'
    if [ "$IMPL_MODE" = rs ]; then
        BRIDGE_SVC=dji-bridge-rs;     BRIDGE_PAT='dji-bridge-rs'
        STREAMER_SVC=dji-streamer-rs; STREAMER_PAT='dji-streamer-rs'
        DISPLAY_SVC=dji-display-rs;   DISPLAY_PAT='dji-display-rs'
    elif [ "$IMPL_MODE" != py ]; then
        # Marker hilang = pasca-boot (tmpfs): intent terakhir tersimpan
        # di enablement, BUKAN is-active — rs yang sedang start/restart
        # membaca inactive dan jatuh ke fallback py permanen (terukur:
        # Python menang Conflicts, rs tak pernah bangkit).
        for pair in "dji-bridge-rs:BRIDGE" "dji-streamer-rs:STREAMER" \
                    "dji-display-rs:DISPLAY"; do
            svc=${pair%%:*}; var=${pair##*:}
            if systemctl is-active "$svc" >/dev/null 2>&1 || \
               systemctl is-enabled "$svc" >/dev/null 2>&1; then
                eval "${var}_SVC=$svc; ${var}_PAT=$svc"
            fi
        done
    fi
    # mitm: hormati mode saat diset eksplisit; kalau tak ada file, impl
    # yang sedang jalan menang (switcher tak memilih impl mitm).
    if [ "$IMPL_MODE" = rs ]; then
        MITM_SVC=dji-mitm-rs; MITM_PAT='dji-mitm-rs'
    elif [ "$IMPL_MODE" != py ]; then
        { systemctl is-active dji-mitm-rs >/dev/null 2>&1 || \
          systemctl is-enabled dji-mitm-rs >/dev/null 2>&1; } && {
            MITM_SVC=dji-mitm-rs; MITM_PAT='dji-mitm-rs'; }
    fi

    # --- 1. EP0 deadlock: свежие "already queued" в dmesg -------------
    if dmesg | tail -30 | grep -q "already queued"; then
        say "EP0 deadlock detected -> restart dji-bridge"
        systemctl restart $BRIDGE_SVC
        dmesg -c > /dev/null 2>&1   # сбросить кольцо, чтобы не триггерилось повторно
        sleep 5
        continue
    fi

    # --- 1b. reset-storm: хост в цикле чтения дескрипторов -----------
    # Клин EP0-worker'а: RC шлёт reset каждые ~1.6 с, так и не дойдя до
    # configured. CONNECT-спам держит bridge.log «свежим», поэтому
    # правило 5 шторм не видит. Детектор — частые "new device" в dmesg.
    st=$(cat "$UDC_STATE" 2>/dev/null)
    # Histeresis 3 tick (~24dtk storm nonstop): RC baru colok me-retry
    # enumerate ~9x/dtk — storm startup itu SAH dan restart di tengahnya
    # membunuh EP0 mid-flight = loop abadi (terjadi 2026-11-10: bridge
    # restart 2x/20dtk, RC tak pernah selesai enum). Bridge muda (<60dtk)
    # juga dikecualikan — sisa storm sesi lalu bisa memenuhi buffer dmesg.
    bridge_epoch=$(systemctl show $BRIDGE_SVC -p ActiveEnterTimestamp --value)
    bridge_age=$(( $(date +%s) - $(date -d "$bridge_epoch" +%s 2>/dev/null || echo 0) ))
    if [ "$st" != "configured" ] && [ "$bridge_age" -ge 60 ] && \
       [ "$(dmesg | tail -60 | grep -c 'new device is')" -ge 10 ]; then
        sn=$(( $(cat /tmp/wdg_storm_n 2>/dev/null || echo 0) + 1 ))
        echo $sn > /tmp/wdg_storm_n
        if [ "$sn" -ge 3 ]; then
            say "reset storm (${st:-?}) >24dtk -> restart dji-bridge"
            systemctl restart $BRIDGE_SVC
            dmesg -c > /dev/null 2>&1
            rm -f /tmp/wdg_storm_n
            sleep 5
            continue
        fi
    else
        rm -f /tmp/wdg_storm_n
    fi

    # --- 2. зомби-клиенты на :9910 ------------------------------------
    # CLOSE-WAIT satu-tick = transient sah: tiap streamer exit (bail 15s
    # di link hening -> systemd respawn) meninggalkan satu selama <8dtk —
    # restart instan di sini membunuh handshake mid-flight = "miss" ke-3.
    # Zombie asli (bridge tak pernah close) bertahan selamanya -> tuntut
    # 3 tick beruntun (~24dtk) sebelum bertindak.
    zw=$(ss -tn state close-wait '( sport = :9910 )' | tail -n +2 | wc -l)
    if [ "$zw" -ge 1 ]; then
        zwn=$(( $(cat /tmp/wdg_zw_n 2>/dev/null || echo 0) + 1 ))
        echo $zwn > /tmp/wdg_zw_n
        if [ "$zwn" -ge 3 ]; then
            say "$zw zombie client(s) on :9910 >24s -> restart dji-bridge"
            systemctl restart $BRIDGE_SVC
            rm -f /tmp/wdg_zw_n
            sleep 5
            continue
        fi
    else
        rm -f /tmp/wdg_zw_n
    fi

    # --- 3. USB-1 пуст (только режим mitm — телефон живёт на USB-1) ---
    # Rebind dibatasi 3x beruntun lalu cooldown 10 mnt: rebind tiap ~52s
    # itu STORM — mencolok HP tepat saat rebind = enumerasi mati
    # (terjadi 2026-11-09: hp tak pernah dikenali mitm).
    if { systemctl is-enabled dji-mitm >/dev/null 2>&1 || [ "$MITM_SVC" = dji-mitm-rs ]; } && [ "$(lsusb | grep -v 'root hub\|1d6b' | wc -l)" -eq 0 ]; then
        no_dev_since=$((no_dev_since + 8))
        rebind_n=$(( $(cat /tmp/wdg_rebind_n 2>/dev/null || echo 0) ))
        rebind_at=$(( $(cat /tmp/wdg_rebind_at 2>/dev/null || echo 0) ))
        now_s=$(date +%s)
        if [ "$no_dev_since" -ge 45 ]; then
            # Cooldown branch juga me-refresh rebind_at -> rebind tiap
            # ~10mnt SELAMANYA (terukur n=14 saat USB-1 kosong). Cap 6:
            # port yang tak pulih setelah 6 rebind tak akan pulih oleh
            # rebind ke-7; counter ter-reset saat device tampak -> plug
            # berikutnya tetap diproteksi penuh.
            if [ "$rebind_n" -lt 3 ] || \
               { [ "$rebind_n" -lt 6 ] && [ $((now_s - rebind_at)) -ge 600 ]; }; then
                say "USB-1 empty ${no_dev_since}s -> rebind xhci-hcd (n=$((rebind_n+1)))"
                echo xhci-hcd.2.auto > /sys/bus/platform/drivers/xhci-hcd/unbind
                sleep 1
                echo xhci-hcd.2.auto > /sys/bus/platform/drivers/xhci-hcd/bind
                echo on > /sys/bus/usb/devices/usb1/power/control 2>/dev/null
                echo $((rebind_n + 1)) > /tmp/wdg_rebind_n
                echo $now_s > /tmp/wdg_rebind_at
            fi
            no_dev_since=0
        fi
    else
        no_dev_since=0
        rm -f /tmp/wdg_rebind_n /tmp/wdg_rebind_at
    fi

    # --- 4. процессы (systemd Restart уже поднимает — это страховка) ---
    if [ "$MITM_SVC" = dji-mitm-rs ] || \
       systemctl is-enabled dji-mitm >/dev/null 2>&1; then
        pgrep -f "$MITM_PAT" > /dev/null || {
            say "mitm($MITM_SVC) dead -> restart"
            systemctl restart "$MITM_SVC"
        }
    fi
    if [ "$STREAMER_SVC" = dji-streamer-rs ] || \
       systemctl is-enabled dji-streamer >/dev/null 2>&1; then
        pgrep -f "$STREAMER_PAT" > /dev/null || {
            say "streamer($STREAMER_SVC) dead -> restart"
            systemctl restart "$STREAMER_SVC"
        }
    fi
    # mediamtx mati = RTSP hilang diam-diam — Restart= sudah ada tapi
    # insurance ini konsisten dengan unit lain.
    if systemctl is-enabled dji-mediamtx >/dev/null 2>&1; then
        pgrep -x mediamtx > /dev/null || {
            say "mediamtx dead -> restart"
            systemctl restart dji-mediamtx
        }
    fi
    if systemctl is-enabled dji-streamer-min >/dev/null 2>&1; then
        pgrep -f 'streamer_min.py' > /dev/null || {
            say "streamer-min dead -> restart"
            systemctl restart dji-streamer-min
        }
    fi
    pgrep -f "$DISPLAY_PAT" > /dev/null || {
        say "display dead -> restart $DISPLAY_SVC"
        systemctl restart $DISPLAY_SVC
    }
    # Denyut main-loop dji-display-rs: pgrep menangkap proses MATI tapi
    # tidak main-loop wedged — proses "hidup" dengan layar beku (kelas
    # insiden stuck-foto). fast_tick menulis /tmp/dji_display_hb tiap
    # detik. Kesegaran dibaca dari `up=` (detik monotonic, ~=
    # /proc/uptime): mtime wall terkena langkah NTP/RTC saat boot —
    # file baru tampak basi dan heartbeat wall-clock internal pernah
    # membunuh proses sehat (387052ms palsu, PID 656).
    # Thread heartbeat internal tetap lapis pertama — ini belt kedua.
    if [ "$DISPLAY_SVC" = "dji-display-rs" ]; then
        hb_pid=$(pgrep -x dji-display-rs | head -1)
        if [ -n "$hb_pid" ]; then
            hb_up=$(stat -c%Y "/proc/$hb_pid" 2>/dev/null)
            hb_up=$(( $(date +%s) - ${hb_up:-$(date +%s)} ))
            hb_age=999
            hb_file_up=$(tr ' ' '\n' < /tmp/dji_display_hb 2>/dev/null \
                         | sed -n 's/^up=\([0-9][0-9]*\)$/\1/p' | head -1)
            if [ -n "$hb_file_up" ]; then
                up_now=$(cut -d. -f1 /proc/uptime)
                hb_age=$(( ${up_now:-0} - hb_file_up ))
            elif find /tmp/dji_display_hb -newermt -20seconds 2>/dev/null | grep -q .; then
                hb_age=0   # binary lama tanpa up= — fallback mtime
            fi
            if [ "$hb_up" -gt 30 ] && [ "$hb_age" -gt 20 ]; then
                say "display-rs heartbeat basi/hilang (${hb_age}dtk) -> restart (wedge main-loop)"
                systemctl restart dji-display-rs
            fi
        fi
    fi
    # Catatan arsitektur display: plane 37 (fbcon) DIMATIKAN permanen oleh
    # plane37_off.py — zpos-nya selalu di atas plane 40, dan splash/status
    # kini digambar oleh input-selector pipeline di plane 40 itu sendiri.
    # Stall/recovering ditangani display.py secara internal (switch pad);
    # jangan menambah rule rchar-stall di sini — socket sah diam saat
    # fase menunggu IDR -> false-positive restart-loop (sudah terjadi).


    # --- 4c. sumber daya, tiap ~2 menit: CMA + termal ------------------
    # CMA mengecil terus = kebocoran buffer decoder per-restart — gejala
    # awalnya ENOSPC/alloc-fail misterius. Termal: STB tertutup, throttle
    # panas bisa membunuh decode lebih dulu daripada OOM.
    slow_tick=$((slow_tick + 1))
    if [ "$slow_tick" -ge 15 ]; then
        slow_tick=0
        cma=$(awk '/CmaFree/{print $2}' /proc/meminfo)
        if [ "${cma:-999999}" -lt 32768 ]; then
            say "CmaFree=${cma}KB kritis -> restart $DISPLAY_SVC"
            systemctl restart $DISPLAY_SVC
        fi
        for z in /sys/class/thermal/thermal_zone*/temp; do
            t=$(cat "$z" 2>/dev/null) || continue
            if [ "${t:-0}" -ge 80000 ]; then
                say "thermal ${z##*zone}: $((t/1000))C — throttling risk"
            fi
        done
        # rotasi display.log — eMMC bukan tempat log tak berujung.
        # JANGAN mv: display memegang fd append ke inode lama — file ter-
        # unlink dan SEMUA log selanjutnya hilang diam-diam (terukur:
        # layar "macet" padahal pipeline sehat, 2.5 jam diagnosa buta).
        # Tulis ke inode yang sama: O_APPEND mengikuti EOF baru.
        if [ "$(stat -c%s /root/display.log 2>/dev/null || echo 0)" -gt 524288 ]; then
            tail -c 262144 /root/display.log > /tmp/d.log && \
                cat /tmp/d.log > /root/display.log && rm -f /tmp/d.log
            say "display.log dirotasi (512KB)"
        fi
        if [ "$(stat -c%s /root/display-rs.log 2>/dev/null || echo 0)" -gt 524288 ]; then
            tail -c 262144 /root/display-rs.log > /tmp/dr.log && \
                cat /tmp/dr.log > /root/display-rs.log && rm -f /tmp/dr.log
            say "display-rs.log dirotasi (512KB)"
        fi
        # mitm_capture.jsonl juga tumbuh tak berujung saat mitm aktif —
        # disk root 6GB pernah 92% penuh. Potong di batas 64MB (sisa 32MB).
        if [ "$(stat -c%s /root/mitm_capture.jsonl 2>/dev/null || echo 0)" -gt 67108864 ]; then
            # JANGAN mv: mitm memegang fd append ke inode lama — file ter-
            # unlink terus tumbuh tak terlihat du. Tulis ke inode yang sama:
            # O_APPEND mengikuti EOF baru, tak ada hole.
            tail -c 33554432 /root/mitm_capture.jsonl > /tmp/m.jsonl && \
                cat /tmp/m.jsonl > /root/mitm_capture.jsonl && rm -f /tmp/m.jsonl
            say "mitm_capture.jsonl dirotasi (64MB->32MB, in-inode)"
        fi
    fi

    # --- 5. гаджет отвалился при воткнутом RC ---------------------------
    # RC-N1 перечисляет аксессуар медленно и с паузами: bounce по "not
    # attached" за 20 с убивал сессию прямо посреди хендшейка. Порог —
    # 90 с, и он обнуляется любым трафиком на :9910 (streamer живёт).
    # --- 5. гаджет отвалился при воткнутом RC ---------------------------
    # RC-N1 перечисляет аксессуар медленно и с паузами: bounce по "not
    # attached" за 20 с убивал сессию прямо посреди хендшейка. Порог —
    # 90 с, и он обнуляется любым трафиком на :9910 (streamer живёт).
    # PENTING: "not attached" juga = RC memang lepas — restart di sana
    # adalah churn sia-sia (tiap ~90s membunuh :9910 + reload display).
    # Hanya bertindak bila UDC pernah "configured" sesi ini (RC fisik
    # terpasang tapi gadget mati) — flag wdg_udc_cfg.
    st=$(cat "$UDC_STATE" 2>/dev/null)
    if [ "$st" = "configured" ]; then
        touch /tmp/wdg_udc_cfg
    fi
    if [ "$st" = "not attached" ] && [ -f /tmp/wdg_udc_cfg ]; then
        active_rx=$(find /var/log/dji-link/bridge.log -newermt -30seconds 2>/dev/null)
        if [ -z "$active_rx" ] && [ ! -f /tmp/wdg_udc_wait ]; then
            touch /tmp/wdg_udc_wait
        elif [ -n "$active_rx" ]; then
            rm -f /tmp/wdg_udc_wait
        elif [ $(( $(date +%s) - $(stat -c %Y /tmp/wdg_udc_wait) )) -ge 90 ]; then
            say "UDC not attached >90s setelah configured, no rx -> restart dji-bridge"
            systemctl restart $BRIDGE_SVC
            rm -f /tmp/wdg_udc_wait /tmp/wdg_udc_cfg
            # Satu bounce per detach: flag cfg wajib di-clear di sini —
            # tanpa ini restart berulang tiap ~135s selamanya selama RC
            # memang tercabut (terukur n>30), dan colok-RC-tepat-di-
            # jendela-restart membunuh handshake setengah jalan = "miss".
            # configured berikutnya men-set flag lagi -> proteksi re-arm.
            sleep 5
        fi
    else
        rm -f /tmp/wdg_udc_wait
    fi

    # --- 6. pipa mati: duml_rx stagnan padahal UDC configured ----------
    # USB-flap membuat raw_gadget basi dengan cara yang licik: state bisa
    # terbaca "configured" dan enum tampak selesai, tapi pipa AOA kosong —
    # streamer tx jalan terus, duml_rx diam. Ini insiden 20:15-20:20 yang
    # lolos rule 1b/5 (state berkedip saat enum-retry + ambang event
    # kelewat tinggi). Invariant sebenarnya: selama configured, RC selalu
    # kirim duml. Ambang 88 dtk menutupi startup + jeda replay normal.
    st=$(cat "$UDC_STATE" 2>/dev/null)
    rx=$(journalctl -u $STREAMER_SVC -o cat --since "-35s" 2>/dev/null | \
         grep -oE "duml_rx=[0-9]+" | tail -1 | cut -d= -f2)
    if [ "$st" = "configured" ] && [ -n "$rx" ]; then
        if [ "$rx" = "$(cat /tmp/wdg_rx_last 2>/dev/null)" ]; then
            n=$(( $(cat /tmp/wdg_rx_stall 2>/dev/null || echo 0) + 1 ))
            echo $n > /tmp/wdg_rx_stall
            if [ "$n" -ge 11 ]; then
                say "duml_rx stagnan di $rx padahal configured -> restart dji-bridge"
                systemctl restart $BRIDGE_SVC
                rm -f /tmp/wdg_rx_stall /tmp/wdg_rx_last
                sleep 5
            fi
        else
            echo "$rx" > /tmp/wdg_rx_last
            echo 0 > /tmp/wdg_rx_stall
        fi
    else
        rm -f /tmp/wdg_rx_stall
    fi
done
