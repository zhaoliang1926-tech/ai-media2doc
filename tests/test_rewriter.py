import json
from unittest.mock import patch, MagicMock
from src.processors.rewriter import _parse_result, RewriteResult


def test_parse_result_valid_json():
    raw = json.dumps({
        "titles": ["标题A", "标题B", "标题C"],
        "content": "这是改写文案正文内容",
        "hashtags": ["标签1", "标签2"],
        "comments": ["评论1", "评论2", "评论3"],
        "category": "知识",
        "summary": "一句话概括"
    })
    result = _parse_result(raw)
    assert isinstance(result, RewriteResult)
    assert len(result.titles) == 3
    assert result.content == "这是改写文案正文内容"
    assert result.category == "知识"


def test_parse_result_with_markdown():
    raw = '```json\n{"titles":["T1","T2","T3"],"content":"文案","hashtags":[],"comments":[],"category":"其他","summary":"概要"}\n```'
    result = _parse_result(raw)
    assert result.titles == ["T1", "T2", "T3"]


def test_parse_result_invalid_json():
    result = _parse_result("这不是 JSON 内容")
    assert result.content == "这不是 JSON 内容"
    assert result.titles == []
