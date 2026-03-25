# 飞书文档分析报告 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每次完成股票分析后，自动在飞书文档（docx）中创建一篇格式化报告，并在多维表格中存储可点击的文档链接，替代原来的纯文本「完整报告」字段。

**Architecture:** 分三步：① `FeishuClient` 新增 `create_feishu_doc` 方法（调用飞书 docx API 创建文档并设置组织内可访问权限）；② 一次性迁移脚本向已有 bitable 添加「报告链接」字段（URL 类型）；③ `daily_watchlist_analysis.py` 集成文档创建，写入「报告链接」并停止写「完整报告」纯文本。文档 URL 通过 `config/watchlist_config.json` 中的 `bitable_url` 提取租户域名来构造（形如 `https://my.feishu.cn/docx/{doc_id}`）。

**Tech Stack:** Python 3, requests, 飞书开放平台 API（docx v1、bitable v1、drive permissions v1）

---

## 文件结构

| 文件 | 改动 |
|------|------|
| `scripts/feishu_client.py` | 新增 `create_feishu_doc(title, text, base_url)` → `str`（返回 doc URL） |
| `scripts/migrate_add_report_link_field.py` | **新建**，一次性：向现有 bitable 添加「报告链接」字段 |
| `scripts/daily_watchlist_analysis.py` | 调用 `create_feishu_doc`，写「报告链接」，移除「完整报告」写入 |

---

## 飞书 API 速查

**创建 docx 文档**
```
POST https://open.feishu.cn/open-apis/docx/v1/documents
Body: {"title": "..."}
Response: {"code":0, "data": {"document": {"document_id": "doxcnXXX", ...}}}
```

**向文档写入内容块（添加到根块）**
```
POST https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children
Body: {"children": [{block}, ...], "index": 0}
```

段落块格式：
```json
{"block_type": 2, "paragraph": {"elements": [{"text_run": {"content": "text"}}]}}
```
二级标题块格式：
```json
{"block_type": 4, "heading2": {"elements": [{"text_run": {"content": "title"}}]}}
```

**设置组织内链接可读权限**
```
PATCH https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public?type=docx
Body: {"link_share_entity": "tenant_readable"}
```

**bitable URL 字段写入格式（type=15）**
```json
{"link": "https://...", "text": "查看报告"}
```

---

### Task 1: FeishuClient.create_feishu_doc

**Files:**
- Modify: `scripts/feishu_client.py`
- Test: `tests/scripts/test_feishu_client_doc.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/scripts/test_feishu_client_doc.py`：
```python
import pytest
from unittest.mock import patch, MagicMock

# 让测试不依赖真实凭据
@pytest.fixture(autouse=True)
def mock_credentials(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp")
    with patch("scripts.feishu_client._load_feishu_credentials", return_value=("id", "secret")):
        yield

def _mock_post(responses: list):
    """依次返回 responses 中的 json 数据"""
    side_effects = []
    for r in responses:
        m = MagicMock()
        m.raise_for_status = MagicMock()
        m.json.return_value = r
        side_effects.append(m)
    return side_effects

def test_create_feishu_doc_returns_url():
    from scripts.feishu_client import FeishuClient
    client = FeishuClient.__new__(FeishuClient)
    client._token = "tok"
    client._token_fetched_at = 9999999999
    client._app_id = "id"
    client._app_secret = "secret"
    client._webhook_url = ""

    base_url = "https://my.feishu.cn"
    with patch("requests.post") as mock_post, \
         patch("requests.patch") as mock_patch:

        mock_post.side_effect = _mock_post([
            # 1. 创建文档
            {"code": 0, "data": {"document": {"document_id": "doxcnABC"}}},
            # 2. 写入内容块
            {"code": 0},
        ])
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock(),
                                             json=MagicMock(return_value={"code": 0}))

        url = client.create_feishu_doc("000977 浪潮信息 2026-03-22", "报告内容", base_url)

    assert url == "https://my.feishu.cn/docx/doxcnABC"

def test_create_feishu_doc_converts_headings():
    """## 开头的行应被识别为标题块，其余为段落块"""
    from scripts.feishu_client import _text_to_blocks
    blocks = _text_to_blocks("## 一、市场分析\n正文内容\n\n## 二、风险\n风险内容")
    assert blocks[0]["block_type"] == 4   # heading2
    assert blocks[1]["block_type"] == 2   # paragraph
    assert blocks[2]["block_type"] == 4   # heading2
    assert blocks[3]["block_type"] == 2   # paragraph
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/wenxiaocheng/.openclaw/workspace/TradingAgents-CN
python -m pytest tests/scripts/test_feishu_client_doc.py -v
```
期望：`ImportError: cannot import name '_text_to_blocks'` 或 `AttributeError: 'FeishuClient' object has no attribute 'create_feishu_doc'`

- [ ] **Step 3: 实现 `_text_to_blocks` 和 `create_feishu_doc`**

在 `scripts/feishu_client.py` 末尾追加（在 `update_bitable_record` 之后）：

```python
def _text_to_blocks(text: str) -> list:
    """将纯文本转换为飞书 docx 内容块列表。
    ## 开头的行 → heading2 块；其他非空行 → paragraph 块。
    """
    blocks = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            content = stripped[3:].strip()
            blocks.append({
                "block_type": 4,
                "heading2": {"elements": [{"text_run": {"content": content}}]}
            })
        elif stripped.startswith("# "):
            content = stripped[2:].strip()
            blocks.append({
                "block_type": 3,
                "heading1": {"elements": [{"text_run": {"content": content}}]}
            })
        else:
            blocks.append({
                "block_type": 2,
                "paragraph": {"elements": [{"text_run": {"content": stripped}}]}
            })
    return blocks
```

在 `FeishuClient` 类末尾（`update_bitable_record` 之后）追加：

```python
    def create_feishu_doc(self, title: str, text: str, base_url: str) -> str:
        """在飞书创建一篇 docx 文档，写入分析报告内容，设置组织内链接可读，返回文档 URL。

        Args:
            title: 文档标题，例如 "000977 浪潮信息 2026-03-22 分析报告"
            text: 报告正文（纯文本，支持 ## 标题）
            base_url: 飞书租户域名，例如 "https://my.feishu.cn"
        Returns:
            文档 URL，例如 "https://my.feishu.cn/docx/doxcnXXX"
        """
        # 1. 创建文档
        resp = requests.post(
            "https://open.feishu.cn/open-apis/docx/v1/documents",
            json={"title": title},
            headers=self._headers(), timeout=15
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"创建飞书文档失败: {result}")
        doc_id = result["data"]["document"]["document_id"]
        logger.info(f"飞书文档创建成功: {doc_id}")

        # 2. 写入内容块（每批最多 50 块，避免超限）
        blocks = _text_to_blocks(text)
        batch_size = 50
        for i in range(0, len(blocks), batch_size):
            batch = blocks[i:i + batch_size]
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                json={"children": batch, "index": i},
                headers=self._headers(), timeout=15
            )
            resp.raise_for_status()
            r = resp.json()
            if r.get("code") != 0:
                logger.warning(f"写入文档块失败（批次 {i}）: {r}")

        # 3. 设置组织内链接可读
        try:
            resp = requests.patch(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public",
                params={"type": "docx"},
                json={"link_share_entity": "tenant_readable"},
                headers=self._headers(), timeout=10
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"设置文档权限失败（不影响文档创建）: {e}")

        doc_url = f"{base_url}/docx/{doc_id}"
        logger.info(f"飞书文档 URL: {doc_url}")
        return doc_url
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/scripts/test_feishu_client_doc.py -v
```
期望：2 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/feishu_client.py tests/scripts/test_feishu_client_doc.py
git commit -m "feat: FeishuClient 新增 create_feishu_doc，支持创建飞书文档报告"
```

---

### Task 2: 迁移脚本——向 bitable 添加「报告链接」字段

**Files:**
- Create: `scripts/migrate_add_report_link_field.py`

> 注：此脚本是一次性运行的，无需写自动化测试，但要在本地手动执行验证。

- [ ] **Step 1: 创建迁移脚本**

新建 `scripts/migrate_add_report_link_field.py`：

```python
#!/usr/bin/env python3
"""
一次性迁移：向现有 bitable 添加「报告链接」字段（URL 类型，type=15）
运行：python scripts/migrate_add_report_link_field.py
"""
import os
import sys
import json
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "watchlist_config.json")


def main():
    from scripts.feishu_client import FeishuClient
    client = FeishuClient()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    bitable_token = config["bitable_token"]
    table_id = config["table_id"]

    # 检查「报告链接」字段是否已存在
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}/tables/{table_id}/fields",
        headers=client._headers(), timeout=10
    )
    resp.raise_for_status()
    fields = resp.json().get("data", {}).get("items", [])
    existing_names = [f["field_name"] for f in fields]
    logger.info(f"现有字段: {existing_names}")

    if "报告链接" in existing_names:
        logger.info("「报告链接」字段已存在，无需迁移")
        return

    # 创建「报告链接」字段（type=15 为 URL 类型）
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}/tables/{table_id}/fields",
        json={"field_name": "报告链接", "type": 15},
        headers=client._headers(), timeout=10
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") == 0:
        logger.info("✅ 「报告链接」字段创建成功")
    else:
        logger.error(f"创建字段失败: {result}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 手动运行迁移脚本**

```bash
python scripts/migrate_add_report_link_field.py
```
期望输出：`✅ 「报告链接」字段创建成功`
（若已存在：`「报告链接」字段已存在，无需迁移`）

- [ ] **Step 3: 提交**

```bash
git add scripts/migrate_add_report_link_field.py
git commit -m "feat: 迁移脚本——向 bitable 添加「报告链接」URL 字段"
```

---

### Task 3: daily_watchlist_analysis 集成文档创建

**Files:**
- Modify: `scripts/daily_watchlist_analysis.py`

- [ ] **Step 1: 提取 base_url，集成文档创建**

在 `run_analysis` 函数中，找到读取 config 的位置（约第 75-78 行），在读取 `bitable_url` 之后添加：

```python
# 从 bitable_url 提取租户域名（例如 https://my.feishu.cn）
base_url = bitable_url.split("/base/")[0]
```

找到逐股分析的 `else` 分支（原来写入多维表格的部分，约第 129-149 行），在 `action`/`target`/`raw_text` 提取完成之后、去重检查之前，加入文档创建：

```python
            # 创建飞书文档（失败不影响主流程）
            doc_url = None
            if raw_text:
                try:
                    doc_title = f"{code} {name} {today} 分析报告".strip()
                    doc_url = feishu.create_feishu_doc(doc_title, raw_text, base_url)
                except Exception as doc_err:
                    logger.warning(f"创建飞书文档失败（不影响写入）: {doc_err}")
```

在 `fields` 字典构建时，移除 `"完整报告": raw_text`，改为加入 `"报告链接"`：

```python
                fields = {
                    "文本": f"{code} {name}".strip(),
                    "日期": int(datetime.strptime(today, "%Y-%m-%d").timestamp() * 1000),
                    "股票代码": code,
                    "股票名称": name,
                    "市场": market,
                    "决策": action,
                    "分析摘要": reasoning,
                }
                if doc_url:
                    fields["报告链接"] = {"link": doc_url, "text": "查看完整报告"}
                if target is not None:
                    fields["目标价"] = float(target)
                if target_1m is not None:
                    fields["1月目标价"] = target_1m
                if target_3m is not None:
                    fields["3月目标价"] = target_3m
                if stop_loss is not None:
                    fields["止损位"] = stop_loss
                feishu.append_bitable_record(bitable_token, table_id, fields)
```

在 `if existing:` 分支中，将补写逻辑更新为补写「报告链接」：

```python
            if existing:
                record_id = existing[0].get("record_id")
                existing_fields = existing[0].get("fields", {})
                if record_id and not existing_fields.get("报告链接") and doc_url:
                    feishu.update_bitable_record(bitable_token, table_id, record_id,
                                                 {"报告链接": {"link": doc_url, "text": "查看完整报告"},
                                                  "文本": f"{code} {name}".strip()})
                    logger.info(f"{code} 今日已有记录，已补写报告链接")
                else:
                    logger.info(f"{code} 今日已有记录，跳过写入")
```

完整修改后的逐股分析核心段落如下（供参考）：

```python
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

            # 创建飞书文档（失败不影响主流程）
            doc_url = None
            if raw_text:
                try:
                    doc_title = f"{code} {name} {today} 分析报告".strip()
                    doc_url = feishu.create_feishu_doc(doc_title, raw_text, base_url)
                except Exception as doc_err:
                    logger.warning(f"创建飞书文档失败（不影响写入）: {doc_err}")

            # 去重检查
            existing = feishu.query_bitable_records(bitable_token, table_id, today, code)
            if existing:
                record_id = existing[0].get("record_id")
                existing_fields = existing[0].get("fields", {})
                if record_id and not existing_fields.get("报告链接") and doc_url:
                    feishu.update_bitable_record(bitable_token, table_id, record_id,
                                                 {"报告链接": {"link": doc_url, "text": "查看完整报告"},
                                                  "文本": f"{code} {name}".strip()})
                    logger.info(f"{code} 今日已有记录，已补写报告链接")
                else:
                    logger.info(f"{code} 今日已有记录，跳过写入")
            else:
                fields = {
                    "文本": f"{code} {name}".strip(),
                    "日期": int(datetime.strptime(today, "%Y-%m-%d").timestamp() * 1000),
                    "股票代码": code,
                    "股票名称": name,
                    "市场": market,
                    "决策": action,
                    "分析摘要": reasoning,
                }
                if doc_url:
                    fields["报告链接"] = {"link": doc_url, "text": "查看完整报告"}
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
```

- [ ] **Step 2: 手动冒烟测试（--force 跳过交易日检查）**

```bash
python scripts/daily_watchlist_analysis.py --force 2>&1 | tail -30
```
期望输出：
```
INFO 飞书文档创建成功: doxcnXXX
INFO 飞书文档 URL: https://my.feishu.cn/docx/doxcnXXX
INFO 写入多维表格成功: 000977
INFO ✅ 000977 浪潮信息 → 买入（目标 ¥75.00）
INFO ===== 分析完成 =====
```

然后打开飞书，在多维表格中点击记录详情，确认「报告链接」字段显示可点击链接，点击后跳转到飞书文档，报告内容正常显示。

- [ ] **Step 3: 提交**

```bash
git add scripts/daily_watchlist_analysis.py
git commit -m "feat: 分析后自动创建飞书文档，bitable 存储报告链接替代纯文本"
```

---

## 常见问题

**Q: 文档权限设置失败**
A: 权限 API 失败不中断流程（try/except），URL 仍会正常写入 bitable。若文档无法访问，需手动在飞书后台将 App 创建的文档设置为"组织内可读"。

**Q: bitable「报告链接」字段写入格式报错**
A: 飞书 URL 字段（type=15）的写入格式为 `{"link": "...", "text": "..."}` 。若 API 返回 `field_value format error`，可改为 type=1（纯文本字段），写入 `doc_url` 字符串即可。

**Q: 文档内容写入失败（blocks API 报错）**
A: 内容块 API 失败不中断流程，文档已创建（只是空文档），URL 仍有效。可查看日志中的具体错误码排查。
