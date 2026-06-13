#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="${PDF_EDGE_TRIMMER_APP_NAME:-PDF Edge Trimmer}"
PORT="${PDF_EDGE_TRIMMER_PORT:-53770}"
APP_PATH="$ROOT/dist/$APP_NAME.app"
SCRIPT_PATH="$(mktemp /tmp/pdf-edge-trimmer-launcher.XXXXXX.applescript)"

mkdir -p "$ROOT/dist" "$ROOT/logs" "$ROOT/output"

cat > "$SCRIPT_PATH" <<APPLESCRIPT
set appUrl to "http://127.0.0.1:$PORT/"
set projectDir to "$ROOT"
set logFile to projectDir & "/logs/pdf-edge-trimmer.log"
set outputDir to projectDir & "/output"
set launchCommand to "cd " & quoted form of projectDir & "; PYTHONPATH=" & quoted form of (projectDir & "/src") & " PDF_EDGE_TRIMMER_OUTPUT_DIR=" & quoted form of outputDir & " nohup /usr/bin/python3 -u -m pdf_edge_trimmer --web --no-open --port $PORT > " & quoted form of logFile & " 2>&1 &"

try
	do shell script "/usr/bin/curl -fsS --max-time 1 " & quoted form of appUrl & " >/dev/null"
on error
	do shell script launchCommand
	repeat 20 times
		delay 0.25
		try
			do shell script "/usr/bin/curl -fsS --max-time 1 " & quoted form of appUrl & " >/dev/null"
			exit repeat
		end try
	end repeat
end try

open location appUrl
APPLESCRIPT

osacompile -o "$APP_PATH" "$SCRIPT_PATH"
rm -f "$SCRIPT_PATH"

echo "Built: $APP_PATH"
