#!/bin/bash
PYEXE="/c/Program Files/WindowsApps/PythonSoftwareFoundation.PythonManager_26.1.240.0_x64__3847v3x7pw1km/pythonw.exe"
export PYTHONUTF8=1; export PYTHONIOENCODING=utf-8
t0=$(date +%s)
for y in 2020 2021 2022 2023 2024 2025 2026; do
  echo "=== $y $(date '+%H:%M:%S') ==="
  rm -f keiba_tmp_${y}.db btv6_hotness_${y}.json
  "$PYEXE" -X utf8 -u backtest_v6_hotness.py --year $y 2>&1 | tail -8
done
echo "🏁 hotness $(date '+%H:%M:%S') $(( $(date +%s) - t0 ))s"
