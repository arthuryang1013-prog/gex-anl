#!/bin/bash
# NQ GEX monitor -- double-click launcher for macOS.
# Starts the collector (background, log in collector.log) and the dashboard (this window).
# Stop everything with Ctrl+C or by closing this window.
# Before launching: IB Gateway (or TWS) must be running and logged in.

cd "$(dirname "$0")" || exit 1

echo "Using: $(python3 --version)"

# Streamlit asks for an email on its very first run; this pre-answers it (only if no config exists yet).
mkdir -p "$HOME/.streamlit"
[ -f "$HOME/.streamlit/credentials.toml" ] || printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"

echo "Starting collector (log: collector.log) ..."
python3 -u nq_gex_collector.py > collector.log 2>&1 &
COLLECTOR_PID=$!

cleanup() {
  echo
  echo "Stopping collector ..."
  kill "$COLLECTOR_PID" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 0' INT TERM HUP

sleep 10
if ! kill -0 "$COLLECTOR_PID" 2>/dev/null; then
  echo
  echo "The collector stopped right after starting. Last lines of collector.log:"
  echo "------------------------------------------------------------"
  tail -n 15 collector.log
  echo "------------------------------------------------------------"
  echo "Common causes: IB Gateway is not logged in, or the API is not enabled."
  echo "Press Enter to close this window."
  read -r
  exit 1
fi

echo "Collector is running. Opening the dashboard (the first snapshot takes 1-2 minutes) ..."
python3 -m streamlit run nq_gex_dashboard.py
