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


def _text_to_blocks(text: str) -> list:
    """将纯文本转换为飞书 docx 内容块列表。
    ## 开头的行 → heading2 块；# 开头的行 → heading1 块；其他非空行 → paragraph 块。
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
                "text": {"elements": [{"text_run": {"content": stripped}}]}
            })
    return blocks


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
        """发送飞书 webhook 消息（text 中已包含 bitable_url）"""
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

    def update_bitable_record(self, bitable_token: str, table_id: str,
                               record_id: str, fields: dict) -> bool:
        """更新多维表格中已有记录的指定字段"""
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}"
               f"/tables/{table_id}/records/{record_id}")
        try:
            resp = requests.put(url, json={"fields": fields},
                                headers=self._headers(), timeout=15)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                logger.info(f"更新多维表格成功: {record_id}")
                return True
            logger.warning(f"更新失败响应: {result}")
        except Exception as e:
            logger.error(f"更新多维表格失败 {record_id}: {e}")
        return False

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

        # 2. 获取根页面块 ID（page block 的 block_id 不一定等于 doc_id）
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks",
            params={"page_size": 1},
            headers=self._headers(), timeout=10
        )
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("items", [])
        page_block_id = items[0]["block_id"] if items else doc_id
        logger.info(f"根页面块 ID: {page_block_id}")

        # 3. 写入内容块（每批最多 50 块，避免超限）
        blocks = _text_to_blocks(text)
        batch_size = 50
        inserted = 0
        for i in range(0, len(blocks), batch_size):
            batch = blocks[i:i + batch_size]
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{page_block_id}/children",
                json={"children": batch, "index": inserted},
                headers=self._headers(), timeout=15
            )
            resp.raise_for_status()
            r = resp.json()
            if r.get("code") != 0:
                logger.warning(f"写入文档块失败（批次 {i}）: {r}")
            else:
                inserted += len(batch)

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
