from src.utils.helper import is_douyin_url, extract_url, format_number, get_month_folder_name


def test_is_douyin_url():
    assert is_douyin_url("https://www.douyin.com/video/1234567890") is True
    assert is_douyin_url("https://v.douyin.com/abcdef/") is True
    assert is_douyin_url("https://www.youtube.com/watch?v=xxx") is False
    assert is_douyin_url("随便一段文字") is False


def test_extract_url():
    assert extract_url("看看这个 https://v.douyin.com/abc123/ 好看吗") == "https://v.douyin.com/abc123/"
    assert extract_url("没有链接的文字") is None


def test_format_number():
    assert format_number(None) == "-"
    assert format_number(999) == "999"
    assert format_number(12300) == "1.2w"
    assert format_number(100000) == "10.0w"


def test_get_month_folder_name():
    from datetime import datetime
    name = get_month_folder_name()
    now = datetime.now()
    assert str(now.year) in name
    assert str(now.month) in name
