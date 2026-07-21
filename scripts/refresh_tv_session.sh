#!/bin/bash
# Sync TradingView session from MacBook to Mini and restart TradingView.
# Run this when TradingView on Mini shows a login screen or session expired.

MINI="sha.zhougmail.com@100.64.0.5"
TV_SRC="/Users/shazhou/Library/Application Support/TradingView"
TV_SERVICE="com.autotrader.tradingview"

echo "=== TradingView session refresh ==="
echo "Source: MacBook ($(whoami))"
echo "Target: $MINI"
echo ""

# Stop TradingView on Mini first so the Cookies file isn't locked
echo "[1/3] Stopping TradingView on Mini..."
ssh "$MINI" "launchctl unload ~/Library/LaunchAgents/${TV_SERVICE}.plist 2>/dev/null; sleep 2"

# Sync session files: Cookies, user storage, app state
echo "[2/3] Syncing session..."
tar czf - \
    -C "/Users/shazhou/Library/Application Support" \
    --exclude="TradingView/Crashpad" \
    --exclude="TradingView/GPUCache" \
    --exclude="TradingView/ShaderCache" \
    --exclude="TradingView/Code Cache" \
    TradingView \
  | ssh "$MINI" "tar xzf - -C ~/Library/Application\ Support/"

if [ $? -ne 0 ]; then
    echo "ERROR: sync failed. Restarting TradingView anyway..."
    ssh "$MINI" "launchctl load ~/Library/LaunchAgents/${TV_SERVICE}.plist"
    exit 1
fi

# Restart TradingView on Mini
echo "[3/3] Starting TradingView on Mini..."
ssh "$MINI" "launchctl load ~/Library/LaunchAgents/${TV_SERVICE}.plist"

# Wait and verify CDP is up
echo ""
echo "Waiting for TradingView to start..."
sleep 15
STATUS=$(ssh "$MINI" "cd ~/tradingview-mcp && PATH=/opt/homebrew/bin:\$PATH node src/cli/index.js status 2>&1")
echo "$STATUS"

if echo "$STATUS" | grep -q '"api_available": true'; then
    echo ""
    echo "✓ Session refreshed. TradingView is live on Mini."
else
    echo ""
    echo "⚠ TradingView started but chart API not ready yet — give it another 30s."
fi
