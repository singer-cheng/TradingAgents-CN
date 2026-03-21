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


def run_analysis(force: bool = False):
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"===== TradingAgents 每日分析启动：{today} =====")

    # 0. 交易日检查（--force 可跳过，用于手动测试）
    if not force and not is_trading_day(today):
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
    ta_config["llm_provider"] = "dashscope"
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
                # 若「完整报告」为空则补写
                record_id = existing[0].get("record_id")
                existing_fields = existing[0].get("fields", {})
                if record_id and not existing_fields.get("完整报告") and raw_text:
                    feishu.update_bitable_record(bitable_token, table_id, record_id,
                                                 {"完整报告": raw_text, "文本": f"{code} {name}".strip()})
                    logger.info(f"{code} 今日已有记录，已补写完整报告")
                else:
                    logger.info(f"{code} 今日已有记录，跳过写入")
            else:
                # 写入多维表格
                fields = {
                    "文本": f"{code} {name}".strip(),  # 主字段（记录标题）
                    "日期": int(datetime.strptime(today, "%Y-%m-%d").timestamp() * 1000),
                    "股票代码": code,
                    "股票名称": name,
                    "市场": market,
                    "决策": action,
                    "分析摘要": reasoning,
                    "完整报告": raw_text,  # Risk Manager 完整分析报告
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="跳过交易日检查（手动测试用）")
    args = parser.parse_args()
    run_analysis(force=args.force)
