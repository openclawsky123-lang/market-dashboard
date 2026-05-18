#!/bin/bash
# ─────────────────────────────────────────────────────────
#  Market Dashboard — One-time setup script
#  Run this once in Claude Code terminal:  bash setup_dashboard.sh
# ─────────────────────────────────────────────────────────

set -e
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SCRIPT_PATH="$SCRIPT_DIR/market_dashboard.py"

echo ""
echo "================================================="
echo "  Market Dashboard Setup"
echo "================================================="

# 1. Install Python dependencies
echo ""
echo "[1/3] Installing Python dependencies..."
pip3 install requests yfinance --quiet --break-system-packages 2>/dev/null || \
pip3 install requests yfinance --quiet

echo "      Done: requests, yfinance installed"

# 2. Test run
echo ""
echo "[2/3] Running test fetch (this takes ~15 seconds)..."
python3 "$SCRIPT_PATH"

if [ $? -ne 0 ]; then
  echo "  ERROR: Script failed. Check error above."
  exit 1
fi

echo "      Test run successful. Check ~/Desktop/market_dashboard.html"

# 3. Set up cron job (10am and 10pm HKT = 02:00 and 14:00 UTC)
echo ""
echo "[3/3] Setting up cron job (10am & 10pm HKT)..."

PYTHON_PATH=$(which python3)
LOG_PATH="$HOME/Desktop/market_dashboard.log"
CRON_JOB="0 2,14 * * * $PYTHON_PATH $SCRIPT_PATH >> $LOG_PATH 2>&1"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -q "market_dashboard.py"; then
  echo "      Cron job already exists — removing old one first..."
  crontab -l 2>/dev/null | grep -v "market_dashboard.py" | crontab -
fi

# Add new cron job
(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "      Cron job added!"
echo ""
echo "  Verifying cron:"
crontab -l | grep market_dashboard

echo ""
echo "================================================="
echo "  SETUP COMPLETE"
echo "================================================="
echo ""
echo "  Dashboard file : ~/Desktop/market_dashboard.html"
echo "  Log file       : ~/Desktop/market_dashboard.log"
echo "  Schedule       : 10:00am & 10:00pm HKT daily"
echo "  Cron (UTC)     : 02:00 & 14:00 UTC"
echo ""
echo "  To open dashboard now:"
echo "    open ~/Desktop/market_dashboard.html"
echo ""
echo "  To check cron jobs:"
echo "    crontab -l"
echo ""
echo "  To remove the cron job:"
echo "    crontab -l | grep -v market_dashboard | crontab -"
echo ""
