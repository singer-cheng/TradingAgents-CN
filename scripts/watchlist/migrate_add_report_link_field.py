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

    # 检查「报告链接」字段是否已存在（分页查询，避免漏检）
    existing_names = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}/tables/{table_id}/fields",
            headers=client._headers(), params=params, timeout=10
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        existing_names.extend(f["field_name"] for f in data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
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
        sys.exit(1)


if __name__ == "__main__":
    main()
