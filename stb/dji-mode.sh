#!/bin/sh
# Режимы STB:
#   standalone — replay рецепта (streamer.py, production)
#   synth      — синтезированная сессия (streamer_min.py)
#   mitm       — телефон через STB (только диагностика)
MODE=/run/dji-mode
case "$1" in
  standalone|synth|mitm)
    systemctl disable --now dji-mitm dji-streamer dji-streamer-min 2>/dev/null
    case "$1" in
      standalone) systemctl enable --now dji-streamer dji-display ;;
      synth)      systemctl enable --now dji-streamer-min dji-display ;;
      mitm)       systemctl enable --now dji-mitm dji-display ;;
    esac
    echo "$1" > $MODE
    echo "mode=$1"
    ;;
  *) echo 'usage: dji-mode.sh standalone|synth|mitm'; exit 1;;
esac
