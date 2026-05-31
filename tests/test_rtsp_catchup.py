#!/usr/bin/env python3
"""离线单测：RTSP→HTTP 转换 (_rtsp_http_url) 与回看 catchup-source 串拼接。

不需要认证 / 网络 / 配置文件：用 GenM3U.__new__ 绕过 __init__，只注入
rtsp_base，验证回看 URL 的拼接契约：
  catchup-source 形如 http://…:5140/rtsp/125.88.55.199/…&playseek=…
  且 accountinfo 鉴权串完整保留、playseek 追加在最末尾。

运行：
  python3 tests/test_rtsp_catchup.py      # 独立运行，全部通过打印 OK
  pytest tests/test_rtsp_catchup.py       # 或用 pytest（若已安装）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_m3u import GenM3U  # noqa: E402

RTSP_BASE = "http://192.168.50.8:5140/rtsp/"
# 合成样例（非真实凭据）：广东电信华为平台单个 accountinfo 鉴权参数的典型结构
SAMPLE_RTSP = (
    "rtsp://125.88.55.199/PLTV/88888888/224/3221225618/index.smil"
    "?accountinfo=FAKEAUTH0123456789ABCDEF"
)
PLAYSEEK_SUFFIX = "&playseek={utc:YmdHMS}-{utcend:YmdHMS}"


def _gen() -> GenM3U:
    """构造一个只设了 rtsp_base 的 GenM3U（绕过 __init__ 的认证/配置依赖）。"""
    g = GenM3U.__new__(GenM3U)
    g.rtsp_base = RTSP_BASE
    return g


def test_rtsp_http_url_basic():
    """rtsp:// 被剥掉、接到 /rtsp/ 基址后，host/path/query 原样透传。"""
    g = _gen()
    out = g._rtsp_http_url(SAMPLE_RTSP)
    assert out == (
        "http://192.168.50.8:5140/rtsp/125.88.55.199/PLTV/88888888/224/"
        "3221225618/index.smil?accountinfo=FAKEAUTH0123456789ABCDEF"
    ), out
    # 关键不变量：以 /rtsp/ 基址开头、accountinfo 鉴权串完整保留
    assert out.startswith("http://192.168.50.8:5140/rtsp/125.88.55.199/")
    assert "accountinfo=FAKEAUTH0123456789ABCDEF" in out
    assert "rtsp://" not in out  # 已转成 http


def test_rtsp_http_url_non_rtsp_returns_empty():
    """非 rtsp:// 输入（含空串）返回 ""，调用方据此跳过/回退。"""
    g = _gen()
    assert g._rtsp_http_url("") == ""
    assert g._rtsp_http_url("igmp://239.3.1.1:8000") == ""
    assert g._rtsp_http_url("http://already/http") == ""


def test_catchup_source_assembly():
    """回看 catchup-source = _rtsp_http_url(rtsp) + &playseek=…（与生成器一致）。"""
    g = _gen()
    http_url = g._rtsp_http_url(SAMPLE_RTSP)
    catchup_source = f"{http_url}{PLAYSEEK_SUFFIX}"

    # 形如 http://…:5140/rtsp/125.88.55.199/…&playseek=…
    assert catchup_source.startswith("http://192.168.50.8:5140/rtsp/125.88.55.199/")
    # accountinfo 完整
    assert "accountinfo=FAKEAUTH0123456789ABCDEF" in catchup_source
    # playseek 在最末尾，且用 & 追加（因原 query 已有 ?accountinfo=）
    assert catchup_source.endswith(PLAYSEEK_SUFFIX)
    assert "?accountinfo=" in catchup_source and "&playseek=" in catchup_source
    # accountinfo 在 playseek 之前（鉴权串没有被 playseek 截断/挤掉）
    assert catchup_source.index("accountinfo=") < catchup_source.index("&playseek=")
    # 模板占位符按字面保留（由播放器替换为实际 UTC 时间，不能被提前格式化）
    assert "{utc:YmdHMS}" in catchup_source
    assert "{utcend:YmdHMS}" in catchup_source


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    if failures:
        print(f"\n{failures} test(s) failed")
        return 1
    print("\nOK — all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
