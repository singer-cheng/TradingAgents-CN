# 每日自选股自动分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每天早上 6:00（北京时间）自动从飞书电子表格读取自选股，逐股运行 TradingAgents 分析，将结果写入飞书多维表格，并通过 Webhook 推送汇总通知。

**Architecture:** FeishuClient 封装飞书 OpenAPI（token 管理、电子表格读取、多维表格读写、webhook），init 脚本一次性创建飞书文档并保存 token 到 config，主脚本每日读取 watchlist → 分析 → 写入报告 → 推送通知，openclaw cron 每天 06:00 触发主脚本。

**Tech Stack:** Python 3.11, requests, akshare (交易日判断), TradingAgentsGraph (股票分析), Feishu OpenAPI v3, openclaw cron (jobs.json)

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `scripts/feishu_client.py` | 飞书 OpenAPI 封装：token 管理、读表格、写多维表、发 webhook |
| `scripts/init_feishu_docs.py` | 一次性初始化：创建飞书电子表格 + 多维表格，写入 config |
| `scripts/daily_watchlist_analysis.py` | 主入口：交易日判断 → 读 watchlist → 分析 → 写报告 → 发通知 |
| `config/watchlist_config.json` | 自动生成（.gitignore），存储 spreadsheetToken、bitableToken、tableId |

---

### Task 1: FeishuClient — 基础封装（token + webhook）

**Files:**
- Create: `scripts/feishu_client.py`

- [ ] **Step 1: 创建 feishu_client.py，实现 token 管理和 webhook 发送**

```python
# scripts/feishu_client.py
import os
import re
import json
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

def _load_feishu_credentials():
    """从 ~/.openclaw/openclaw.json 读取飞书 App ID 和 App Secret"""
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    feishu = data["channels"]["feishu"]
    return feishu["appId"], feishu["appSecret"]


class FeishuClient:
    def __init__(self):
        self._token: Optional[str] = None
        self._token_fetched_at: float = 0
        self._app_id, self._app_secret = _load_feishu_credentials()
        self._webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")

    def get_token(self) -> str:
        """获取 tenant_access_token，距上次获取超过 110 分钟则刷新"""
        if time.time() - self._token_fetched_at > 6600:
            resp = requests.post(TOKEN_URL, json={
                "app_id": self._app_id,
                "app_secret": self._app_secret
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"获取飞书 token 失败: {data}")
            self._token = data["tenant_access_token"]
            self._token_fetched_at = time.time()
            logger.info("飞书 token 刷新成功")
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}", "Content-Type": "application/json"}

    def send_webhook(self, text: str) -> bool:
        """发送飞书 webhook 消息"""
        if not self._webhook_url:
            logger.warning("FEISHU_WEBHOOK_URL 未配置，跳过 webhook")
            return False
        payload = {"msg_type": "text", "content": {"text": text}}
        for attempt in range(3):
            try:
                resp = requests.post(self._webhook_url, json=payload, timeout=10)
                resp.raise_for_status()
                result = resp.json()
                if result.get("code") == 0 or result.get("StatusCode") == 0:
                    logger.info("Webhook 发送成功")
                    return True
                logger.warning(f"Webhook 响应异常: {result}")
            except Exception as e:
                wait = 2 ** attempt
                logger.warning(f"Webhook 发送失败 (尝试 {attempt+1}/3): {e}, {wait}s 后重试")
                time.sleep(wait)
        logger.error("Webhook 发送最终失败")
        return False
```

- [ ] **Step 2: 确认 credentials 文件存在并可读**

```bash
python3 -c "
import json, os
with open(os.path.expanduser('~/.openclaw/openclaw.json')) as f:
    d = json.load(f)
feishu = d['channels']['feishu']
print('appId:', feishu.get('appId', 'missing'))
print('appSecret:', 'ok' if feishu.get('appSecret') else 'missing')
"
```

预期输出：`appId: cli_a91481e969b81bdb`，appSecret: ok

- [ ] **Step 3: 手动测试 token 获取**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
python3 -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from scripts.feishu_client import FeishuClient
c = FeishuClient()
tok = c.get_token()
print('token 前20:', tok[:20], '...')
"
```

预期：打印 token 前缀，无报错

---

### Task 2: FeishuClient — 电子表格读取

**Files:**
- Modify: `scripts/feishu_client.py`

- [ ] **Step 1: 在 FeishuClient 中增加 get_watchlist 方法**

```python
    def get_watchlist(self, spreadsheet_token: str, sheet_id: str = None) -> list:
        """
        从电子表格读取自选股列表
        返回: [(code, name, market), ...]
        """
        # 若未指定 sheet_id，读取第一个 sheet
        if not sheet_id:
            url = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
            resp = requests.get(url, headers=self._headers(), timeout=10)
            resp.raise_for_status()
            sheets = resp.json()["data"]["sheets"]
            sheet_id = sheets[0]["sheet_id"]

        # 读取数据范围 A2:D200（跳过表头，最多200行）
        range_str = f"{sheet_id}!A2:D200"
        url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rows = data.get("data", {}).get("valueRange", {}).get("values", [])
        result = []
        for row in rows:
            if not row or not row[0]:
                continue
            code = str(row[0]).strip()
            name = str(row[1]).strip() if len(row) > 1 and row[1] else ""
            market = str(row[2]).strip() if len(row) > 2 and row[2] else "A股"
            result.append((code, name, market))
        logger.info(f"读取自选股列表: {len(result)} 只")
        return result
```

- [ ] **Step 2: 确认 API 格式正确（无 spreadsheet token 可暂跳过，init 后验证）**

---

### Task 3: FeishuClient — 多维表格读写

**Files:**
- Modify: `scripts/feishu_client.py`

- [ ] **Step 1: 增加 query_bitable_records（去重查询）和 append_bitable_record（写入）方法**

```python
    def query_bitable_records(self, bitable_token: str, table_id: str,
                               date_str: str, stock_code: str) -> list:
        """查询多维表格中当日该股票是否已有记录，用于去重"""
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}"
               f"/tables/{table_id}/records/search")
        payload = {
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {"field_name": "股票代码", "operator": "is", "value": [stock_code]},
                    {"field_name": "日期", "operator": "is", "value": [date_str]}
                ]
            },
            "page_size": 1
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("items", [])

    def append_bitable_record(self, bitable_token: str, table_id: str,
                               fields: dict) -> bool:
        """向多维表格写入一条记录，失败时重试 3 次"""
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}"
               f"/tables/{table_id}/records")
        for attempt in range(3):
            try:
                resp = requests.post(url, json={"fields": fields},
                                     headers=self._headers(), timeout=15)
                resp.raise_for_status()
                result = resp.json()
                if result.get("code") == 0:
                    logger.info(f"写入多维表格成功: {fields.get('股票代码', '')}")
                    return True
                logger.warning(f"写入失败响应: {result}")
            except Exception as e:
                wait = 2 ** attempt
                logger.warning(f"写入多维表格失败 (尝试 {attempt+1}/3): {e}, {wait}s 后重试")
                time.sleep(wait)
        logger.error(f"写入多维表格最终失败: {fields.get('股票代码', '')}")
        return False
```

- [ ] **Step 2: 确认 feishu_client.py 语法无误**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
python3 -c "import scripts.feishu_client; print('语法 OK')"
```

预期：`语法 OK`

---

### Task 4: 初始化脚本 init_feishu_docs.py

**Files:**
- Create: `scripts/init_feishu_docs.py`

- [ ] **Step 1: 创建初始化脚本**

```python
#!/usr/bin/env python3
"""
一次性初始化脚本：在飞书创建自选股电子表格和分析报告多维表格
运行：python scripts/init_feishu_docs.py
"""
import os
import sys
import json
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "watchlist_config.json")

def main():
    from scripts.feishu_client import FeishuClient
    client = FeishuClient()

    # 检查 config 是否已存在
    if os.path.exists(CONFIG_PATH):
        ans = input(f"⚠️  {CONFIG_PATH} 已存在，继续将覆盖。确认？(y/N): ").strip().lower()
        if ans != "y":
            print("已取消")
            return

    # 1. 创建电子表格
    logger.info("创建自选股电子表格...")
    resp = requests.post(
        "https://open.feishu.cn/open-apis/sheets/v3/spreadsheets",
        json={"title": "自选股列表"},
        headers=client._headers(), timeout=10
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"创建电子表格失败: {result}")
    spreadsheet_token = result["data"]["spreadsheet"]["spreadsheetToken"]
    spreadsheet_url = result["data"]["spreadsheet"]["url"]
    logger.info(f"电子表格创建成功: {spreadsheet_url}")

    # 获取 sheet_id（需单独查询，创建接口不返回 sheets 数组）
    resp2 = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        headers=client._headers(), timeout=10
    )
    resp2.raise_for_status()
    sheets = resp2.json()["data"]["sheets"]
    sheet_id = sheets[0]["sheet_id"]

    # 写入表头和示例数据
    range_str = f"{sheet_id}!A1:D2"
    requests.put(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        json={"valueRange": {"range": range_str, "values": [
            ["股票代码", "股票名称", "市场", "备注"],
            ["000977", "浪潮信息", "A股", "示例"]
        ]}},
        headers=client._headers(), timeout=10
    )

    # 2. 创建多维表格 App
    logger.info("创建分析报告多维表格...")
    resp = requests.post(
        "https://open.feishu.cn/open-apis/bitable/v1/apps",
        json={"name": "TradingAgents 每日分析报告"},
        headers=client._headers(), timeout=10
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"创建多维表格失败: {result}")
    bitable_token = result["data"]["app"]["app_token"]
    bitable_url = result["data"]["app"]["url"]
    logger.info(f"多维表格创建成功: {bitable_url}")

    # 获取默认 table ID
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}/tables",
        headers=client._headers(), timeout=10
    )
    table_id = resp.json()["data"]["items"][0]["table_id"]

    # 3. 创建字段
    fields = [
        {"field_name": "日期", "type": 5},          # 日期
        {"field_name": "股票代码", "type": 1},       # 文本
        {"field_name": "股票名称", "type": 1},
        {"field_name": "市场", "type": 3},           # 单选
        {"field_name": "决策", "type": 3},
        {"field_name": "目标价", "type": 2},         # 数字
        {"field_name": "1月目标价", "type": 2},
        {"field_name": "3月目标价", "type": 2},
        {"field_name": "止损位", "type": 2},
        {"field_name": "分析摘要", "type": 1},
    ]
    for field in fields[1:]:  # 跳过第一个（默认已有名称字段）
        requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}/tables/{table_id}/fields",
            json=field, headers=client._headers(), timeout=10
        )
    logger.info("多维表格字段创建完成")

    # 4. 写入 config
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    config = {
        "spreadsheet_token": spreadsheet_token,
        "spreadsheet_url": spreadsheet_url,
        "bitable_token": bitable_token,
        "bitable_url": bitable_url,
        "table_id": table_id
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    logger.info(f"配置已写入: {CONFIG_PATH}")

    print("\n✅ 初始化完成！")
    print(f"   自选股电子表格: {spreadsheet_url}")
    print(f"   分析报告多维表格: {bitable_url}")
    print("\n请在飞书中将这两个文档共享给应用（App Bot）后，再运行主分析脚本。")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 确认脚本语法**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
python3 -m py_compile scripts/init_feishu_docs.py && echo "语法 OK"
```

---

### Task 5: 主分析脚本 daily_watchlist_analysis.py

**Files:**
- Create: `scripts/daily_watchlist_analysis.py`

- [ ] **Step 1: 创建主分析脚本**

```python
#!/usr/bin/env python3
"""
每日自选股自动分析主脚本
每天 06:00 由 openclaw cron 触发
"""
import os
import sys
import re
import json
import time
import logging
from datetime import datetime
from typing import Optional

# 项目根目录加入 Python 路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# 配置日志
log_dir = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "daily_analysis.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "watchlist_config.json")


def is_trading_day(date_str: str) -> bool:
    """检查给定日期是否为 A 股交易日"""
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        return date_str in cal["trade_date"].astype(str).values
    except Exception as e:
        logger.warning(f"交易日检查失败（保守继续）: {e}")
        return True


def extract_price(text: str, pattern: str) -> Optional[float]:
    """从原始文本中用正则提取价格"""
    m = re.search(pattern, text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def run_analysis():
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"===== TradingAgents 每日分析启动：{today} =====")

    # 0. 交易日检查
    if not is_trading_day(today):
        logger.info(f"{today} 非交易日，退出")
        return

    # 1. 读取配置
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"配置文件不存在: {CONFIG_PATH}，请先运行 init_feishu_docs.py")
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    spreadsheet_token = config["spreadsheet_token"]
    bitable_token = config["bitable_token"]
    bitable_url = config["bitable_url"]
    table_id = config["table_id"]

    # 2. 读取自选股列表
    from scripts.feishu_client import FeishuClient
    feishu = FeishuClient()
    watchlist = feishu.get_watchlist(spreadsheet_token)
    if not watchlist:
        logger.warning("自选股列表为空，退出")
        return
    logger.info(f"自选股列表：{len(watchlist)} 只 — {[c[0] for c in watchlist]}")

    # 3. 初始化 TradingAgents
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG
    ta_config = DEFAULT_CONFIG.copy()
    ta_config["backend_url"] = os.getenv("DASHSCOPE_BASE_URL",
                                          "https://dashscope.aliyuncs.com/compatible-mode/v1")
    ta_config["deep_think_llm"] = os.getenv("DASHSCOPE_MODEL", "qwen-plus-latest")
    ta_config["quick_think_llm"] = os.getenv("DASHSCOPE_MODEL", "qwen-plus-latest")
    ta = TradingAgentsGraph(config=ta_config)

    # 4. 逐股分析
    start_time = time.time()
    results = []
    for code, name, market in watchlist:
        logger.info(f"开始分析 {code} {name} ({market})")
        try:
            final_state, decision = ta.propagate(code, today)

            action = decision.get("action", "未知")
            target = decision.get("target_price")
            reasoning = (decision.get("reasoning") or "")[:300]

            raw_text = final_state.get("final_trade_decision", "")
            target_1m = extract_price(raw_text, r"1[个月].*?(\d+\.?\d*)[元¥]")
            target_3m = extract_price(raw_text, r"3[个月].*?(\d+\.?\d*)[元¥]")
            stop_loss = extract_price(raw_text, r"止损.*?(\d+\.?\d*)[元¥]")

            # 去重检查
            existing = feishu.query_bitable_records(bitable_token, table_id, today, code)
            if existing:
                logger.info(f"{code} 今日已有记录，跳过写入")
            else:
                # 写入多维表格
                fields = {
                    "日期": int(datetime.strptime(today, "%Y-%m-%d").timestamp() * 1000),
                    "股票代码": code,
                    "股票名称": name,
                    "市场": market,
                    "决策": action,
                    "分析摘要": reasoning,
                }
                if target is not None:
                    fields["目标价"] = float(target)
                if target_1m is not None:
                    fields["1月目标价"] = target_1m
                if target_3m is not None:
                    fields["3月目标价"] = target_3m
                if stop_loss is not None:
                    fields["止损位"] = stop_loss
                feishu.append_bitable_record(bitable_token, table_id, fields)

            price_str = f"¥{target:.2f}" if target else "N/A"
            results.append(("ok", code, name, action, price_str, None))
            logger.info(f"✅ {code} {name} → {action}（目标 {price_str}）")

        except Exception as e:
            logger.error(f"❌ {code} 分析失败: {e}", exc_info=True)
            results.append(("err", code, name, None, None, str(e)))

    # 5. 构建 Webhook 通知
    elapsed = int(time.time() - start_time)
    minutes, seconds = divmod(elapsed, 60)
    ok_count = sum(1 for r in results if r[0] == "ok")
    err_count = len(results) - ok_count

    lines = [f"📊 TradingAgents 今日分析完成（{today}）\n"]
    for r in results:
        if r[0] == "ok":
            lines.append(f"✅ {r[1]} {r[2]} → {r[3]}（目标价 {r[4]}）")
        else:
            short_err = str(r[5])[:60] if r[5] else "未知错误"
            lines.append(f"⚠️ {r[1]} {r[2]} → 分析失败（{short_err}）")

    lines.append(f"\n📋 查看完整报告：{bitable_url}")
    lines.append(f"⏱ 耗时：{minutes}分{seconds}秒  共 {len(results)} 只，成功 {ok_count} 只")
    if err_count > 0:
        lines.append(f"⚠️ 失败 {err_count} 只，详情见日志")

    summary_text = "\n".join(lines)
    feishu.send_webhook(summary_text)
    logger.info("===== 分析完成 =====")


if __name__ == "__main__":
    run_analysis()
```

- [ ] **Step 2: 确认脚本语法**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
python3 -m py_compile scripts/daily_watchlist_analysis.py && echo "语法 OK"
```

---

### Task 6: 配置文件 .gitignore 更新

**Files:**
- Modify: `.gitignore` (or `config/.gitignore`)

- [ ] **Step 1: 将 watchlist_config.json 加入 .gitignore**

在项目根目录 `.gitignore` 中追加：

```
# 飞书文档 Token（自动生成，含敏感信息）
config/watchlist_config.json
```

- [ ] **Step 2: 验证**

```bash
grep "watchlist_config" /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN/.gitignore
```

预期：输出 `config/watchlist_config.json`

- [ ] **Step 3: 提交代码**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
git add scripts/feishu_client.py scripts/init_feishu_docs.py scripts/daily_watchlist_analysis.py .gitignore
git commit -m "feat: 新增每日自选股自动分析（飞书读取/写入+webhook通知）"
```

---

### Task 7: 注册 openclaw 定时任务

**Files:**
- Modify: `~/.openclaw/cron/jobs.json`

- [ ] **Step 1: 计算下次 06:00 的时间戳，写入 jobs.json**

```python
import json, os, time
from datetime import datetime, timezone

# 计算今天或明天的 06:00 CST（Asia/Shanghai = UTC+8）
now_ts = time.time()
# CST offset = +8h
cst_now = now_ts + 8 * 3600
cst_today = int(cst_now / 86400) * 86400
cst_6am = cst_today + 6 * 3600  # 今天 06:00 CST
if cst_6am - 8 * 3600 <= now_ts:  # 已过 06:00
    cst_6am += 86400              # 用明天的
next_run_ms = int((cst_6am - 8 * 3600) * 1000)  # 转回 UTC 毫秒

jobs_path = os.path.expanduser("~/.openclaw/cron/jobs.json")
with open(jobs_path, "r") as f:
    data = json.load(f)

new_job = {
    "id": "tradingagents-daily-analysis",
    "agentId": "main",
    "name": "TradingAgents 每日自选股分析",
    "enabled": True,
    "schedule": {"kind": "cron", "expr": "0 6 * * *", "tz": "Asia/Shanghai"},
    "sessionTarget": "isolated",
    "wakeMode": "next-heartbeat",
    "payload": {
        "kind": "agentTurn",
        "message": "请执行 /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN/scripts/daily_watchlist_analysis.py 脚本，完成今日自选股分析并推送飞书报告。完成后告诉我执行结果。"
    },
    "delivery": {"mode": "announce", "channel": "feishu"},
    "state": {"nextRunAtMs": next_run_ms, "lastRunAtMs": None, "consecutiveErrors": 0}
}

# 去重：移除已有同名 job
data["jobs"] = [j for j in data["jobs"] if j.get("id") != "tradingagents-daily-analysis"]
data["jobs"].append(new_job)

with open(jobs_path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"✅ 定时任务已注册，下次运行：{datetime.utcfromtimestamp(next_run_ms/1000).strftime('%Y-%m-%d %H:%M:%S UTC')}")
```

运行：

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
python3 -c "
import json, os, time
from datetime import datetime

now_ts = time.time()
cst_now = now_ts + 8 * 3600
cst_today = int(cst_now / 86400) * 86400
cst_6am = cst_today + 6 * 3600
if cst_6am - 8*3600 <= now_ts:
    cst_6am += 86400
next_run_ms = int((cst_6am - 8*3600) * 1000)

jobs_path = os.path.expanduser('~/.openclaw/cron/jobs.json')
with open(jobs_path, 'r') as f:
    data = json.load(f)

new_job = {
    'id': 'tradingagents-daily-analysis',
    'agentId': 'main',
    'name': 'TradingAgents 每日自选股分析',
    'enabled': True,
    'schedule': {'kind': 'cron', 'expr': '0 6 * * *', 'tz': 'Asia/Shanghai'},
    'sessionTarget': 'isolated',
    'wakeMode': 'next-heartbeat',
    'payload': {
        'kind': 'agentTurn',
        'message': '请执行 /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN/scripts/daily_watchlist_analysis.py 脚本，完成今日自选股分析并推送飞书报告。完成后告诉我执行结果。'
    },
    'delivery': {'mode': 'announce', 'channel': 'feishu'},
    'state': {'nextRunAtMs': next_run_ms, 'lastRunAtMs': None, 'consecutiveErrors': 0}
}

data['jobs'] = [j for j in data['jobs'] if j.get('id') != 'tradingagents-daily-analysis']
data['jobs'].append(new_job)

with open(jobs_path, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f'定时任务已注册，下次运行：{datetime.utcfromtimestamp(next_run_ms/1000).strftime(\"%Y-%m-%d %H:%M:%S UTC\")}')
"
```

- [ ] **Step 2: 验证 jobs.json**

```bash
cat ~/.openclaw/cron/jobs.json | python3 -m json.tool | grep -A5 '"tradingagents-daily-analysis"'
```

预期：显示 job 的 id、schedule.expr 和 nextRunAtMs

---

### Task 8: 初始化飞书文档（部署时执行）

> 此任务需要飞书应用已获得相应权限后才能运行。

- [ ] **Step 1: 前置条件 — 在飞书开放平台为 App `cli_a91481e969b81bdb` 开通权限**

需开通（飞书开放平台 → 应用管理 → 权限管理）：
- `sheets:spreadsheet:readonly` （只读电子表格，用于读取自选股列表）
- `sheets:spreadsheet` （写入电子表格，用于初始化时写入表头）
- `bitable:app` （读写多维表格，用于写入分析报告）
- `docx:document` （可选，在「我的空间」创建文档）

> **注意**：自建应用创建的文档归属 App Bot，需在飞书中手动将文档共享给应用，或由应用创建后分享给自己。

- [ ] **Step 2: 运行初始化脚本**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
python3 scripts/init_feishu_docs.py
```

预期：打印两个飞书文档链接，`config/watchlist_config.json` 生成

- [ ] **Step 3: 在飞书中编辑自选股列表**

打开初始化输出的电子表格链接，按格式（代码/名称/市场/备注）填入要监控的股票。

---

### Task 9: 验收测试（端到端）

- [ ] **Step 1: 手动触发一次分析（跳过交易日检查）**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
# 临时注释掉交易日检查，或在非交易日临时改 is_trading_day 返回 True
python3 scripts/daily_watchlist_analysis.py
```

预期：
- 控制台和 `logs/daily_analysis.log` 显示分析进度
- 飞书多维表格新增记录
- 飞书 webhook 收到汇总通知

- [ ] **Step 2: 验证去重（再次运行）**

再次运行 `daily_watchlist_analysis.py`，检查日志：

```
INFO 000977 今日已有记录，跳过写入
```

- [ ] **Step 3: 验证非交易日静默退出**

临时修改 `is_trading_day` 中的 akshare 调用或直接测试非交易日场景（例如传入周末日期）：

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from scripts.daily_watchlist_analysis import is_trading_day
# 2026-03-22 是周日，应为非交易日
print('2026-03-22 是交易日:', is_trading_day('2026-03-22'))
"
```

预期：`2026-03-22 是交易日: False`，main 脚本运行后日志显示"非交易日，退出"，**不发 webhook**。

- [ ] **Step 4: 确认 openclaw cron 已生效**

```bash
cat ~/.openclaw/cron/jobs.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
jobs=[j for j in d['jobs'] if j['id']=='tradingagents-daily-analysis']
if jobs:
    import datetime
    j=jobs[0]
    ts=j['state']['nextRunAtMs']/1000
    print('下次运行:', datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S 本地时间'))
    print('时区:', j['schedule']['tz'])
"
```

预期：显示明天或今天 06:00（北京时间）的本地时间

---

## 快速参考

**执行顺序（首次部署）：**

1. 飞书开放平台开通权限
2. `python3 scripts/init_feishu_docs.py`  — 创建飞书文档
3. 填写飞书自选股列表
4. openclaw cron 注册（Task 7）
5. 手动触发测试（Task 9）

**关键配置路径：**
- 飞书凭证：`~/.openclaw/openclaw.json` → `channels.feishu`
- Webhook URL：`.env` → `FEISHU_WEBHOOK_URL`
- DashScope：`.env` → `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, `DASHSCOPE_MODEL`
- 文档 Token：`config/watchlist_config.json`（勿提交）
