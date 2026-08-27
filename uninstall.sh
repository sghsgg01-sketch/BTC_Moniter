#!/bin/bash
# 卸载 BTC 均线监控的定时任务（不删除程序和配置文件）
PLIST="$HOME/Library/LaunchAgents/com.btc.monitor.plist"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "已卸载定时任务。程序文件仍保留在 $(cd "$(dirname "$0")" && pwd)"
