#!/usr/bin/env python3
"""离线单测：RTSP→HTTP 转换 (_rtsp_http_url)、回看 catchup-source 串拼接，
以及 _generate_m3u 产出的表头 / 主组播 / RTSP直播 / RTSP回看 结构。

不需要认证 / 网络 / 配置文件：用 GenM3U.__new__ 绕过 __init__，只注入所需属性。
验证回看 URL 的拼接契约：
  catchup-source 形如 http://…:5140/rtsp/125.88.55.199/…&playseek=…
  且 accountinfo 鉴权串完整保留、playseek 追加在最末尾。

运行：
  python3 tests/test_rtsp_catchup.py      # 独立运行，全部通过打印 OK
  pytest tests/test_rtsp_catchup.py       # 或用 pytest（若已安装）
  # 依赖 generate_m3u 的第三方包（requests/pyyaml/bs4），uv 用法：
  #   uv run --no-project --with requests --with pyyaml --with beautifulsoup4 \
  #     python tests/test_rtsp_catchup.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_m3u import EPGChannel, GenM3U, IPTVChannel  # noqa: E402

RTSP_BASE = "http://192.168.50.8:5140/rtsp/"
EPG_XMLTV = "http://epg.51zmt.top:8000/e.xml.gz"
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


def _gen_full(output_dir: str) -> GenM3U:
    """构造可跑 _generate_m3u 的 GenM3U（注入全部所需属性，绕过认证/网络）。"""
    g = GenM3U.__new__(GenM3U)
    g.unicast_url = "http://192.168.50.8:5140/rtp/"
    g.rtsp_base = RTSP_BASE
    g.epg_xmltv_url = EPG_XMLTV
    g.output_dir = output_dir
    g.m3u_stream_path = os.path.join(output_dir, "iptv.m3u")
    g.m3u_playback_path = os.path.join(output_dir, "iptv_playback.m3u")
    return g


def test_generate_m3u_structure():
    """端到端（离线）跑 _generate_m3u，核对表头/主源/RTSP直播/RTSP回看 结构。"""
    # CCTV-1：有 RTSP，应出现在主源 + RTSP直播 + RTSP回看
    ch1 = IPTVChannel(
        channel_id="1", channel_name="CCTV-1综合高清", user_channel_id="101",
        igmp_url="igmp://239.3.1.1:8000", rtsp_url=SAMPLE_RTSP,
        fcc_enable=True, fcc_ip="125.88.60.1", fcc_port="8027",
    )
    # CCTV-2：无 RTSP，只应出现在主源（且无 catchup），不进 RTSP 两组
    ch2 = IPTVChannel(
        channel_id="2", channel_name="CCTV-2财经高清", user_channel_id="102",
        igmp_url="igmp://239.3.1.2:8000", rtsp_url="",
    )
    channels = [ch1, ch2]
    epg = [
        EPGChannel(tvg_name="CCTV1", tvg_logo="http://logo/cctv1", group_title="央视"),
        EPGChannel(tvg_name="CCTV2", tvg_logo="http://logo/cctv2", group_title="央视"),
    ]
    codec_map = {"239.3.1.1:8000": {"video": "H.264"},
                 "239.3.1.2:8000": {"video": "H.264"}}

    with tempfile.TemporaryDirectory() as d:
        g = _gen_full(d)
        g._generate_m3u(channels, epg, codec_map)
        with open(g.m3u_stream_path, encoding="utf-8-sig") as f:
            m3u = f.read()
        with open(g.m3u_playback_path, encoding="utf-8-sig") as f:
            playback = f.read()

    lines = m3u.splitlines()

    # ① 表头带节目单（x-tvg-url + url-tvg 双写）
    assert lines[0].startswith(f'#EXTM3U x-tvg-url="{EPG_XMLTV}"')
    assert f'url-tvg="{EPG_XMLTV}"' in lines[0]

    # ② 主组播：tvg-id 用 51zmt 码 CCTV1，URL 是组播 /rtp/ + FCC，
    #    catchup-source 走 rtp2httpd /rtsp/ + playseek
    assert 'tvg-id="CCTV1"' in m3u
    # tvg-name 也对齐 51zmt 码（按 display-name 匹配 XMLTV），但可见名仍是运营商 CCTV-1
    cctv1_main = [ln for ln in lines if 'group-title="央视"' in ln and 'tvg-id="CCTV1"' in ln]
    assert cctv1_main, "未找到 CCTV1 主组播条目"
    assert all('tvg-name="CCTV1"' in ln for ln in cctv1_main), cctv1_main
    assert all(ln.endswith(",CCTV-1") for ln in cctv1_main), cctv1_main
    assert "http://192.168.50.8:5140/rtp/239.3.1.1:8000?fcc=125.88.60.1:8027&fcc-type=huawei" in m3u
    assert ('catchup-source="http://192.168.50.8:5140/rtsp/125.88.55.199/'
            "PLTV/88888888/224/3221225618/index.smil?accountinfo="
            "FAKEAUTH0123456789ABCDEF&playseek={utc:YmdHMS}-{utcend:YmdHMS}\"") in m3u

    # ③ CCTV-2 无 RTSP：主源在、tvg-id=CCTV2、但该条无 catchup-source
    assert 'tvg-id="CCTV2"' in m3u
    cctv2_main = [ln for ln in lines if 'tvg-id="CCTV2"' in ln and 'group-title="央视"' in ln]
    assert cctv2_main and all("catchup-source=" not in ln for ln in cctv2_main), cctv2_main

    # ④ RTSP直播：仅 CCTV1 一条；该 #EXTINF 行无 tvg-id；名带 [RTSP]
    live = [ln for ln in lines if 'group-title="RTSP直播"' in ln]
    assert len(live) == 1, live
    assert "tvg-id=" not in live[0]
    assert "CCTV-1 [RTSP]" in live[0]

    # ⑤ RTSP回看：仅 CCTV1 一条；有 tvg-id=CCTV1 + tvg-name=CCTV1；名带 [回看]；带 catchup
    catchup_grp = [ln for ln in lines if 'group-title="RTSP回看"' in ln]
    assert len(catchup_grp) == 1, catchup_grp
    assert 'tvg-id="CCTV1"' in catchup_grp[0]
    assert 'tvg-name="CCTV1"' in catchup_grp[0]  # tvg-name 对齐 51zmt 码以绑节目单
    assert "CCTV-1 [回看]" in catchup_grp[0]      # 可见名带 [回看] 后缀
    assert "catchup-source=" in catchup_grp[0]
    assert "&playseek={utc:YmdHMS}-{utcend:YmdHMS}" in catchup_grp[0]

    # ⑥ 回放文件 URL 已从裸 rtsp:// 转成 rtp2httpd /rtsp/（LAN 可用）
    assert "rtsp://" not in playback
    assert "http://192.168.50.8:5140/rtsp/125.88.55.199/" in playback


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
