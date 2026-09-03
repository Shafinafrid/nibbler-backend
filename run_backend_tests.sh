#!/bin/bash
# Run every backend suite, writing a one-line result per suite.
#
# NOTE: macOS has no `timeout` binary — do NOT wrap the python call with it,
# or every suite reports a false FAIL(127).
#
#   bash run_backend_tests.sh [output-file]
out="${1:-backend_results.txt}"
: > "$out"
fails=0
for t in tests/test_*.py; do
  result=$(./.venv/bin/python "$t" 2>&1)
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
exit 0
