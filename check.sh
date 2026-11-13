#!/usr/bin/env bash
# Gerbang syntax lokal — jalankan SEBELUM commit/deploy:
#   bash check.sh
# Menangkap file rusak sebelum sempat ter-deploy. Exit = jumlah gagal.
set -u
cd "$(dirname "$0")"
F=0

echo "== python AST =="
PY=""
for c in python3 python py; do
  if "$c" -c "import ast" 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "!! tidak ada interpreter python — cek AST dilewati"
else
for f in stb/*.py; do
  # fcntl/dkk Unix-only — parse AST saja, bukan import.
  if "$PY" -c "import ast; ast.parse(open('$f',encoding='utf-8').read())" 2>/dev/null; then
    echo "ok   $f"
  else
    echo "FAIL $f"; "$PY" -c "import ast; ast.parse(open('$f',encoding='utf-8').read())" 2>&1 | tail -3
    F=$((F+1))
  fi
done
fi

echo "== sh syntax =="
for f in stb/*.sh deploy.sh check.sh; do
  [ -f "$f" ] || continue
  if sh -n "$f" 2>/dev/null; then echo "ok   $f"
  else echo "FAIL $f"; sh -n "$f"; F=$((F+1)); fi
done

echo "----"
[ $F -eq 0 ] && echo "CHECK: SEMUA OK" || echo "CHECK: $F GAGAL"
exit $F
