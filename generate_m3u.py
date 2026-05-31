#!/usr/bin/env python3
"""IPTV M3U 播放列表生成器。"""

import argparse
import csv
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher, get_close_matches
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

# ── 常量 ──────────────────────────────────────────

AUTH_WAIT_SECONDS = 10
REQUEST_TIMEOUT = 20
M3U_HEADER = "#EXTM3U\n\n"
DEFAULT_GROUP_TITLE = "其他"
AUTH_SERVER_URL = "http://125.88.80.45:8082/EPG/jsp/ValidAuthenticationHWCTC.jsp"
CHANNEL_LIST_URL = "http://125.88.80.45:8082/EPG/jsp/getchannellistHWCTC.jsp"

# TS stream type → 编码名称
STREAM_TYPE_NAMES = {
    0x01: "MPEG-1", 0x02: "H.262", 0x1B: "H.264", 0x24: "H.265",
    0x42: "AVS2", 0x03: "MP1-Audio", 0x04: "MP2-Audio",
    0x0F: "AAC", 0x11: "AAC-LATM", 0x81: "AC-3", 0x06: "PES-Private",
}
PROBE_SAMPLE_BYTES = 2097152  # 2MB per stream (4K streams need more data for PAT/PMT)
PROBE_TIMEOUT = 15
PROBE_CONCURRENCY = 3
PROBE_RETRIES = 2

CHANNEL_PATTERN = re.compile(
    r'ChannelID="(\d+)",ChannelName="([^"]+)",UserChannelID="(\d+)",'
    r'ChannelURL="([^|]+)\|([^"]+)"'
    r'.*?FCCEnable="(\d)"'
    r'(?:,ChannelFCCIP="([^"]*)",ChannelFCCPort="(\d+)")?'
)

# CCTV 频道号提取: "CCTV-1综合" → ("CCTV1", ...), "CCTV5＋体育" → ("CCTV5＋", ...)
# 注意: 不匹配 "CCTV4K"（独立频道），用负前瞻排除数字后紧跟 K 的情况
_CCTV_RE = re.compile(r"^CCTV-?(\d{1,2})(?![Kk])([＋+])?")

log = logging.getLogger(__name__)


def _similarity(a: str, b: str) -> float:
    """返回两个字符串的相似度 (0~1)。"""
    return SequenceMatcher(None, a, b).ratio()


def _normalize_name(name: str) -> tuple[str, int]:
    """将频道名归一化为 (基础名, 画质等级)。

    处理链:
    1. 去掉技术后缀: 测试、时移专用、开机
    2. 去掉码率后缀: 4M、1M
    3. 检测并去掉画质后缀: 4K超高清 > 超清 > 高清 > 标清
    4. CCTV 频道统一为 "CCTV{N}" 格式

    Examples:
        "CCTV-1综合高清"      → ("CCTV1", 2)
        "CCTV-1超清"          → ("CCTV1", 3)
        "CCTV-1高清4M"        → ("CCTV1", 2)
        "CCTV1-1M开机标清"    → ("CCTV1", 1)
        "CCTV5＋体育高清-测试" → ("CCTV5＋", 2)
        "广东卫视4k超高清"    → ("广东卫视", 4)
        "广东4K超高清"        → ("广东4K", 4)
        "CCTV4K-25P"          → ("CCTV4K", 4)
        "广东珠江超高清"      → ("广东珠江", 3)
        "广东卫视时移专用"    → ("广东卫视", 0)
    """
    # ── 第 1 步: 去掉技术后缀 ──
    name = re.sub(r"-?测试$", "", name)
    name = re.sub(r"时移专用$", "", name)
    name = re.sub(r"开机", "", name)

    # ── 第 2 步: 去掉码率后缀 (先于画质, 因为可能出现 "高清4M") ──
    name = re.sub(r"-?\d+M(?=[\u4e00-\u9fff]|$)", "", name)
    name = name.rstrip("-")

    # ── 第 3 步: 检测画质 ──
    quality = 0

    # 4K 帧率变体: "CCTV4K-25P" → "CCTV4K"
    m = re.search(r"(4[Kk])-?\d+[Pp]$", name)
    if m:
        quality = 4
        name = name[: m.end(1)]
    else:
        # 4K 复合后缀: "广东卫视4k超高清窄色域30", "广东卫视4k超高清25p"
        m = re.search(r"4[Kk](?:超高清|超清)?(?:窄色域\d+)?(?:-?\d+[Pp])?$", name)
        if m:
            quality = 4
            candidate = name[: m.start()]
            # 基础名足够长 → 4K 是画质变体; 太短 → 4K 是频道名的一部分
            name = candidate if len(candidate) >= 3 else name[: m.start()] + "4K"
        else:
            # 标准画质后缀
            m = re.search(r"(超高清|超清|高清|标清)$", name)
            if m:
                quality = {"超高清": 3, "超清": 3, "高清": 2, "标清": 1}[m.group(1)]
                name = name[: m.start()]

    # ── 第 4 步: CCTV / CETV 频道归一化 ──
    m = _CCTV_RE.match(name)
    if m:
        plus = "＋" if m.group(2) else ""
        name = f"CCTV-{m.group(1)}{plus}"
    else:
        m = re.match(r"^CETV-?(\d{1,2})", name)
        if m:
            name = f"CETV-{m.group(1)}"

    return name, quality


# ── 数据模型 ──────────────────────────────────────


@dataclass
class IPTVChannel:
    """IPTV 频道。"""

    channel_id: str
    channel_name: str
    user_channel_id: str
    igmp_url: str
    rtsp_url: str
    fcc_enable: bool = False
    fcc_ip: str = ""
    fcc_port: str = ""


@dataclass
class EPGChannel:
    """EPG 频道（匹配用）。"""

    tvg_name: str
    tvg_logo: str
    group_title: str


# ── TS 编码探测 ───────────────────────────────────


def _parse_pmt(data: bytes) -> dict[str, str]:
    """从 TS 数据中解析 PAT→PMT，返回 {video: codec, audio: codec}。"""
    # Step 1: find PMT PID from PAT (PID 0x0000)
    pmt_pid = None
    pos = 0
    while pos + 188 <= len(data):
        if data[pos] != 0x47:
            pos += 1
            continue
        pid = ((data[pos + 1] & 0x1F) << 8) | data[pos + 2]
        if pid == 0x0000:
            afc = (data[pos + 3] >> 4) & 0x03
            off = pos + 4
            if afc & 0x02:
                off += 1 + data[pos + 4]
            if afc & 0x01 and off < pos + 188:
                ptr = data[off]
                toff = off + 1 + ptr
                if toff + 8 < pos + 188 and data[toff] == 0x00:
                    sl = ((data[toff + 1] & 0x0F) << 8) | data[toff + 2]
                    eo = toff + 8
                    end = toff + 3 + sl - 4
                    while eo + 4 <= end:
                        prog = (data[eo] << 8) | data[eo + 1]
                        ppid = ((data[eo + 2] & 0x1F) << 8) | data[eo + 3]
                        if prog != 0:
                            pmt_pid = ppid
                            break
                        eo += 4
                    if pmt_pid:
                        break
        pos += 188

    if not pmt_pid:
        return {}

    # Step 2: parse PMT for stream types
    result: dict[str, str] = {}
    pos = 0
    while pos + 188 <= len(data):
        if data[pos] != 0x47:
            pos += 1
            continue
        pid = ((data[pos + 1] & 0x1F) << 8) | data[pos + 2]
        if pid == pmt_pid:
            afc = (data[pos + 3] >> 4) & 0x03
            off = pos + 4
            if afc & 0x02:
                off += 1 + data[pos + 4]
            if afc & 0x01 and off < pos + 188:
                ptr = data[off]
                toff = off + 1 + ptr
                if toff + 12 < pos + 188 and data[toff] == 0x02:
                    sl = ((data[toff + 1] & 0x0F) << 8) | data[toff + 2]
                    pil = ((data[toff + 10] & 0x0F) << 8) | data[toff + 11]
                    eo = toff + 12 + pil
                    end = toff + 3 + sl - 4
                    while eo + 5 <= end and eo < pos + 188:
                        st = data[eo]
                        eil = ((data[eo + 3] & 0x0F) << 8) | data[eo + 4]
                        name = STREAM_TYPE_NAMES.get(st, "0x%02X" % st)
                        if st in (0x01, 0x02, 0x1B, 0x24, 0x42):
                            result["video"] = name
                        elif st in (0x03, 0x04, 0x0F, 0x11, 0x81):
                            result["audio"] = name
                        eo += 5 + eil
                    return result
        pos += 188
    return result


def _probe_one(url: str) -> dict[str, str]:
    """探测单个流的编码信息，边收边解析，尽早返回。"""
    try:
        with requests.get(url, stream=True, timeout=PROBE_TIMEOUT) as r:
            data = b""
            for chunk in r.iter_content(chunk_size=65536):
                data += chunk
                # 每收到 256KB 尝试一次解析，成功即返回
                if len(data) % 262144 < 65536:
                    result = _parse_pmt(data)
                    if result.get("video"):
                        return result
                if len(data) >= PROBE_SAMPLE_BYTES:
                    break
        return _parse_pmt(data)
    except Exception:
        return {}


def probe_codecs(
    channels: list[IPTVChannel], unicast_url: str, cache_path: str,
) -> dict[str, dict[str, str]]:
    """增量探测频道编码，只探测缓存中缺失或为空的条目。

    已有有效编码信息的频道不会被重新探测，可多次运行逐步补全。
    """
    # Load existing cache
    existing: dict[str, dict[str, str]] = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            existing = json.load(f)

    # Build unique multicast → URL mapping, skip already-known
    tasks: dict[str, str] = {}
    skipped = 0
    for ch in channels:
        igmp = ch.igmp_url
        if not igmp.startswith("igmp://"):
            continue
        mcast = igmp[7:]
        if mcast in tasks:
            continue
        # Skip if cache already has valid codec info
        cached = existing.get(mcast, {})
        if cached.get("video"):
            skipped += 1
            continue
        url = f"{unicast_url}{mcast}"
        if ch.fcc_enable and ch.fcc_ip:
            url += f"?fcc={ch.fcc_ip}:{ch.fcc_port}&fcc-type=huawei"
        tasks[mcast] = url

    if not tasks:
        log.info("编码缓存已完整 (%d 条)，无需探测", skipped)
        return existing

    log.info(
        "开始探测 %d 个组播地址的编码信息 (跳过 %d 个已知, 并发=%d)...",
        len(tasks), skipped, PROBE_CONCURRENCY,
    )
    results: dict[str, dict[str, str]] = {}
    pending = dict(tasks)

    for attempt in range(1, PROBE_RETRIES + 1):
        done = 0
        with ThreadPoolExecutor(max_workers=PROBE_CONCURRENCY) as pool:
            futures = {pool.submit(_probe_one, url): mc for mc, url in pending.items()}
            for future in as_completed(futures):
                mcast = futures[future]
                info = future.result()
                if info:
                    results[mcast] = info
                done += 1
                if done % 20 == 0 or done == len(pending):
                    log.info("  第 %d 轮进度: %d/%d", attempt, done, len(pending))

        # Remaining unknowns for next retry
        pending = {mc: url for mc, url in pending.items() if mc not in results}
        if not pending:
            break
        log.info("  第 %d 轮完成，剩余 %d 个未识别，重试...", attempt, len(pending))

    # Merge: existing + new results (new results overwrite empty entries)
    merged = dict(existing)
    for mc, info in results.items():
        merged[mc] = info
    # Mark still-unknown as empty (but don't overwrite existing valid data)
    for mc in pending:
        if not merged.get(mc, {}).get("video"):
            merged[mc] = {}

    # Save cache
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    # Summary
    new_found = len(results)
    still_unknown = sum(1 for v in merged.values() if not v.get("video"))
    log.info("编码探测完成: 新识别 %d 个，仍未知 %d 个", new_found, still_unknown)
    codecs: dict[str, int] = {}
    for info in merged.values():
        v = info.get("video", "unknown")
        codecs[v] = codecs.get(v, 0) + 1
    log.info("编码统计: %s", ", ".join("%s=%d" % (k, v) for k, v in sorted(codecs.items())))
    return merged


# ── 主类 ──────────────────────────────────────────


class GenM3U:
    """IPTV M3U 播放列表生成器。"""

    def __init__(self, config_path=None):
        if config_path is None:
            # 优先读本地私有配置（含真实凭据，已 gitignore），回退到 config.yaml
            for cand in ("config.yaml.local", "config.yaml"):
                if os.path.exists(cand):
                    config_path = cand
                    break
            else:
                raise FileNotFoundError(
                    "未找到配置文件，请先 cp config.example.yaml config.yaml.local 并填写认证信息"
                )
        elif not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        log.info("加载配置: %s", config_path)
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        if not cfg:
            raise ValueError("配置文件为空")

        self.epg_urls: list[dict[str, str]] = cfg["epg_urls"]
        # 节目单 XMLTV 地址（写进 M3U 表头 x-tvg-url，给播放器显示 EPG 节目表）。
        # 默认用 51zmt 的 e.xml.gz：与频道台标/分类页同源、每日更新，频道按
        # 51zmt 频道码（即 tvg-id）精确绑定。一般无需改，可在 config 覆盖。
        self.epg_xmltv_url: str = cfg.get(
            "epg_xmltv_url", "http://epg.51zmt.top:8000/e.xml.gz",
        )
        self.unicast_url: str = cfg["unicast_url"]                       # .../rtp/
        # RTSP 直播备用源 relay 基址：默认把 unicast_url 末段 rtp/ 换成 rtsp/
        # （同一 rtp2httpd 服务）。可在 config 显式覆盖 rtsp_url，默认无需配置。
        self.rtsp_base: str = cfg.get("rtsp_url") or (
            self.unicast_url.rstrip("/").rsplit("/", 1)[0] + "/rtsp/"
        )
        self.cache_dir: str = cfg.get("cache_dir", "cache")
        self.output_dir: str = cfg.get("output_dir", "output")

        # 认证表单数据
        auth = cfg["auth"]
        account = auth["iptv_account"]          # 完整 IPTV 账号，如 07623024411@iptv.gd
        bare_id = account.partition("@")[0]     # @ 前 → UserID（裸号）
        net_id = account.replace("@", "%40")    # 完整账号 URL 编码 → NetUserID/DHCPUserID
        ver = auth.get("stb_version", "1.1.0-UNIONMAN_UNP-SJA5.2024v1")
        self._auth_form_data = (
            f"UserID={bare_id}&Lang=&SupportHD=1&"
            f"NetUserID={net_id}&"
            f"DHCPUserID={net_id}&"
            f"Authenticator={auth['authenticator']}&"
            f"STBType={auth.get('stb_type', 'UNP-SJA5')}&"
            f"STBVersion={ver}&conntype=&"
            f"STBID={auth['stb_id']}&"
            "templateName=iptvsnmv3&areaId=&"
            f"userToken={auth.get('user_token', '')}&"
            "userGroupId=&productPackageId=&"
            f"mac={auth['mac'].replace(':', '%3A')}&UserField=&"
            f"SoftwareVersion={ver}&"
            f"IsSmartStb={auth.get('is_smart_stb', 0)}&"
            "desktopId=&stbmaker=&VIP="
        )

        # 文件路径
        self.res_html_path = os.path.join(self.cache_dir, "res.html")
        self.codec_cache_path = os.path.join(self.cache_dir, "codec_info.json")
        self.mapping_csv_path = os.path.join(self.output_dir, "channel_data.csv")
        self.m3u_stream_path = os.path.join(self.output_dir, "iptv.m3u")
        self.m3u_playback_path = os.path.join(self.output_dir, "iptv_playback.m3u")

    def generate(self, do_probe: bool = False) -> None:
        """主流程：认证 → 解析 → (探测编码) → 去重 → EPG → 匹配 → 生成。"""
        self._authenticate()
        channels = self._parse_channels()
        if do_probe:
            probe_codecs(channels, self.unicast_url, self.codec_cache_path)
        codec_map = self._load_codec_cache()
        channels = self._dedup_channels(channels, codec_map)
        epg_channels = self._fetch_and_parse_epg()
        self._generate_m3u(channels, epg_channels, codec_map)

    def _load_codec_cache(self) -> dict[str, dict[str, str]]:
        """加载编码缓存，不存在则返回空 dict。"""
        if os.path.exists(self.codec_cache_path):
            with open(self.codec_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            log.info("已加载编码缓存: %d 条", len(data))
            return data
        log.info("无编码缓存，跳过编码标签（使用 --probe 生成）")
        return {}

    def _dedup_channels(
        self, channels: list[IPTVChannel], codec_map: dict[str, dict[str, str]],
    ) -> list[IPTVChannel]:
        """同名频道只保留最高画质版本，排除 AVS2 编码。

        排序优先级: 画质优先，仅排除确认为 AVS2 的版本。
        未探测到编码的频道视为安全（不因 codec 未知而降级画质）。
        """
        groups: dict[str, list[tuple[int, IPTVChannel]]] = {}
        for ch in channels:
            base, quality = _normalize_name(ch.channel_name)
            groups.setdefault(base, []).append((quality, ch))

        deduped: list[IPTVChannel] = []
        excluded_avs2: list[str] = []
        for base, candidates in groups.items():
            # 排序: (is_not_avs2, quality) — 仅 AVS2 被降级，其余按画质排序
            def _sort_key(item: tuple[int, IPTVChannel]) -> tuple[int, int]:
                q, ch = item
                mcast = ch.igmp_url[7:] if ch.igmp_url.startswith("igmp://") else ""
                video = codec_map.get(mcast, {}).get("video", "")
                is_not_avs2 = 0 if video == "AVS2" else 1
                return (is_not_avs2, q)

            candidates.sort(key=_sort_key, reverse=True)
            best_rank, best_ch = candidates[0]
            mcast = best_ch.igmp_url[7:] if best_ch.igmp_url.startswith("igmp://") else ""
            best_codec = codec_map.get(mcast, {}).get("video", "")

            if best_codec == "AVS2":
                excluded_avs2.append(base)
            else:
                deduped.append(best_ch)
                # 同名组中编码未知的版本也保留（可能未探测到，单独展示供验证）
                for q, ch in candidates[1:]:
                    m = ch.igmp_url[7:] if ch.igmp_url.startswith("igmp://") else ""
                    if m and not codec_map.get(m, {}).get("video"):
                        deduped.append(ch)

        if excluded_avs2:
            log.warning(
                "以下频道所有版本均为 AVS2，已排除: %s", ", ".join(excluded_avs2),
            )
        log.info(
            "去重: %d → %d 个频道 (排除 %d 个纯 AVS2)",
            len(channels), len(deduped), len(excluded_avs2),
        )
        return deduped

    def _authenticate(self) -> None:
        """认证 IPTV 服务器，获取频道数据。"""
        os.makedirs(self.cache_dir, exist_ok=True)

        session = requests.Session()

        # 第一步：POST 认证，获取 cookie（Session 自动管理）
        log.info("正在认证 IPTV 服务器...")
        session.post(
            AUTH_SERVER_URL,
            data=self._auth_form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )

        log.info("认证成功，等待 %d 秒...", AUTH_WAIT_SECONDS)
        time.sleep(AUTH_WAIT_SECONDS)

        # 第二步：GET 频道列表（Session 自动带 cookie）
        log.info("正在获取频道信息...")
        response = session.get(CHANNEL_LIST_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        with open(self.res_html_path, "w", encoding="utf-8-sig") as f:
            f.write(response.text)
        log.info("已获取频道数据: %s", self.res_html_path)

    def _parse_channels(self) -> list[IPTVChannel]:
        """解析频道 HTML，返回频道列表，同时写入 CSV。"""
        os.makedirs(self.output_dir, exist_ok=True)

        with open(self.res_html_path, encoding="utf-8-sig") as f:
            html = f.read()

        raw = CHANNEL_PATTERN.findall(html)
        if not raw:
            log.warning("未从 HTML 中解析到任何频道数据")

        channels = [
            IPTVChannel(
                channel_id=ch[0],
                channel_name=re.sub(r"\s+", "", ch[1]),
                user_channel_id=ch[2],
                igmp_url=ch[3],
                rtsp_url=ch[4],
                fcc_enable=ch[5] == "1" if len(ch) > 5 else False,
                fcc_ip=ch[6] if len(ch) > 6 else "",
                fcc_port=ch[7] if len(ch) > 7 else "",
            )
            for ch in raw
        ]

        with open(self.mapping_csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["channel_id", "channel_name", "user_channel_id", "igmp_url", "rtsp_url",
                 "fcc_enable", "fcc_ip", "fcc_port"]
            )
            for ch in channels:
                writer.writerow(
                    [ch.channel_id, ch.channel_name, ch.user_channel_id,
                     ch.igmp_url, ch.rtsp_url,
                     ch.fcc_enable, ch.fcc_ip, ch.fcc_port]
                )

        log.info("已解析 %d 个频道: %s", len(channels), self.mapping_csv_path)
        return channels

    def _fetch_and_parse_epg(self) -> list[EPGChannel]:
        """从多个 EPG 分类页抓取频道数据。"""
        os.makedirs(self.cache_dir, exist_ok=True)
        all_channels: list[EPGChannel] = []
        for entry in self.epg_urls:
            url, group = entry["url"], entry["group"]
            channels = self._parse_epg_category_page(url, group)
            all_channels.extend(channels)
        log.info("EPG 汇总: 共 %d 个频道（来自 %d 个分类页）",
                 len(all_channels), len(self.epg_urls))
        return all_channels

    def _parse_epg_category_page(self, url: str, group: str) -> list[EPGChannel]:
        """解析单个 EPG 分类页，返回 EPGChannel 列表。

        分类页表格结构: 序号 | 台标 | 频道名称 | tvg-name | tvg-id | 分类 | 最新节目日期
        """
        cache_file = os.path.join(self.cache_dir, f"epg_{group}.html")
        log.info("正在抓取 EPG 分类页: %s (%s)", group, url)

        html = ""
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.encoding = response.apparent_encoding
            html = response.text
            with open(cache_file, "w", encoding="utf-8-sig") as f:
                f.write(html)
        except (requests.RequestException, OSError) as e:
            log.warning("EPG 分类页 '%s' 抓取失败: %s", group, e)
            if os.path.exists(cache_file):
                log.info("  使用缓存: %s", cache_file)
                with open(cache_file, encoding="utf-8-sig") as f:
                    html = f.read()
            else:
                log.warning("  无缓存可用，跳过")
                return []

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            log.warning("分类页 '%s' 未找到 <table>，跳过", group)
            return []

        tbody = table.find("tbody")
        if not tbody:
            log.warning("分类页 '%s' 未找到 <tbody>，跳过", group)
            return []

        channels: list[EPGChannel] = []
        for row in tbody.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 5:
                continue

            # 列 2: 频道名称 — <a><strong>CCTV-1综合</strong></a>
            # 列 3: tvg-name — <a><code>CCTV1</code></a>
            code_tag = tds[3].find("code")
            tvg_name = code_tag.get_text(strip=True) if code_tag else ""

            # tvg-logo: 分类页无 logo 图片，用频道页链接占位
            link = tds[2].find("a")
            tvg_logo = urljoin(url, link["href"]) if link and link.get("href") else ""

            channels.append(EPGChannel(
                tvg_name=tvg_name,
                tvg_logo=tvg_logo,
                group_title=group,
            ))

        log.info("  %s: %d 个频道", group, len(channels))
        return channels

    @staticmethod
    def _guess_group(name: str) -> str:
        """根据频道名猜测分组（fallback 规则）。"""
        if "CCTV" in name or "cctv" in name:
            return "央视"
        if "卫视" in name:
            return "卫视"
        if "河源" in name:
            return "河源"
        if any(kw in name for kw in ("广东", "深圳", "珠江", "南方", "广州")):
            return "广东"
        return DEFAULT_GROUP_TITLE

    def _build_epg_index(
        self, epg_channels: list[EPGChannel],
    ) -> dict[str, EPGChannel]:
        """将 EPG 频道构建为 归一化名 → EPGChannel 索引。

        EPG tvg-name（如 "CCTV1"、"广东卫视"）经过与 IPTV 频道名
        相同的归一化处理，使两边可以用字典精确查找。
        """
        index: dict[str, EPGChannel] = {}
        for ch in epg_channels:
            if not ch.tvg_name:
                continue
            key = self._normalize_epg_name(ch.tvg_name)
            if key not in index:
                index[key] = ch
        return index

    @staticmethod
    def _normalize_epg_name(name: str) -> str:
        """归一化 EPG tvg-name，与 _normalize_name 产生相同的基础名。"""
        name = re.sub(r"[-]", "", name)
        m = _CCTV_RE.match(name)
        if m:
            plus = "＋" if m.group(2) else ""
            name = f"CCTV-{m.group(1)}{plus}"
        else:
            m = re.match(r"^CETV-?(\d{1,2})", name)
            if m:
                name = f"CETV-{m.group(1)}"
        return name

    def _match_channel(
        self,
        base_name: str,
        channel_name: str,
        epg_index: dict[str, EPGChannel],
    ) -> tuple[str, str, str]:
        """三级匹配: 精确 → 忽略大小写 → 高阈值模糊 → fallback。

        返回 (tvg_logo, group_title, epg_id)。epg_id = 命中 EPG 频道的 tvg_name，
        即 51zmt 频道码（如 CCTV1 / HunanTV），与节目单 XMLTV 的 <channel id>
        同一套编码，用作 tvg-id 精确绑定节目单；未命中返回 ""。
        """
        # ① 精确匹配（覆盖 90%+ 的频道）
        ch = epg_index.get(base_name)
        if ch:
            log.info("频道 '%s' → '%s' [精确]  分类=%s",
                     channel_name, ch.tvg_name, ch.group_title)
            return ch.tvg_logo, ch.group_title, ch.tvg_name

        # ② 忽略大小写（处理 cctv/CCTV 等差异）
        lower = base_name.lower()
        for key, ch in epg_index.items():
            if key.lower() == lower:
                log.info("频道 '%s' → '%s' [忽略大小写]  分类=%s",
                         channel_name, ch.tvg_name, ch.group_title)
                return ch.tvg_logo, ch.group_title, ch.tvg_name

        # ③ 高阈值模糊匹配（仅处理小差异，如 纪录/记录）
        keys = list(epg_index.keys())
        matches = get_close_matches(base_name, keys, n=1, cutoff=0.85)
        if matches:
            ch = epg_index[matches[0]]
            log.info("频道 '%s' → '%s' [模糊 %.0f%%]  分类=%s",
                     channel_name, ch.tvg_name,
                     _similarity(base_name, matches[0]) * 100, ch.group_title)
            return ch.tvg_logo, ch.group_title, ch.tvg_name

        # ④ fallback: 按频道名关键词猜分组
        group = self._guess_group(channel_name)
        log.debug("频道 '%s' 未匹配 EPG → fallback '%s'", channel_name, group)
        return "", group, ""

    @staticmethod
    def _channel_sort_key(
        group: str, display_name: str,
    ) -> tuple[int, str | int, str]:
        """生成排序 key: 先按分组顺序，组内按自然排序。"""
        GROUP_ORDER = {"央视": 0, "卫视": 1, "广东": 2, "河源": 3, "数字付费": 4, "其他": 5, "未识别": 6}
        group_idx = GROUP_ORDER.get(group, 99)

        # CCTV 频道按数字排序 (CCTV-1, CCTV-2, ..., CCTV-17)
        m = _CCTV_RE.match(display_name)
        if m:
            num = int(m.group(1))
            suffix = m.group(2) or ""  # "＋" 排在同号之后
            return (group_idx, num, suffix)

        return (group_idx, 999, display_name)

    def _rtsp_http_url(self, rtsp_url: str) -> str:
        """RTSP 源经 rtp2httpd 转 HTTP 单播：剥 rtsp:// 接到 /rtsp/ 基址后。

        rtsp://host/path?query → {rtsp_base}host/path?query
        完整 query（含 accountinfo 等鉴权尾巴）原样透传即可生效；RTSP 不加 FCC。
        """
        if not rtsp_url.startswith("rtsp://"):
            return ""
        return f"{self.rtsp_base}{rtsp_url[7:]}"

    def _generate_m3u(
        self,
        channels: list[IPTVChannel],
        epg_channels: list[EPGChannel],
        codec_map: dict[str, dict[str, str]],
    ) -> None:
        """生成 M3U 直播和回放文件。"""
        os.makedirs(self.output_dir, exist_ok=True)

        epg_index = self._build_epg_index(epg_channels)
        log.info("EPG 索引: %d 个条目", len(epg_index))

        # 预处理: 为每个频道计算 display_name、group、URL、epg_id
        entries: list[tuple[str, str, str, str, str, str, IPTVChannel]] = []
        for channel in channels:
            base_name, _quality = _normalize_name(channel.channel_name)
            display_name = base_name

            tvg_logo, group_title, epg_id = self._match_channel(
                base_name, channel.channel_name, epg_index,
            )

            # 编码未知的频道归入"未识别"分组
            igmp = channel.igmp_url
            mcast = igmp[7:] if igmp.startswith("igmp://") else ""
            if mcast and not codec_map.get(mcast, {}).get("video"):
                group_title = "未识别"

            http_url = (
                f"{self.unicast_url}{igmp[7:]}"
                if igmp.startswith("igmp://")
                else igmp
            )
            if channel.fcc_enable and channel.fcc_ip:
                http_url += f"?fcc={channel.fcc_ip}:{channel.fcc_port}&fcc-type=huawei"

            entries.append(
                (display_name, group_title, tvg_logo, http_url,
                 channel.rtsp_url, epg_id, channel),
            )

        # 排序: 分组顺序 → 组内自然排序
        entries.sort(key=lambda e: self._channel_sort_key(e[1], e[0]))

        # 直播表头带节目单：x-tvg-url / url-tvg 双写以兼容不同播放器
        #（Kodi IPTV Simple 认 x-tvg-url，部分播放器认 url-tvg）。
        stream_header = (
            f'#EXTM3U x-tvg-url="{self.epg_xmltv_url}" '
            f'url-tvg="{self.epg_xmltv_url}"\n\n'
        )

        with (
            open(self.m3u_stream_path, "w", encoding="utf-8-sig") as stream,
            open(self.m3u_playback_path, "w", encoding="utf-8-sig") as playback,
        ):
            stream.write(stream_header)
            playback.write(M3U_HEADER)

            for (display_name, group_title, tvg_logo, http_url,
                 rtsp_url, epg_id, channel) in entries:
                # tvg-id / tvg-name：优先 51zmt 频道码（绑定节目单），未命中回退。
                # 51zmt XMLTV 的 <channel id> 是纯数字序号、<display-name> 才是码
                #（CCTV1 / 湖南卫视…）；故 tvg-id 绑码靠播放器按 display-name 匹配，
                # tvg-name 也对齐成同一个码以最大化命中，未命中则用运营商显示名。
                tvg_id = epg_id or channel.user_channel_id
                epg_name = epg_id or display_name

                # catchup 回看：经 rtp2httpd 转 HTTP（/rtsp/ + playseek 时移），
                # LAN 播放器无需直达运营商 RTSP 即可回看。仅 RTSP 源可转换时添加。
                catchup_attrs = ""
                catchup_http = self._rtsp_http_url(rtsp_url)
                if catchup_http:
                    catchup_source = (
                        f"{catchup_http}&playseek={{utc:YmdHMS}}-{{utcend:YmdHMS}}"
                    )
                    catchup_attrs = (
                        f'catchup="default" catchup-days="7" '
                        f'catchup-source="{catchup_source}" '
                    )

                # 直播（组播 + FCC 转 HTTP 单播 + catchup 回看元数据）
                stream.write(
                    f'#EXTINF:-1 tvg-id="{tvg_id}" '
                    f'tvg-name="{epg_name}" '
                    f'tvg-logo="{tvg_logo}" group-title="{group_title}" '
                    f"{catchup_attrs},"
                    f"{display_name}\n"
                )
                stream.write(f"{http_url}\n\n")

                # 回放（经 rtp2httpd 转 HTTP 的 RTSP；供 catchup 参考，LAN 可直接播。
                # 直播兜底见末尾 RTSP直播 组、时移回看见 RTSP回看 组）
                playback_url = self._rtsp_http_url(rtsp_url) or rtsp_url
                playback.write(
                    f'#EXTINF:-1 tvg-id="{channel.user_channel_id}" '
                    f'tvg-name="{display_name}回放" '
                    f'tvg-logo="{tvg_logo}" group-title="{group_title}","'
                    f"{display_name}回放\n"
                )
                playback.write(f"{playback_url}\n\n")

            # ── RTSP直播（独立平铺分组，排在所有正常组之后）──
            # 范围 = 与组播主源完全相同的去重频道；每个有 rtsp_url 的频道一条，
            # 主源有什么备用就有什么、一一对应、画质一致。纯直播兜底：裸调
            # /rtsp/（不带 playseek）即直播。URL 经 rtp2httpd 转 HTTP，播放器只需
            # HTTP、不必能到 IPTV 内网。tvg-id 留空 → 零合并风险（TiviMate 等
            # 不会按 id 把它折叠进主源），代价是此组无 EPG 节目单（应急用可接受）。
            for (display_name, _group, tvg_logo, _http_url,
                 rtsp_url, _epg_id, _channel) in entries:
                live_url = self._rtsp_http_url(rtsp_url)
                if not live_url:
                    continue
                stream.write(
                    f'#EXTINF:-1 tvg-name="{display_name} [RTSP]" '
                    f'tvg-logo="{tvg_logo}" group-title="RTSP直播",'
                    f"{display_name} [RTSP]\n"
                )
                stream.write(f"{live_url}\n\n")

            # ── RTSP回看（独立平铺分组，紧随 RTSP直播）──
            # 与 RTSP直播 同源同范围，但带 catchup：直播主 URL 仍是裸 /rtsp/（直播），
            # catchup-source 加 playseek 走 rtp2httpd 时移。tvg-id 绑节目单（= 主组播
            # 同一 tvg-id），靠它从 EPG 选过去节目回看；显示名带 [回看] 后缀与主源/
            # RTSP直播 区分。已知权衡：与主组播同 tvg-id，个别播放器可能按 id 合并到
            # 同台（M3U 通病），靠 [回看] 后缀缓解。
            for (display_name, _group, tvg_logo, _http_url,
                 rtsp_url, epg_id, channel) in entries:
                live_url = self._rtsp_http_url(rtsp_url)
                if not live_url:
                    continue
                tvg_id = epg_id or channel.user_channel_id
                epg_name = epg_id or display_name
                catchup_source = (
                    f"{live_url}&playseek={{utc:YmdHMS}}-{{utcend:YmdHMS}}"
                )
                stream.write(
                    f'#EXTINF:-1 tvg-id="{tvg_id}" '
                    f'tvg-name="{epg_name}" '
                    f'tvg-logo="{tvg_logo}" group-title="RTSP回看" '
                    f'catchup="default" catchup-days="7" '
                    f'catchup-source="{catchup_source}",'
                    f"{display_name} [回看]\n"
                )
                stream.write(f"{live_url}\n\n")

        log.info("M3U 直播文件已生成: %s", self.m3u_stream_path)
        log.info("M3U 回放文件已生成: %s", self.m3u_playback_path)


# ── 入口 ──────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="IPTV M3U 播放列表生成器")
    parser.add_argument(
        "--probe", action="store_true",
        help="探测所有频道的视频编码并缓存（首次运行或编码变更时使用）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        GenM3U().generate(do_probe=args.probe)
    except Exception:
        log.exception("生成失败")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
