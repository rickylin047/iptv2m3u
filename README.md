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
- 可配合 cron 定时更新（含黄金时段探测编码）

## 环境要求

- Python 3.12+（推荐用 [uv](https://github.com/astral-sh/uv) 管理）
- 运行在能访问 IPTV 内网的设备上（如旁路由 / 拨入 IPTV 大内网的 OpenWrt）
- [rtp2httpd](https://github.com/stackia/rtp2httpd)：将 IGMP 组播转 HTTP 单播，并提供 FCC / catchup 代理

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
| `user_id` | 用户 ID（通常为宽带账号） |
| `stb_id` | 机顶盒硬件 ID |
| `mac` | 机顶盒 MAC 地址 |
| `authenticator` | 认证令牌（**有时效，过期需重新获取**） |
| `user_token` | 用户令牌 |
| `stb_type` / `stb_version` | 机顶盒型号 / 固件版本 |

## 播放器

生成的 M3U 兼容 VLC、PotPlayer、TiviMate、Kodi（PVR IPTV Simple Client）等。直播经 rtp2httpd 转 HTTP 单播 + FCC 秒切；支持 catchup 的播放器可回看时移。

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

[MIT](./LICENSE)
</content>
