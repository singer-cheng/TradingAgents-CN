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
