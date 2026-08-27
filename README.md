# BTC 均线监控

监测 BTC 价格与关键 EMA 均线的关系，抵达时自动推送提醒到微信或 Telegram。

**监控目标**

| 级别 | 均线 |
|------|------|
| 日线 | EMA120、EMA200、EMA350 |
| 周线 | EMA250、EMA350 |

数据源：Binance BTCUSDT（主）→ OKX（备用），EMA 计算方式与 TradingView 上币安
BTCUSDT 图表一致（含当前未收盘K线）。纯 Python 标准库，无需安装任何依赖。

**两种运行方式**（二选一，也可同时用）：

| | 本机 launchd | GitHub Actions |
|---|---|---|
| 需要电脑开着 | ✅ 是，Mac 睡眠时不检查 | ❌ 否，云端 7×24 运行 |
| 费用 | 免费 | 免费（见下方额度说明） |
| 检查频率 | 每 15 分钟，准时 | 每 30 分钟，可能延迟十几分钟 |
| 安装 | `bash install.sh` | 推仓库 + 填一个 Secret |

笔记本用户建议用 GitHub Actions —— macOS 合盖或空闲睡眠时，本机定时任务不会运行。

## 什么算"抵达"（触发提醒的条件，满足其一）

1. 价格进入均线 ±1% 区间（`touch_threshold_pct` 可调）
2. 价格上穿或下穿均线
3. 当日/当周K线的影线扫过均线（同一根K线只提醒一次，避免旧影线反复触发）

触发一次后进入静默：价格离开均线 ±3%（`rearm_pct`）之外才会重新武装，
且同一均线两次提醒至少间隔 6 小时（`cooldown_minutes`），避免价格在均线附近
震荡时刷屏。想要重复提醒可把 `rearm_pct` 设为 `0`，则只按冷却间隔重复提醒。

## 快速开始（三步）

```bash
# 第 1 步：生成配置文件
bash install.sh

# 第 2 步：编辑 config.json，启用至少一个通知渠道（见下），然后测试
python3 btc_monitor.py test-notify

# 第 3 步：再跑一次 install.sh 安装定时任务（每 15 分钟自动检查）
bash install.sh
```

随时查看当前价格与各均线的距离：

```bash
python3 btc_monitor.py status
```

## 部署到 GitHub Actions（云端 7×24 运行，不用开电脑）

所需文件都已就绪（[.github/workflows/monitor.yml](.github/workflows/monitor.yml)、
[config.ci.json](config.ci.json)、[.gitignore](.gitignore)）。四步：

**1. 建仓库并推代码**（在本目录执行；仓库建议设为 Private）

```bash
git init && git add -A && git commit -m "feat: BTC 均线监控"
```

然后去 <https://github.com/new> 新建一个空仓库，按页面提示执行它给出的
`git remote add origin ...` 和 `git push -u origin main` 两条命令。

**2. 填入密钥**：仓库页面 → Settings → Secrets and variables → Actions →
New repository secret，名字填 `SERVERCHAN_SENDKEY`，值填你的 SendKey。
（用 Telegram 的话再加 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。）

**3. 手动跑一次验证**：仓库页面 → Actions → 左侧「BTC 均线监控」→
Run workflow。看到绿勾即成功，此后每 30 分钟自动运行。

**4.（可选）关掉本机任务**，避免同一条提醒收到两遍：

```bash
bash uninstall.sh
```

### 关于 GitHub 免费额度

- **Private 仓库**：每月 2000 分钟免费。每 30 分钟跑一次 ≈ 1440 次/月，
  每次计费约 1 分钟，刚好够用但余量不多。想更保险，把 monitor.yml 里的
  `*/30 * * * *` 改成 `0 * * * *`（每小时），用量降到约 720 分钟。
- **Public 仓库**：Actions 分钟数无限免费，但代码公开（密钥在 Secrets 里，
  不会泄露）。注意 GitHub 会把「超过 60 天无任何活动」的公开仓库定时任务自动
  停用——本程序每次运行都会提交 state.json，属于仓库活动，正常不会被停；
  真被停用前 GitHub 也会发邮件通知你。

### 几个已经处理好的坑

- **币安封锁美国 IP**：GitHub 的服务器在美国，直连 `api.binance.com` 会返回
  HTTP 451。程序已改为优先走币安官方公开行情镜像
  `data-api.binance.vision`（实测数据与主站逐根完全一致，EMA 差 0.000000%），
  该镜像无地区限制；失败才回退主站，再失败才用 OKX。
- **定时不准**：GitHub 的 cron 常延迟几分钟到十几分钟。这对本程序没有影响——
  判断"抵达"时会检查整根K线的最高价/最低价，两次运行之间发生的触碰仍会被捕获。
- **云端无状态**：每次运行完会把 state.json 提交回仓库，这样"哪条均线已经提醒
  过、还在冷却中"的记忆能跨次运行保留，不会重复轰炸。
- **密钥安全**：[.gitignore](.gitignore) 已排除本机的 config.json（里面有明文
  SendKey）。云端用的是 config.ci.json，不含任何密钥，密钥只从 Secrets 注入。

## 通知渠道配置（config.json 的 notify 段）

### 方式一：微信推送（推荐 Server酱）

微信个人号不开放程序发消息的接口，但 **Server酱** 通过微信公众号/服务号推送，
效果就是微信里收到提醒：

1. 打开 <https://sct.ftqq.com>，用微信扫码登录
2. 复制你的 **SendKey**（形如 `SCT100000XXXXXXXX`）
3. 填入 config.json：`"serverchan": {"enabled": true, "sendkey": "SCT..."}`
4. 免费版每天 5 条额度，对本监控（触发才发）完全够用

备选 **PushPlus**（<https://www.pushplus.plus>，同样微信扫码取 token）：
`"pushplus": {"enabled": true, "token": "..."}`

> 真正的手机短信（SMS）需要购买阿里云/腾讯云短信服务，按条收费且需要企业签名
> 备案，个人提醒场景不划算，故未采用。

### 方式二：Telegram 机器人

1. 在 Telegram 找 **@BotFather** 发送 `/newbot` 创建机器人，得到 `bot_token`
2. 找 **@userinfobot** 发一条消息，得到你的数字 `chat_id`
3. **先给你的新机器人发一条任意消息**（否则机器人无法主动发给你）
4. 填入 config.json：

```json
"telegram": {
  "enabled": true,
  "bot_token": "123456:ABC-DEF...",
  "chat_id": "123456789",
  "proxy": "http://127.0.0.1:7890"
}
```

大陆网络需要在 `proxy` 里填本机代理地址（Clash 默认 `http://127.0.0.1:7890`）；
能直连 Telegram 的网络该项留空即可。

### macOS 本机通知

默认开启（`"macos": {"enabled": true}`），触发时这台 Mac 会弹系统通知，
用于本机测试，人不在电脑前时无用，可关闭。

## 常用命令

```bash
python3 btc_monitor.py status       # 查看当前价格与各均线距离
python3 btc_monitor.py check        # 手动执行一次检查
python3 btc_monitor.py test-notify  # 测试通知渠道
python3 btc_monitor.py daemon       # 前台循环运行（替代 launchd）
bash uninstall.sh                   # 卸载定时任务
tail -f monitor.log                 # 查看运行日志
```

## 注意事项

- launchd 定时任务只在这台 Mac 开机且未睡眠时运行。合盖或空闲睡眠期间不检查，
  唤醒后会立刻补跑一次。若这台 Mac 经常睡眠，请改用上面的 GitHub Actions 方案。
  查看本机睡眠计时：`pmset -g | grep " sleep"`。
- 周线 EMA350 需要 350 周（约 6.7 年）数据，Binance 数据从 2017 年 8 月开始
  （约 470 周），与 TradingView 币安 BTCUSDT 图表的算法和数据一致，数值吻合；
  但与 Bitstamp 等更长历史的图表相比会略有出入，属正常现象。
- 所有状态存在 `state.json`，日志在 `monitor.log`，删除它们不影响程序，
  只会重置提醒静默状态。
