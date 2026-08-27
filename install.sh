#!/bin/bash
# 安装 BTC 均线监控的 launchd 定时任务（每 15 分钟自动检查一次）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.btc.monitor.plist"
PYTHON="$(command -v python3)"
LABEL="com.btc.monitor"

if [ ! -f "$DIR/config.json" ]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  echo "已生成 config.json，请先编辑它填入通知渠道的密钥，再重新运行本脚本。"
  echo "  编辑: open -e \"$DIR/config.json\""
  exit 0
fi

# 路径可能含 & < > 等 XML 特殊字符，写入 plist 前需转义
xml_escape() { local s=$1; s=${s//&/&amp;}; s=${s//</&lt;}; s=${s//>/&gt;}; printf '%s' "$s"; }
DIR_XML=$(xml_escape "$DIR")
PYTHON_XML=$(xml_escape "$PYTHON")

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_XML}</string>
        <string>${DIR_XML}/btc_monitor.py</string>
        <string>check</string>
    </array>
    <key>StartInterval</key><integer>900</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>${DIR_XML}/launchd.log</string>
    <key>StandardErrorPath</key><string>${DIR_XML}/launchd.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" > /dev/null

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ 已安装并启动：每 15 分钟自动检查一次 BTC 均线"
echo "   查看运行日志: tail -f \"$DIR/monitor.log\""
echo "   卸载: bash \"$DIR/uninstall.sh\""
