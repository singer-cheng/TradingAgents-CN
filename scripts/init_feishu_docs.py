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
    bitable_fields = [
        {"field_name": "日期", "type": 5},
        {"field_name": "股票代码", "type": 1},
        {"field_name": "股票名称", "type": 1},
        {"field_name": "市场", "type": 3},
        {"field_name": "决策", "type": 3},
        {"field_name": "目标价", "type": 2},
        {"field_name": "1月目标价", "type": 2},
        {"field_name": "3月目标价", "type": 2},
        {"field_name": "止损位", "type": 2},
        {"field_name": "分析摘要", "type": 1},
    ]
    for field in bitable_fields:
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
