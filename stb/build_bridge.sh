#!/usr/bin/env bash
# Build the C++ dji-bridge natively on the HG680P (aarch64, gcc from Armbian).
# The bridge is self-contained POSIX code — no external libs, no cmake needed.
# Usage: bash build_bridge.sh
set -e
cd "$(dirname "$0")/pi_src"

CXX="${CXX:-g++}"
if ! command -v "$CXX" >/dev/null; then
    echo "g++ not found — apt install g++ first"; exit 1
fi

echo ">> building dji-bridge (aarch64, -O2)"
# bridge.cpp includes "pi/aoa_device.hpp" — fake that layout with -I.
mkdir -p .inc/pi
cp aoa_device.hpp raw_gadget.hpp .inc/pi/
"$CXX" -std=c++20 -O2 -pthread -I.inc \
    bridge.cpp aoa_device.cpp raw_gadget.cpp \
    -o ../dji-bridge
echo ">> done: $(ls -la ../dji-bridge | awk '{print $5}') bytes"
echo "   run: sudo ./dji-bridge            (autodetect UDC, listen :9910)"
