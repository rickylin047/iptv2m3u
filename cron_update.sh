#!/bin/sh
# IPTV M3U 定时更新脚本 — 在 VM101 (iStoreOS) 上通过 cron 执行
#
# 安装方法 (SSH 到 VM101):
#   chmod +x /root/iptv2m3u/cron_update.sh
#   crontab -e
#   # 每天 20:00 探测编码+更新直播源（黄金时段，频道覆盖率最高）
#   0 20 * * * /root/iptv2m3u/cron_update.sh --probe
#   # 每天 08:00 仅更新直播源（刷新认证令牌和 FCC 地址）
#   0 8 * * * /root/iptv2m3u/cron_update.sh

WORKDIR="/root/iptv2m3u"
PYTHON="$WORKDIR/.venv/bin/python"
SCRIPT="$WORKDIR/generate_m3u.py"
LOGFILE="$WORKDIR/cron.log"
MAX_LOG_LINES=2000
MAX_JITTER=300  # 最大随机延迟秒数（5分钟），避免固定时间请求

cd "$WORKDIR" || exit 1

# ---- 磁盘守护 (2026-04-23 新增) ----
# 1. 清理遗留的调试文件（>1 天未修改的 pcap/ts）
find /tmp -maxdepth 1 \( -name '*.pcap' -o -name '*.ts' \) -mtime +1 -delete 2>/dev/null

# 2. /tmp 使用率 >80% 时中止（避免继续写日志加重爆盘）
TMP_USE=$(df /tmp | awk 'NR==2 {gsub("%","",$5); print $5+0}')
if [ "$TMP_USE" -gt 80 ]; then
    printf '[FAIL] %s /tmp 使用率 %s%%，中止执行\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$TMP_USE" >> "$LOGFILE"
    exit 2
fi

# 随机延迟 0~MAX_JITTER 秒
jitter=$(awk "BEGIN{srand(); printf \"%d\", rand()*$MAX_JITTER}")
echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 随机延迟 ${jitter}s" >> "$LOGFILE"
sleep "$jitter"

# 构建参数
ARGS=""
case "$1" in
    --probe) ARGS="--probe" ;;
esac

# 执行
echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] 开始更新 (args: $ARGS)" >> "$LOGFILE"
$PYTHON "$SCRIPT" $ARGS >> "$LOGFILE" 2>&1
RC=$?
if [ "$RC" -eq 0 ]; then
    printf '%s [INFO] 完成 (exit=0)\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOGFILE"
else
    # 失败时在日志段顶部再写一行 [FAIL] 摘要，便于 head/grep 快速判断
    printf '%s [FAIL] 本次执行失败 (exit=%s) — 上面是 traceback\n\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$RC" >> "$LOGFILE"
fi

# 日志轮转：保留最近 MAX_LOG_LINES 行
if [ "$(wc -l < "$LOGFILE")" -gt "$MAX_LOG_LINES" ]; then
    tail -n "$MAX_LOG_LINES" "$LOGFILE" > "$LOGFILE.tmp"
    mv "$LOGFILE.tmp" "$LOGFILE"
fi

exit $RC
