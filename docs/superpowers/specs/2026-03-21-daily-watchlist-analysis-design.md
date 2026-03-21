# 每日自选股自动分析 — 设计文档

**日期：** 2026-03-21
**状态：** 已用户确认，Spec 审查 v2

---

## 1. 目标

每天早上 6:00（本地时间），自动从飞书电子表格读取自选股列表，逐股运行 TradingAgents 分析，将结果写入飞书多维表格，并通过 Webhook 推送汇总通知（含多维表格链接）。

---

## 2. 飞书权限申请（首次部署必须）

在[飞书开放平台控制台](https://open.feishu.cn)为 App `cli_a91481e969b81bdb` 开通以下权限：

| 权限标识 | 用途 |
|---------|------|
| `sheets:spreadsheet:readonly` | 读取电子表格（自选股列表） |
| `bitable:app` | 读写多维表格（分析报告） |
| `docx:document` | 在「我的空间」创建文档 |

> **注意**：自建应用创建的文档归属 App Bot，不是用户个人空间，需在「我的空间」手动共享给应用，或使用 `space/v1` API 创建到指定空间。

---

## 3. 飞书文档结构

### 3.1 电子表格：《自选股列表》（用户维护）

| 列 | 字段名 | 示例 | 说明 |
|----|--------|------|------|
| A | 股票代码 | 000977 | A股6位 / 美股Ticker / 港股代码.HK |
| B | 股票名称 | 浪潮信息 | 可留空，脚本不依赖此列 |
| C | 市场 | A股 | A股 / 美股 / 港股 |
| D | 备注 | AI服务器 | 可选，脚本忽略 |

- 第1行为表头，从第2行起为数据
- 空行自动跳过

### 3.2 多维表格：《TradingAgents 每日分析报告》（脚本写入）

| 字段名 | 飞书字段类型 | 说明 |
|--------|------------|------|
| 日期 | 日期 | 分析日期，精确到天 |
| 股票代码 | 文本 | |
| 股票名称 | 文本 | |
| 市场 | 单选 | A股 / 美股 / 港股 |
| 决策 | 单选 | 买入 / 持有 / 卖出 / 谨慎增持 / 谨慎减持 |
| 目标价 | 数字 | SignalProcessor 解析结果（元/美元） |
| 1月目标价 | 数字 | 从原文正则提取，无则留空 |
| 3月目标价 | 数字 | 从原文正则提取，无则留空 |
| 止损位 | 数字 | 从原文正则提取，无则留空 |
| 分析摘要 | 多行文本 | decision["reasoning"]，300字以内 |

---

## 4. 文件结构

```
TradingAgents-CN/
  scripts/
    daily_watchlist_analysis.py   # 主入口：读表→分析→写表→通知
    feishu_client.py              # 飞书 OpenAPI 封装
    init_feishu_docs.py           # 一次性初始化：创建文档，写入 config
  config/
    watchlist_config.json         # 自动生成（已加入 .gitignore）
                                  # 存储 spreadsheetToken、bitableToken、tableId
```

`config/watchlist_config.json` 必须加入 `.gitignore`，防止飞书文档 Token 泄露。

---

## 5. 执行流程

```
CronCreate (每天 06:00 本地时间)
    │
    ├─ 0. 交易日检查（非交易日直接退出，不发通知）
    │
    ├─ 1. 读取 config/watchlist_config.json
    │       → spreadsheetToken, bitableToken, tableId, bitable_url
    │
    ├─ 2. feishu_client.get_watchlist(spreadsheetToken)
    │       → [(code, name, market), ...]
    │
    ├─ 3. for each stock:
    │       a. TradingAgentsGraph.propagate(code, today)
    │          → (final_state, decision)
    │       b. 主路径：直接取 decision["action"]、decision["target_price"]、decision["reasoning"]
    │          补充正则：从 final_state["final_trade_decision"] 提取 1月/3月目标价、止损位
    │       c. 写入前去重：查询多维表格当日该股票是否已有记录，有则跳过
    │       d. feishu_client.append_bitable_record(...)
    │
    ├─ 4. feishu_client.send_webhook(summary_text, bitable_url)
    │
    └─ 5. 记录日志到 logs/daily_analysis.log
```

---

## 6. 关键实现细节

### 6.1 飞书 Token 管理

```python
class FeishuClient:
    def __init__(self):
        self._token = None
        self._token_fetched_at = 0

    def get_token(self):
        # 距上次获取超过 110 分钟则刷新（有效期 2 小时）
        if time.time() - self._token_fetched_at > 6600:
            resp = requests.post(TOKEN_URL, json={
                "app_id": APP_ID,
                "app_secret": APP_SECRET
            })
            self._token = resp.json()["tenant_access_token"]
            self._token_fetched_at = time.time()
        return self._token
```

凭证来源：
- `APP_ID` / `APP_SECRET`：从 `~/.openclaw/openclaw.json` → `channels.feishu` 读取
- `FEISHU_WEBHOOK_URL`：从 `.env` 读取

### 6.2 决策解析

```python
# 主路径：使用 SignalProcessor 已解析的结构化结果
state, decision = ta.propagate(code, today)
action      = decision.get("action", "未知")           # 买入/持有/卖出
target      = decision.get("target_price")             # 目标价
reasoning   = decision.get("reasoning", "")            # 摘要

# 补充正则：从原文提取 1月/3月目标价、止损位
raw_text = state.get("final_trade_decision", "")
target_1m  = extract_price(raw_text, r"1[个月].*?(\d+\.?\d*)[元¥]")
target_3m  = extract_price(raw_text, r"3[个月].*?(\d+\.?\d*)[元¥]")
stop_loss  = extract_price(raw_text, r"止损.*?(\d+\.?\d*)[元¥]")
```

### 6.3 交易日检查

```python
import akshare as ak
def is_trading_day(date_str: str) -> bool:
    try:
        cal = ak.tool_trade_date_hist_sina()
        return date_str in cal["trade_date"].astype(str).values
    except Exception:
        return True  # 获取失败时保守地继续运行
```

### 6.4 重复写入去重

写入多维表格前，先查询当日该股票是否已存在记录：

```python
records = feishu_client.query_bitable(bitable_token, table_id,
    filter={"date": today, "stock_code": code})
if records:
    logger.info(f"{code} 今日已有记录，跳过")
    continue
```

### 6.5 错误处理

- 单只股票分析失败：记录错误，继续下一只，在 Webhook 通知中用 ⚠️ 标注
- 飞书 API 失败：重试 3 次（指数退避），仍失败则写本地日志
- 非交易日：静默退出，不发任何通知

### 6.6 Webhook 通知格式

```
📊 TradingAgents 今日分析完成（2026-03-21）

✅ 000977 浪潮信息 → 持有（目标价 ¥68.00）
✅ 600036 招商银行 → 买入（目标价 ¥42.50）
⚠️ 000001 平安银行 → 分析失败（网络超时）

📋 查看完整报告：https://feishu.cn/...
⏱ 耗时：18分32秒  共 3 只，成功 2 只
```

---

## 7. 初始化流程（首次运行一次）

```bash
python scripts/init_feishu_docs.py
```

脚本执行：
1. 检查 `config/watchlist_config.json` 是否存在，**若存在则提示确认后才继续**（防止误操作覆盖）
2. 调用飞书 API 创建电子表格《自选股列表》，写入示例表头和一行示例数据
3. 调用飞书 API 创建多维表格《TradingAgents 每日分析报告》，创建所有字段
4. 将 token 写入 `config/watchlist_config.json`
5. 打印两个文档访问链接

用户在飞书中编辑《自选股列表》填入股票后，即可等待第二天 6:00 自动运行。

---

## 8. Cron 注册

通过直接写入 `~/.openclaw/cron/jobs.json` 注册持久化定时任务（openclaw 原生机制，永久有效，无需续期）。

**jobs.json 条目格式：**
```json
{
  "id": "tradingagents-daily-analysis",
  "agentId": "main",
  "name": "TradingAgents 每日自选股分析",
  "enabled": true,
  "schedule": {
    "kind": "cron",
    "expr": "0 6 * * *",
    "tz": "Asia/Shanghai"
  },
  "sessionTarget": "isolated",
  "wakeMode": "next-heartbeat",
  "payload": {
    "kind": "agentTurn",
    "message": "请执行 /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN/scripts/daily_watchlist_analysis.py 脚本，完成今日自选股分析并推送飞书报告。完成后告诉我执行结果。"
  },
  "delivery": {
    "mode": "announce",
    "channel": "feishu"
  },
  "state": {
    "nextRunAtMs": 1774130400000,
    "lastRunAtMs": null,
    "consecutiveErrors": 0
  }
}
```

**关键字段说明：**
- `schedule.tz`：`Asia/Shanghai`，确保 6:00 是北京时间
- `sessionTarget`：`isolated`，每次在独立 session 中运行
- `delivery.channel`：`feishu`，执行完毕结果通过飞书通知
- `state.nextRunAtMs`：首次运行时间戳（需在写入时动态计算下一个 6:00 AM）

---

## 9. 配置文件说明

| 配置项 | 来源 | 说明 |
|--------|------|------|
| FEISHU_APP_ID | `~/.openclaw/openclaw.json` → `channels.feishu.appId` | 敏感，不入 git |
| FEISHU_APP_SECRET | `~/.openclaw/openclaw.json` → `channels.feishu.appSecret` | 敏感，不入 git |
| FEISHU_WEBHOOK_URL | `.env` → `FEISHU_WEBHOOK_URL` | 已配置 |
| DASHSCOPE_API_KEY | `.env` | 已配置 |
| DASHSCOPE_BASE_URL | `.env` | 已配置 |
| spreadsheetToken | `config/watchlist_config.json` | 初始化后自动写入，加入 .gitignore |
| bitableToken | `config/watchlist_config.json` | 同上 |
| tableId | `config/watchlist_config.json` | 同上 |
