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

import gzip
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_m3u import (  # noqa: E402
    EPGChannel, GenM3U, IPTVChannel, _ms_to_xmltv,
)

RTSP_BASE = "http://192.168.50.8:5140/rtsp/"
EPG_XMLTV = "http://epg.51zmt.top:8000/e.xml.gz"
# 合成样例（非真实凭据）：广东电信华为平台单个 accountinfo 鉴权参数的典型结构
SAMPLE_RTSP = (
    "rtsp://125.88.55.199/PLTV/88888888/224/3221225618/index.smil"
    "?accountinfo=FAKEAUTH0123456789ABCDEF"
)
PLAYSEEK_SUFFIX = "&playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}"


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
    # 模板占位符按字面保留（由播放器替换为实际本地时间，不能被提前格式化）
    assert "${(b)yyyyMMddHHmmss}" in catchup_source
    assert "${(e)yyyyMMddHHmmss}" in catchup_source


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
        channel_id="1", channel_name="CCTV-1综合高清",
        user_channel_id="101",
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
        with open(g.m3u_playback_path, encoding="utf-8-sig") as fp:
            playback = fp.read()

    lines = m3u.splitlines()

    # ① 表头带节目单（x-tvg-url + url-tvg 双写）
    assert lines[0].startswith(f'#EXTM3U x-tvg-url="{EPG_XMLTV}"')
    assert f'url-tvg="{EPG_XMLTV}"' in lines[0]

    # ② 主组播：tvg-id 用 51zmt 码 CCTV1，URL 是组播 /rtp/ + FCC，
    #    catchup-source 走 rtp2httpd /rtsp/ + playseek
    assert 'tvg-id="CCTV1"' in m3u
    # tvg-name 也对齐 51zmt 码（按 display-name 匹配 XMLTV），可见名仍是运营商 CCTV-1
    cctv1_main = [
        ln for ln in lines
        if 'group-title="央视"' in ln and 'tvg-id="CCTV1"' in ln
    ]
    assert cctv1_main, "未找到 CCTV1 主组播条目"
    assert all('tvg-name="CCTV1"' in ln for ln in cctv1_main), cctv1_main
    assert all(ln.endswith(",CCTV-1") for ln in cctv1_main), cctv1_main
    assert (
        "http://192.168.50.8:5140/rtp/239.3.1.1:8000"
        "?fcc=125.88.60.1:8027&fcc-type=huawei"
    ) in m3u
    assert ('catchup-source="http://192.168.50.8:5140/rtsp/125.88.55.199/'
            "PLTV/88888888/224/3221225618/index.smil?accountinfo="
            "FAKEAUTH0123456789ABCDEF&playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}\"") in m3u

    # ③ CCTV-2 无 RTSP：主源在、tvg-id=CCTV2、但该条无 catchup-source
    assert 'tvg-id="CCTV2"' in m3u
    cctv2_main = [
        ln for ln in lines
        if 'tvg-id="CCTV2"' in ln and 'group-title="央视"' in ln
    ]
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
    assert "&playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}" in catchup_grp[0]

    # ⑥ 回放文件 URL 已从裸 rtsp:// 转成 rtp2httpd /rtsp/（LAN 可用）
    assert "rtsp://" not in playback
    assert "http://192.168.50.8:5140/rtsp/125.88.55.199/" in playback


def test_rewrite_epg_xmltv():
    """_rewrite_epg_xmltv: 数字 channel id → display-name，自托管 URL 更新。"""
    # 构造最小 XMLTV（2 channel + 2 programme，用数字 id）
    xmltv = b"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="1">
    <display-name>CCTV1</display-name>
  </channel>
  <channel id="27">
    <display-name>\xe6\xb9\x96\xe5\x8d\x97\xe5\x8d\xab\xe8\xa7\x86</display-name>
  </channel>
  <programme start="20260531200000 +0800" stop="20260531210000 +0800" channel="1">
    <title>\xe6\x96\xb0\xe9\x97\xbb\xe8\x81\x94\xe6\x92\xad</title>
  </programme>
  <programme start="20260531200000 +0800" stop="20260531213000 +0800" channel="27">
    <title>\xe5\xbf\xab\xe4\xb9\x90\xe5\xa4\xa7\xe6\x9c\xac\xe8\x90\xa5</title>
  </programme>
</tv>"""
    raw_gz = gzip.compress(xmltv)

    with tempfile.TemporaryDirectory() as d:
        cache_dir = os.path.join(d, "cache")
        output_dir = os.path.join(d, "output")
        os.makedirs(cache_dir)
        os.makedirs(output_dir)

        # 预置 cache（模拟下载失败时的 fallback）
        cache_path = os.path.join(cache_dir, "epg_xmltv_raw.gz")
        with open(cache_path, "wb") as f:
            f.write(raw_gz)

        # 构造 GenM3U（绕过 __init__）
        g = GenM3U.__new__(GenM3U)
        g.epg_xmltv_url = "http://will-fail.invalid/e.xml.gz"
        g.epg_xmltv_cache = cache_path
        g.epg_xmltv_path = os.path.join(output_dir, "epg.xml.gz")
        g.unicast_url = "http://192.168.50.8:5140/rtp/"
        g.output_dir = output_dir
        g.cache_dir = cache_dir

        g._rewrite_epg_xmltv()

        # 断言：输出文件存在且 gzip 可解
        assert os.path.exists(g.epg_xmltv_path), "epg.xml.gz 未生成"
        with open(g.epg_xmltv_path, "rb") as f:
            out_gz = f.read()
        out_xml = gzip.decompress(out_gz)
        root = ET.fromstring(out_xml)

        # 断言：channel id 已重写为 display-name
        ch_ids = {ch.get("id") for ch in root.findall("channel")}
        assert "CCTV1" in ch_ids, f"缺少 CCTV1, got {ch_ids}"
        assert "湖南卫视" in ch_ids, f"缺少 湖南卫视, got {ch_ids}"
        assert "1" not in ch_ids, f"数字 id '1' 不应存在, got {ch_ids}"
        assert "27" not in ch_ids, f"数字 id '27' 不应存在, got {ch_ids}"

        # 断言：programme channel 已重写
        prog_chs = {p.get("channel") for p in root.findall("programme")}
        assert "CCTV1" in prog_chs, f"programme 缺少 CCTV1, got {prog_chs}"
        assert "湖南卫视" in prog_chs, f"programme 缺少 湖南卫视, got {prog_chs}"

        # 断言：self.epg_xmltv_url 更新为自托管
        assert g.epg_xmltv_url == "http://192.168.50.8/epg.xml.gz", g.epg_xmltv_url


def test_ms_to_xmltv():
    """_ms_to_xmltv: UTC epoch 毫秒 → XMLTV 时间戳 (CST +0800)。"""
    # 2026-05-31 20:00:00 CST = 2026-05-31 12:00:00 UTC = 1780228800 s
    ms = 1780228800 * 1000
    assert _ms_to_xmltv(ms) == "20260531200000 +0800", _ms_to_xmltv(ms)
    # 2026-06-01 00:30:00 CST
    ms2 = (1780228800 + 4 * 3600 + 30 * 60) * 1000
    assert _ms_to_xmltv(ms2) == "20260601003000 +0800", _ms_to_xmltv(ms2)


def _mock_session(responses: dict[str, dict]):
    """构造一个 mock session，其 .get() 按 channelId 返回预置 JSON。"""
    class MockResp:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data

    class MockSession:
        def get(self, url, params=None, timeout=None):
            cid = str(params.get("channelId", "")) if params else ""
            data = responses.get(cid, {"playbillCount": 0, "playbillLites": []})
            return MockResp(data)

    return MockSession()


def test_fetch_playbill_xmltv():
    """_fetch_playbill_xmltv: 运营商节目单 → XMLTV，有数据/无数据频道均正确处理。"""
    ch1 = IPTVChannel(
        channel_id="6718", channel_name="CCTV-1综合高清",
        user_channel_id="101",
        igmp_url="igmp://239.3.1.1:8000", rtsp_url="",
    )
    ch2 = IPTVChannel(
        channel_id="9999", channel_name="河源综合高清",
        user_channel_id="501",
        igmp_url="igmp://239.3.1.99:8000", rtsp_url="",
    )
    epg = [
        EPGChannel(tvg_name="CCTV1", tvg_logo="http://logo/cctv1", group_title="央视"),
    ]

    pb_data = {
        "6718": {
            "playbillCount": 2,
            "playbillLites": [
                {"startTime": 1780228800000, "endTime": 1780232400000, "name": "新闻联播", "ID": "1"},
                {"startTime": 1780232400000, "endTime": 1780236000000, "name": "焦点访谈", "ID": "2"},
            ],
        },
    }

    with tempfile.TemporaryDirectory() as d:
        g = GenM3U.__new__(GenM3U)
        g.unicast_url = "http://192.168.50.8:5140/rtp/"
        g.output_dir = d
        g.epg_xmltv_path = os.path.join(d, "epg.xml.gz")
        g.epg_xmltv_url = "http://epg.51zmt.top:8000/e.xml.gz"
        g.session = _mock_session(pb_data)

        ok = g._fetch_playbill_xmltv(
            [ch1, ch2], [ch1, ch2], epg,
        )

        assert ok, "应返回 True（至少 1 个频道有数据）"
        assert os.path.exists(g.epg_xmltv_path), "epg.xml.gz 未生成"

        with open(g.epg_xmltv_path, "rb") as f:
            xml_bytes = gzip.decompress(f.read())
        root = ET.fromstring(xml_bytes)

        ch_ids = {c.get("id") for c in root.findall("channel")}
        assert "CCTV1" in ch_ids, f"缺少 CCTV1, got {ch_ids}"
        assert "501" in ch_ids, f"缺少 501 (河源 fallback), got {ch_ids}"

        progs = root.findall("programme")
        cctv1_progs = [p for p in progs if p.get("channel") == "CCTV1"]
        assert len(cctv1_progs) == 2, f"CCTV1 应有 2 条节目, got {len(cctv1_progs)}"
        assert cctv1_progs[0].get("start") == "20260531200000 +0800"
        assert cctv1_progs[0].find("title").text == "新闻联播"

        heyuan_progs = [p for p in progs if p.get("channel") == "501"]
        assert len(heyuan_progs) == 0, "河源无数据应无 programme"

        assert g.epg_xmltv_url == "http://192.168.50.8/epg.xml.gz", g.epg_xmltv_url


def test_playbill_sibling_fallback():
    """去重后频道 ID 无 playbill 数据，备选组中另一个 ID 有数据 → 成功。"""
    # 去重后选中 10000145（高清），但它没有 playbill；同名组的 6718 有数据
    ch_deduped = IPTVChannel(
        channel_id="10000145", channel_name="CCTV-1综合高清",
        user_channel_id="101",
        igmp_url="igmp://239.3.1.1:8000", rtsp_url="",
    )
    ch_sibling = IPTVChannel(
        channel_id="6718", channel_name="CCTV-1综合",
        user_channel_id="102",
        igmp_url="igmp://239.3.1.2:8000", rtsp_url="",
    )
    epg = [
        EPGChannel(tvg_name="CCTV1", tvg_logo="", group_title="央视"),
    ]

    pb_data = {
        "6718": {
            "playbillCount": 1,
            "playbillLites": [
                {"startTime": 1780228800000, "endTime": 1780232400000, "name": "新闻联播", "ID": "1"},
            ],
        },
    }

    with tempfile.TemporaryDirectory() as d:
        g = GenM3U.__new__(GenM3U)
        g.unicast_url = "http://192.168.50.8:5140/rtp/"
        g.output_dir = d
        g.epg_xmltv_path = os.path.join(d, "epg.xml.gz")
        g.epg_xmltv_url = "http://epg.51zmt.top:8000/e.xml.gz"
        g.session = _mock_session(pb_data)

        ok = g._fetch_playbill_xmltv(
            [ch_deduped], [ch_deduped, ch_sibling], epg,
        )

        assert ok, "备选 ID 有数据，应返回 True"

        with open(g.epg_xmltv_path, "rb") as f:
            xml_bytes = gzip.decompress(f.read())
        root = ET.fromstring(xml_bytes)

        progs = [p for p in root.findall("programme") if p.get("channel") == "CCTV1"]
        assert len(progs) == 1, f"应有 1 条节目（来自备选 6718）, got {len(progs)}"
        assert progs[0].find("title").text == "新闻联播"


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
