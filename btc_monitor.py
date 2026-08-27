#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 均线监控程序

监测 BTC 价格与关键 EMA 均线的关系：
  - 日线: EMA120 / EMA200 / EMA350
  - 周线: EMA250 / EMA350
当价格抵达（进入阈值区间、发生穿越、或当根K线扫过均线）时，
通过 Server酱(微信) / PushPlus(微信) / Telegram / macOS 通知发出提醒。

数据源: Binance (主) -> OKX (备)，EMA 算法与 TradingView 一致
（含当前未收盘K线，前 N 根收盘价的 SMA 作种子，alpha = 2/(N+1)）。

用法:
  python3 btc_monitor.py check        # 单次检查（供 launchd/cron 定时调用）
  python3 btc_monitor.py status       # 查看当前价格与各均线的距离
  python3 btc_monitor.py test-notify  # 测试通知渠道是否配置成功
  python3 btc_monitor.py daemon       # 前台循环运行（不用 launchd 时）
"""

import copy
import fcntl
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("BTC_MONITOR_CONFIG") or os.path.join(
    BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "monitor.log")

# GitHub Actions 等美国机房 IP 会被 api.binance.com 拒绝(HTTP 451)，
# data-api.binance.vision 是币安官方公开行情镜像，数据完全一致且无地区限制
BINANCE_HOSTS = ["data-api.binance.vision", "api.binance.com"]

DEFAULT_CONFIG = {
    "symbol": "BTCUSDT",
    "levels": {"daily": [120, 200, 350], "weekly": [250, 350]},
    # 价格与均线的距离小于该百分比视为"抵达"
    "touch_threshold_pct": 1.0,
    # 触发一次提醒后，价格需离开均线该百分比之外才重新武装（避免反复提醒）
    # 设为 0 表示只按 cooldown_minutes 间隔重复提醒
    "rearm_pct": 3.0,
    # 同一均线两次提醒之间的最小间隔（分钟）
    "cooldown_minutes": 360,
    # daemon 模式下的检查间隔（秒）
    "check_interval_seconds": 900,
    # 连续多少次拉取数据失败后发一次故障提醒
    "failure_alert_threshold": 8,
    "notify": {
        "serverchan": {"enabled": False, "sendkey": ""},
        "pushplus": {"enabled": False, "token": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": "", "proxy": ""},
        "macos": {"enabled": True},
    },
}

WEEK_MS = 7 * 24 * 3600 * 1000
DAY_MS = 24 * 3600 * 1000

SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- 基础工具

def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_env_overrides(cfg):
    """从环境变量读取密钥并自动启用对应渠道。
    这样部署到 GitHub Actions 时密钥只存在 Secrets 里，不进代码仓库。"""
    n = cfg["notify"]
    if os.environ.get("SERVERCHAN_SENDKEY"):
        n["serverchan"]["sendkey"] = os.environ["SERVERCHAN_SENDKEY"]
        n["serverchan"]["enabled"] = True
    if os.environ.get("PUSHPLUS_TOKEN"):
        n["pushplus"]["token"] = os.environ["PUSHPLUS_TOKEN"]
        n["pushplus"]["enabled"] = True
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        n["telegram"]["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
        n["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
        n["telegram"]["enabled"] = True
    if sys.platform != "darwin":
        n["macos"]["enabled"] = False  # 非 macOS 环境没有 osascript
    return cfg


def load_config():
    # 深拷贝，避免 env 覆盖或 daemon 反复加载时污染 DEFAULT_CONFIG
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = deep_merge(cfg, json.load(f))
        except (ValueError, OSError) as e:
            log("配置文件读取失败，使用默认配置: %s" % e)
    return apply_env_overrides(cfg)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {"levels": {}, "consecutive_failures": 0, "failure_alerted": False}


def save_state(state):
    tmp = "%s.tmp.%d" % (STATE_PATH, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def http_get_json(url, timeout=15, proxy=""):
    req = urllib.request.Request(url, headers={"User-Agent": "btc-monitor/1.0"})
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(
            handler, urllib.request.HTTPSHandler(context=SSL_CTX))
    else:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=SSL_CTX))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, timeout=15, proxy=""):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "btc-monitor/1.0",
                 "Content-Type": "application/json"})
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(
            handler, urllib.request.HTTPSHandler(context=SSL_CTX))
    else:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=SSL_CTX))
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------- 行情数据
# K线统一格式: (open_time_ms, high, low, close)

def fetch_binance_klines(symbol, interval, host):
    """分页拉取 Binance 全部历史K线（最后一根为当前未收盘K线）。"""
    out = []
    start = 0
    while True:
        url = ("https://%s/api/v3/klines"
               "?symbol=%s&interval=%s&limit=1000&startTime=%d"
               % (host, symbol, interval, start))
        batch = http_get_json(url)
        if not isinstance(batch, list):
            raise RuntimeError("Binance 返回异常: %s" % str(batch)[:200])
        if not batch:
            break
        for k in batch:
            out.append((int(k[0]), float(k[2]), float(k[3]), float(k[4])))
        if len(batch) < 1000:
            break
        start = int(batch[-1][0]) + 1
    if not out:
        raise RuntimeError("Binance 未返回K线数据")
    return out


def fetch_okx_klines(inst_id, bar):
    """分页拉取 OKX 全部历史K线，返回按时间升序。"""
    out = []
    # 当前时段的最新K线来自 candles 接口
    url = ("https://www.okx.com/api/v5/market/candles"
           "?instId=%s&bar=%s&limit=300" % (inst_id, bar))
    resp = http_get_json(url)
    if resp.get("code") != "0":
        raise RuntimeError("OKX candles 返回异常: %s" % str(resp)[:200])
    rows = resp["data"]  # 降序
    for k in rows:
        out.append((int(k[0]), float(k[2]), float(k[3]), float(k[4])))
    # 更早的历史用 history-candles 分页（before=更早方向用 after 参数）
    while rows:
        oldest = int(rows[-1][0])
        url = ("https://www.okx.com/api/v5/market/history-candles"
               "?instId=%s&bar=%s&limit=100&after=%d" % (inst_id, bar, oldest))
        resp = http_get_json(url)
        if resp.get("code") != "0":
            # 分页中途失败必须报错，静默截断会算出严重失真的均线
            raise RuntimeError("OKX history-candles 返回异常: %s" % str(resp)[:200])
        rows = resp["data"]
        for k in rows:
            out.append((int(k[0]), float(k[2]), float(k[3]), float(k[4])))
        time.sleep(0.15)  # OKX history 接口限速
    out.sort(key=lambda x: x[0])
    if not out:
        raise RuntimeError("OKX 未返回K线数据")
    return out


def fetch_klines(symbol, timeframe):
    """timeframe: 'daily' | 'weekly'。依次尝试币安各入口，最后降级到 OKX。
    各币安入口数据完全一致，故均标记为同一数据源 'Binance'。"""
    errors = []
    interval = {"daily": "1d", "weekly": "1w"}[timeframe]
    for host in BINANCE_HOSTS:
        try:
            return fetch_binance_klines(symbol, interval, host), "Binance"
        except Exception as e:  # noqa: BLE001 - 任意网络/解析错误都降级
            errors.append("%s: %s" % (host, e))
    try:
        inst = symbol.replace("USDT", "-USDT")
        bar = {"daily": "1Dutc", "weekly": "1Wutc"}[timeframe]
        return fetch_okx_klines(inst, bar), "OKX"
    except Exception as e:  # noqa: BLE001
        errors.append("OKX: %s" % e)
    raise RuntimeError("所有数据源均失败: " + " | ".join(errors))


def ema(closes, period):
    """与 TradingView 一致：前 period 根收盘价的 SMA 作种子,
    alpha=2/(N+1), 之后逐根递推。"""
    alpha = 2.0 / (period + 1)
    val = sum(closes[:period]) / float(period)
    for c in closes[period:]:
        val = alpha * c + (1 - alpha) * val
    return val


# ---------------------------------------------------------------- 监控逻辑

def build_snapshot(cfg):
    """拉取数据并计算所有目标均线。返回 (price, levels, sources)
    levels: [{key, name, period, timeframe, value, dist_pct,
              candle_high, candle_low, bars}]"""
    levels = []
    sources = {}
    price = None
    for timeframe in ("daily", "weekly"):
        periods = cfg["levels"].get(timeframe) or []
        if not periods:
            continue
        klines, src = fetch_klines(cfg["symbol"], timeframe)
        sources[timeframe] = src
        closes = [k[3] for k in klines]
        cur_high, cur_low, cur_close = klines[-1][1], klines[-1][2], klines[-1][3]
        if price is None:
            # daily 在前，故优先用日线；仅配置周线时用周线当前K线收盘价
            price = cur_close
        tf_name = "日线" if timeframe == "daily" else "周线"
        for p in periods:
            if len(closes) < p:
                log("警告: %s K线只有 %d 根，不足 EMA%d 周期，跳过"
                    % (tf_name, len(closes), p))
                continue
            v = ema(closes, p)
            levels.append({
                "key": "%s_%s_ema%d" % (cfg["symbol"], timeframe, p),
                "name": "%s EMA%d" % (tf_name, p),
                "period": p,
                "timeframe": timeframe,
                "value": v,
                "candle_high": cur_high,
                "candle_low": cur_low,
                "candle_open": klines[-1][0],
                "bars": len(closes),
            })
    if price is None:
        raise RuntimeError("未能获取最新价格（daily 级别未配置或拉取失败）")
    for lv in levels:
        lv["dist_pct"] = (price - lv["value"]) / lv["value"] * 100.0
    return price, levels, sources


def check_level(lv, price, cfg, lst, now_ts):
    """判断单条均线是否需要提醒。返回 (should_alert, reason, new_state)。"""
    touch_pct = float(cfg["touch_threshold_pct"])
    rearm_pct = float(cfg["rearm_pct"])
    cooldown_s = float(cfg["cooldown_minutes"]) * 60

    dist = lv["dist_pct"]
    side = "above" if price >= lv["value"] else "below"
    prev_side = lst.get("side")
    in_band = abs(dist) <= touch_pct
    crossed = prev_side is not None and prev_side != side
    # 当根K线（今日/本周）的高低点是否扫过均线。K线范围只增不减，
    # 所以同一根K线只允许触发一次，且首次运行不追溯本根K线早前的影线
    straddled = lv["candle_low"] <= lv["value"] <= lv["candle_high"]
    range_touch = (straddled and bool(lst)
                   and lst.get("range_candle") != lv["candle_open"])

    armed = lst.get("armed", True)
    # 价格离开均线足够远后重新武装；rearm_pct=0 表示始终武装（纯冷却模式）
    if rearm_pct <= 0 or abs(dist) > rearm_pct:
        armed = True

    event = in_band or crossed or range_touch
    reasons = []
    if in_band:
        reasons.append("价格进入均线 ±%.2f%% 区间" % touch_pct)
    if crossed:
        reasons.append("价格%s穿均线" % ("上" if side == "above" else "下"))
    if range_touch and not in_band and not crossed:
        reasons.append("当根K线影线扫过均线")

    last_alert = float(lst.get("last_alert_ts") or 0)
    cooled = (now_ts - last_alert) >= cooldown_s

    should = bool(event and armed and cooled)
    new_state = {
        # 穿越事件被冷却/静默拦下时保留旧方向，让它保持待发而不是被吞掉
        "side": (prev_side if (crossed and not should) else side),
        "armed": (False if should else armed),
        "last_alert_ts": (now_ts if should else last_alert),
        "last_dist_pct": round(dist, 4),
        # 任何提醒发生时若K线正扫过均线，则本根K线的影线触碰视为已报告
        "range_candle": (lv["candle_open"] if (should and straddled)
                         else lst.get("range_candle")),
    }
    return should, "；".join(reasons), new_state


def format_alert(hits, price, now):
    lines = ["⚠️ BTC 抵达关键均线", ""]
    lines.append("当前价格: $%s" % format(price, ",.0f"))
    for lv, reason in hits:
        lines.append("")
        lines.append("▸ %s = $%s（偏离 %+.2f%%）"
                     % (lv["name"], format(lv["value"], ",.0f"), lv["dist_pct"]))
        lines.append("  触发: %s" % reason)
    lines.append("")
    lines.append("时间: %s" % now.strftime("%Y-%m-%d %H:%M %Z"))
    return "\n".join(lines)


# ---------------------------------------------------------------- 通知渠道

def notify_serverchan(cfg_sc, title, body):
    url = "https://sctapi.ftqq.com/%s.send" % cfg_sc["sendkey"]
    data = urllib.parse.urlencode(
        {"title": title, "desp": body.replace("\n", "\n\n")}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "btc-monitor/1.0"})
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=SSL_CTX))
    with opener.open(req, timeout=15) as resp:
        r = json.loads(resp.read().decode("utf-8"))
    if r.get("code") not in (0, "0"):
        raise RuntimeError("Server酱返回: %s" % str(r)[:200])


def notify_pushplus(cfg_pp, title, body):
    r = http_post_json("https://www.pushplus.plus/send",
                       {"token": cfg_pp["token"], "title": title,
                        "content": body.replace("\n", "<br>"),
                        "template": "html"})
    rj = json.loads(r)
    if rj.get("code") != 200:
        raise RuntimeError("PushPlus返回: %s" % str(rj)[:200])


def notify_telegram(cfg_tg, title, body):
    url = "https://api.telegram.org/bot%s/sendMessage" % cfg_tg["bot_token"]
    r = http_post_json(url, {"chat_id": cfg_tg["chat_id"],
                             "text": title + "\n\n" + body},
                       proxy=cfg_tg.get("proxy", ""))
    rj = json.loads(r)
    if not rj.get("ok"):
        raise RuntimeError("Telegram返回: %s" % str(rj)[:200])


def notify_macos(_cfg, title, body):
    short = body.split("\n\n")[0].replace('"', "'")[:180]
    p = subprocess.run(
        ["osascript", "-e",
         'display notification "%s" with title "%s" sound name "Glass"'
         % (short.replace("\\", "").replace('"', "'"),
            title.replace('"', "'"))],
        check=False, capture_output=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError("osascript exit %d: %s"
                           % (p.returncode,
                              p.stderr.decode("utf-8", "replace").strip()[:200]))


CHANNELS = [
    ("serverchan", notify_serverchan, "sendkey"),
    ("pushplus", notify_pushplus, "token"),
    ("telegram", notify_telegram, "bot_token"),
    ("macos", notify_macos, None),
]


def send_all(cfg, title, body):
    """向所有已启用渠道发送。返回成功渠道数。"""
    sent = 0
    for name, fn, required_key in CHANNELS:
        ch = cfg["notify"].get(name) or {}
        if not ch.get("enabled"):
            continue
        if required_key and not ch.get(required_key):
            log("渠道 %s 已启用但缺少 %s，跳过" % (name, required_key))
            continue
        try:
            fn(ch, title, body)
            sent += 1
            log("通知已发送: %s" % name)
        except Exception as e:  # noqa: BLE001
            log("通知发送失败 [%s]: %s" % (name, e))
    return sent


# ---------------------------------------------------------------- 命令

def cmd_check(cfg):
    state = load_state()
    try:
        price, levels, sources = build_snapshot(cfg)
    except Exception as e:  # noqa: BLE001
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        log("数据拉取失败(第%d次): %s" % (state["consecutive_failures"], e))
        if (state["consecutive_failures"] >= int(cfg["failure_alert_threshold"])
                and not state.get("failure_alerted")):
            send_all(cfg, "BTC监控: 数据源异常",
                     "已连续 %d 次拉取行情失败，请检查网络。\n最近错误: %s"
                     % (state["consecutive_failures"], e))
            state["failure_alerted"] = True
        save_state(state)
        return 1

    if state.get("consecutive_failures"):
        log("数据源恢复正常")
    state["consecutive_failures"] = 0
    state["failure_alerted"] = False

    now = datetime.now(timezone.utc).astimezone()
    now_ts = time.time()
    hits = []
    prev_states = {}
    for lv in levels:
        lst = state["levels"].get(lv["key"], {})
        src = sources[lv["timeframe"]]
        if lst and lst.get("source") != src:
            # 数据源切换后均线数值有偏差，废弃旧方向记录以免报假穿越
            lst = dict(lst, side=None)
        should, reason, new_state = check_level(lv, price, cfg, lst, now_ts)
        new_state["source"] = src
        prev_states[lv["key"]] = lst
        state["levels"][lv["key"]] = new_state
        log("%s: EMA=$%.0f 偏离%+.2f%% %s"
            % (lv["name"], lv["value"], lv["dist_pct"],
               ("→ 触发提醒(%s)" % reason) if should
               else ("[%s]" % reason if reason else "")))
        if should:
            hits.append((lv, reason))

    # 清理配置中已不存在的均线的历史状态
    valid = {lv["key"] for lv in levels}
    state["levels"] = {k: v for k, v in state["levels"].items() if k in valid}

    if hits:
        body = format_alert(hits, price, now)
        title = "BTC 抵达 " + "、".join(lv["name"] for lv, _ in hits)
        if send_all(cfg, title, body) == 0:
            log("警告: 有触发但没有任何通知渠道发送成功，保留触发状态供下次重试")
            for lv, _ in hits:
                st = state["levels"][lv["key"]]
                old = prev_states[lv["key"]]
                st["armed"] = True
                st["last_alert_ts"] = float(old.get("last_alert_ts") or 0)
                st["range_candle"] = old.get("range_candle")
    save_state(state)
    return 0


def cmd_status(cfg):
    price, levels, sources = build_snapshot(cfg)
    print()
    print("BTC 当前价格: $%s   (数据源: %s)"
          % (format(price, ",.2f"),
             " / ".join("%s:%s" % (k, v) for k, v in sources.items())))
    print()
    print("%-14s %14s %10s %8s" % ("均线", "数值", "偏离", "K线数"))
    print("-" * 50)
    for lv in sorted(levels, key=lambda x: -x["value"]):
        marker = "  ← 接近!" if abs(lv["dist_pct"]) <= cfg["touch_threshold_pct"] else ""
        print("%-12s %14s %+9.2f%% %8d%s"
              % (lv["name"], "$" + format(lv["value"], ",.0f"),
                 lv["dist_pct"], lv["bars"], marker))
    print()
    print("提醒条件: 偏离 ≤ ±%.1f%%，或穿越，或当根K线扫过均线"
          % cfg["touch_threshold_pct"])
    return 0


def cmd_test_notify(cfg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = send_all(cfg, "BTC监控: 测试消息",
                 "✅ 通知渠道配置成功！\n发送时间: %s" % now)
    enabled = [name for name, _, _ in CHANNELS
               if (cfg["notify"].get(name) or {}).get("enabled")]
    print("已启用渠道: %s；发送成功: %d 个" % (", ".join(enabled) or "无", n))
    if not enabled:
        print("没有任何已启用的通知渠道。本机请编辑 config.json；"
              "GitHub Actions 请确认已添加 SERVERCHAN_SENDKEY 等 Secret。")
    # 明确的测试命令：一条都没发出去就是失败，让 CI 直接标红
    return 0 if n > 0 else 1


def cmd_daemon(cfg):
    interval = int(cfg["check_interval_seconds"])
    log("daemon 模式启动，检查间隔 %d 秒" % interval)
    while True:
        try:
            cmd_check(load_config())
        except Exception as e:  # noqa: BLE001
            log("检查过程异常: %s" % e)
        time.sleep(interval)


_LOCK_FH = None  # 保持文件句柄存活，flock 随进程退出自动释放


def acquire_lock():
    global _LOCK_FH
    _LOCK_FH = open(os.path.join(BASE_DIR, ".lock"), "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("另一个实例正在运行，本次跳过")
        sys.exit(0)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    cfg = load_config()
    if cmd in ("check", "daemon"):
        acquire_lock()
    if cmd == "check":
        sys.exit(cmd_check(cfg))
    elif cmd == "status":
        sys.exit(cmd_status(cfg))
    elif cmd == "test-notify":
        sys.exit(cmd_test_notify(cfg))
    elif cmd == "daemon":
        cmd_daemon(cfg)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
