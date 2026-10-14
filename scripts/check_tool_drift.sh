#!/usr/bin/env bash
set -euo pipefail
MCP=${MCP:-https://mcp.cryptovault.co.uk/mcp/}; BASE=${BASE:-https://mcp.cryptovault.co.uk}
H=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
SID=$(curl -s -D - -o /dev/null "${H[@]}" "$MCP" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"drift-check","version":"1"}}}' \
  | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}' | tr -d '\r')
curl -s -o /dev/null "${H[@]}" ${SID:+-H "Mcp-Session-Id: $SID"} "$MCP" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
curl -s "${H[@]}" ${SID:+-H "Mcp-Session-Id: $SID"} "$MCP" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | jq -r '.result.tools[].name' | sort > /tmp/live.txt
curl -s "$BASE/.well-known/mcp/server-card.json" | jq -r '.tools[].name' | sort > /tmp/card.txt
curl -s "$BASE/.well-known/marketplace.json"     | jq -r '.mcp_tools[]'   | sort > /tmp/mkt.txt
echo "live=$(wc -l </tmp/live.txt) card=$(wc -l </tmp/card.txt) marketplace=$(wc -l </tmp/mkt.txt)"
rc=0; for f in card mkt; do diff -u /tmp/live.txt /tmp/$f.txt >/dev/null || { echo "DRIFT in $f:"; diff /tmp/live.txt /tmp/$f.txt | grep '^[<>]' || true; rc=1; }; done
[ $rc = 0 ] && echo "OK: server-card and marketplace.json match tools/list"; exit $rc
