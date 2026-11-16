#!/bin/bash
# 云服务器上由 cron 每 15 分钟调用一次；要不要生成由 digest.py --check 按美东时间判断，
# 每一版（8、10 … 22、0 点）只生成一次。日志：/root/pm-news-digest/data/server.log
set -u
cd "$(dirname "$0")/.." || exit 1
exec 9>/tmp/pm-news-digest.lock
flock -n 9 || exit 0   # 上一次还没跑完就跳过

PY=venv/bin/python
# 加 --force 可以不管时段立刻生成一版：server/run.sh --force
[ "${1:-}" = "--force" ] || [ "$($PY digest.py --check 2>/dev/null)" = "run=true" ] || exit 0

echo "==== $(TZ=America/New_York date '+%F %H:%M') ET"
git pull -q --rebase origin main || echo "[warn] git pull 失败"
$PY events/sync_events.py || echo "[warn] 日历同步失败，继续生成摘要"
CALENDAR_DB=events/calendar.db $PY digest.py --force --no-notify || exit 1

git add data events/calendar.db
git diff --cached --quiet && exit 0
git commit -q -m "摘要 $(TZ=America/New_York date '+%m-%d %H:%M') ET"
for i in 1 2 3; do
  git pull -q --rebase origin main && git push -q origin main && echo "已推送到 GitHub" && exit 0
  sleep 5
done
echo "[error] 推送失败"
exit 1
