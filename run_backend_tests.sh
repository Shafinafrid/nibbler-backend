#!/bin/bash
# Run every backend suite, writing a one-line result per suite.
#
# NOTE: macOS has no `timeout` binary — do NOT wrap the python call with it,
# or every suite reports a false FAIL(127).
#
#   bash run_backend_tests.sh [output-file]
out="${1:-backend_results.txt}"
python_bin="${PYTHON_BIN:-./.venv/bin/python}"
: > "$out"
fails=0
for t in tests/test_*.py; do
  # Isolation is enforced by the runner, not left to every individual file.
  # hermetic moves away from the repo .env, overwrites inherited provider
  # credentials and blocks non-loopback sockets before the suite imports app.
  result=$("$python_bin" -c '
import importlib, runpy, sys
sys.path.insert(0, sys.argv[2])
importlib.import_module("hermetic")
runpy.run_path(sys.argv[1], run_name="__main__")
' "$PWD/$t" "$PWD/tests" 2>&1)
  code=$?
  if [ $code -ne 0 ]; then
    echo "FAIL($code) $t" >> "$out"
    echo "$result" | tail -6 | sed 's/^/    /' >> "$out"
    fails=$((fails+1))
  else
    echo "ok       $t" >> "$out"
  fi
done
echo "---" >> "$out"
echo "suites failing: $fails" >> "$out"
cat "$out"
if [ "$fails" -ne 0 ]; then
  exit 1
fi
exit 0
