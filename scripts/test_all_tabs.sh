#!/bin/bash
for tab in trend reversal sector dtqiaoban; do
  echo "=== $tab ==="
  curl -s -m 30 -w 'TIME=%{time_total}s\n' "http://127.0.0.1:8080/api/bt/$tab?days=5&top_n=3&capital=30000" | head -c 250
  echo
done