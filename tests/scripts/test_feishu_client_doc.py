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
    client._token_fetched_at = 9999999999  # 防止自动刷新 token（会多发一次 POST）
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
