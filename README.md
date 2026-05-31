# iptv2m3u

从运营商 IPTV 系统提取频道列表，自动探测编码、匹配 EPG、去重择优，生成带 **FCC 快速换台**与 **catchup 回看**属性的标准 M3U 播放列表。

> 面向广东电信华为平台调试（认证接口、FCC 协议、组播地址段均为该平台特征）。其他运营商/平台需自行适配。

## 功能特性

- 模拟机顶盒认证，获取频道列表
- 频道名归一化（CCTV-{N} / CETV-{N} 统一）+ 去重：同名多版本保留最高画质、排除多数播放器不支持的 AVS2
- 增量编码探测：直接解析 TS 流 PAT/PMT 判定 H.264/H.265/AVS2，结果缓存，已知频道跳过
- EPG 多分类页抓取 + 精确匹配（频道 logo、分组）
- 分组排序输出（央视 → 卫视 → 广东 → 数字付费 → …）
- 生成带 FCC 快速换台参数和 catchup 回看属性的 M3U
- 直播双源：组播 + FCC 为主源，另出一组经 rtp2httpd 转 HTTP 的 RTSP **备用源**（组播异常时兜底）
- 可配合 cron 定时更新（含黄金时段探测编码）

## 环境要求

- Python 3.12+（推荐用 [uv](https://github.com/astral-sh/uv) 管理）
- 运行在能访问 IPTV 内网的设备上（如旁路由 / 拨入 IPTV 大内网的 OpenWrt）
- [rtp2httpd](https://github.com/stackia/rtp2httpd)：将 IGMP 组播转 HTTP 单播，并提供 FCC / catchup 代理

## 网络拓扑

整套方案有三个关键节点：**光猫**（ONT，通常一口走互联网、一口走 IPTV 组播）、**主路由**（家庭网关 / NAT）、**IPTV 路由**（运行 iStoreOS，本项目与 rtp2httpd 都跑在这里）。

### 物理接线

```
                    运营商 ISP
              （互联网 + IPTV 组播源）
                        │ 光纤
                  ┌─────┴─────┐
                  │    光猫    │  ONT（互联网口 + IPTV 口）
                  └──┬──────┬─┘
          互联网口 ───┘      └─── IPTV 口
                │                    │ PPPoE 拨入
         ┌──────┴──────┐     ┌───────┴────────┐
         │    主路由    │     │    IPTV 路由    │
         │  网关 / NAT  │     │   (iStoreOS)   │
         └──────┬──────┘     │   · rtp2httpd  │
                │            │   · iptv2m3u   │
                │            └───────┬────────┘
                │                    │
                └────────┬───────────┘
                         │  家庭 LAN
                   ┌─────┴─────┐
                   │   交换机   │
                   └─────┬─────┘
                         │
             播放器 / 客户端（APTV · VLC · Kodi）
```

- **主路由**：上联光猫互联网口，负责家庭 LAN 与上网。
- **IPTV 路由（iStoreOS）**：WAN 口经光猫 IPTV 口 **PPPoE 拨入运营商 IPTV 大内网**；LAN 口并入家庭网络。组播转单播、FCC 秒切、catchup 回看、M3U 生成全在这台上完成。本机对互联网的访问（拉 EPG 等）走主路由。

### 数据流

```
直播媒体流:
  IPTV 组播源 ──PPPoE / IGMP──▶ IPTV 路由：rtp2httpd
                                （组播 → HTTP 单播 + FCC 秒切 + catchup）
                                       │ HTTP 单播
                                       ▼
                                  播放器 / 客户端

播放列表生成（本项目）:
  iptv2m3u ──认证──▶ IPTV EPG 服务器 ──▶ 拉频道列表 / 探测编码 / 匹配 EPG
       │
       └──▶ 生成 M3U ──写入──▶ uhttpd web 根 ──HTTP 订阅──▶ 播放器
```

> 媒体流与控制流分离：组播经 `rtp2httpd` 在 IPTV 路由本地转成 HTTP 单播下发，播放器只需订阅 IPTV 路由上的一个 HTTP 地址即可。

### 为什么 IPTV 要单独一台路由 PPPoE 拨入

运营商的 IPTV 与普通上网是**两套独立承载**：光猫上分属不同 VLAN，IPTV 侧需单独 PPPoE 拨号，认证后接入的是一个**只有组播源、没有公网**的运营商内网。交给一台专用路由处理，原因有几点：

- **网络性质不同**：IPTV 是 RTP/UDP 组播（239.x），依赖 IGMP 加入组，需要组播代理与策略路由（IPTV 网段走 PPPoE、其余走主路由上网）。这套配置专用且复杂，独立一台才好施展。
- **不污染主网络**：组播流量大，混进家庭主网会拖慢全网；隔离到专用路由，主网清爽。
- **解耦稳定**：IPTV PPPoE 偶有重拨，独立后抖动不影响全家上网；主路由也无需具备 IPTV 能力，可任意品牌/固件。
- **组播转单播的落点**：播放器大多不便直接收组播，由这台路由上的 `rtp2httpd` 统一转成 HTTP 单播下发，它天然就是“组播 → 单播”网关。

## 直播源：组播主 / RTSP 备

运营商对每个频道**同时下发两路源**：组播（`igmp://…`）与 RTSP 单播（`rtsp://…smil?accountinfo=…`）。本工具默认以**组播 + FCC 为主源**——换台最快、延迟最低；同时为每个频道再生成一条 **RTSP 备用源**，单独成组 `📡RTSP备用`。

- **主源（组播 + FCC）**：经 `rtp2httpd` 把组播转 HTTP 单播，FCC 秒级换台。依赖 IGMP 加组与 FCC 单播加速，链路相对“娇贵”（FCC 端口、上游 NAT 转发等都可能影响它）。
- **备用源（RTSP）**：同样经 `rtp2httpd` 转 HTTP 单播（走 `/rtsp/` 路径），但走 **TCP 单播、不依赖 IGMP/FCC**，天然更稳；**无 FCC**、换台延迟略高。与主源**同频道、同画质**，仅作组播异常时的兜底。

标准 M3U 不支持“一个频道两个 URL 自动 fallback”（各播放器行为不一、不通用），因此备用源**单独平铺成一组**：组播主源仍在各自原分组（央视 / 卫视 / 广东…），RTSP 备用全部集中到 `📡RTSP备用` 一组、显示名带 `[RTSP]` 后缀。组播频道黑屏时，切到该组的同名频道即可继续看。

> 备用条目刻意**不写 `tvg-id`**，避免被 TiviMate 等播放器按 id 与主源识别为同台而合并折叠；代价是备用源没有 EPG 节目单——应急观看可以接受。

## 适用边界 / 与同类项目

本项目针对**组播 + FCC 平台**（广东电信华为平台），产出**带编码探测 + EPG 匹配 + RTSP 备用**的成品 M3U，强调“开箱即用、主源秒切、组播挂了有兜底”。

如果你的线路**本就没有组播、只有 RTSP**（部分地区 / 平台已是纯单播），就用不到 FCC 与组播代理，可参考 [`yujincheng08/rust-iptv-proxy`](https://github.com/yujincheng08/rust-iptv-proxy)（AGPL-3.0，同为广东 IPTV）等“直接代理 RTSP”的思路。本项目的 RTSP 备用分支借鉴了“RTSP 也能作直播源”这一公开协议思路，但**全部自行实现、未引用其代码**。

## 快速开始

```bash
# 安装依赖（uv 会按 pyproject.toml / uv.lock 安装）
uv sync

# 配置认证信息（config.yaml.local 已被 gitignore，放心填真实凭据）
cp config.example.yaml config.yaml.local
# 编辑 config.yaml.local，填入你自己机顶盒的认证参数（见下）

# 生成播放列表
uv run generate_m3u.py            # 仅更新直播源
uv run generate_m3u.py --probe    # 同时探测编码（较慢）
```

生成的 M3U 写入 `config.yaml` 里的 `output_dir`（默认 `/www`，配合 uhttpd 等可直接 HTTP 订阅）。

## 获取认证信息

`config.yaml` 的 `auth` 段需要从**你自己合法订阅**的机顶盒获取，可通过抓包机顶盒开机认证请求，或读取机顶盒配置得到：

| 字段 | 说明 |
|------|------|
| `iptv_account` | 完整 IPTV 账号（含账号域，如 `07623024411@iptv.gd`） |
| `stb_id` | 机顶盒硬件 ID |
| `mac` | 机顶盒 MAC 地址 |
| `authenticator` | 认证令牌（**有时效，过期需重新获取**） |
| `user_token` | 用户令牌 |
| `stb_type` / `stb_version` | 机顶盒型号 / 固件版本 |

### 如何抓到这些字段

机顶盒开机时会向运营商的 EPG / 认证服务器发一个 HTTP POST 完成鉴权，上面这些字段就在那个请求里。恩山无线论坛也有不少 IPTV 抓包 / 认证分析讨论，例如「[广东电信 IPTV 验证过程分析、直播源提取、openwrt 单线复用、RTSP 代理](https://www.right.com.cn/forum/thread-8237625-1-1.html)」；各省份 / 平台差异很大，下面只抽象通用思路，不直接套用某个固定接口。

思路是**在机顶盒与上游之间把这次请求截下来**：

1. **选抓包点**：机顶盒所连的那台路由（OpenWrt / iStoreOS 等）就是天然的抓包位置——它本就在机顶盒与认证服务器的链路上，可在其上抓流量；也可用交换机端口镜像把机顶盒口的流量复制出来分析。
2. **触发认证**：重启机顶盒，让它重新走一遍开机鉴权（认证令牌有时效，抓的就是这一刻发出的请求）。
3. **定位请求**：在抓包结果里找发往 EPG / 认证服务器的 **HTTP POST**（表单里能看到 `UserID`、`Authenticator`、`STBID`、`MAC` 等键名），各运营商 / 平台的接口路径不尽相同，按抓到的实际请求为准。
4. **对应到配置**：把 POST 表单里的值按上表填进 `config.yaml` 对应字段即可。`iptv_account` 填完整账号（POST 里的 `NetUserID` / `DHCPUserID` 即「账号@账号域」形式）。

> `authenticator` 等令牌有时效，过期后认证会失败，届时按上述步骤重新抓一次刷新即可。

## 播放器

生成的 M3U 兼容 VLC、PotPlayer、Kodi（PVR IPTV Simple Client）、APTV 等。直播经 rtp2httpd 转 HTTP 单播 + FCC 秒切；支持 catchup 的播放器可回看时移。

## 定时更新

`cron_update.sh` 可挂到 crontab 做每日自动刷新（执行前随机延迟防风控），例如：

```cron
0 20 * * * /path/to/cron_update.sh --probe   # 黄金时段：探测编码 + 更新
0  8 * * * /path/to/cron_update.sh            # 早间：仅刷新直播源
```

## 免责声明

本项目仅供**学习研究**与**个人自用**：用于在自己的设备上观看你**已合法订阅**的 IPTV 服务。

- 请勿用于公开转发、分发广播内容或频道源，遵守所在地法律法规与运营商服务条款。
- 不要提交 / 公开任何真实认证凭据、账号或可用源列表（`config.yaml.local` / `config.yaml` 均已被 `.gitignore` 忽略）。
- 作者不对任何滥用或由此产生的后果负责。

## 许可证

[AGPL-3.0](./LICENSE)。本项目以 AGPL-3.0 开源；若你修改本项目并**通过网络对外提供服务**，亦须向使用者提供相应（含改动）源码。
</content>
