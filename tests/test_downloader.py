from src.utils.helper import is_douyin_url


def test_douyin_url_detection():
    valid_urls = [
        "https://www.douyin.com/video/1234567890123",
        "https://v.douyin.com/iRxxx/",
    ]
    for url in valid_urls:
        assert is_douyin_url(url), f"应为抖音链接：{url}"

    invalid_urls = [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://youtube.com/watch?v=xxx",
    ]
    for url in invalid_urls:
        assert not is_douyin_url(url), f"不应为抖音链接：{url}"
