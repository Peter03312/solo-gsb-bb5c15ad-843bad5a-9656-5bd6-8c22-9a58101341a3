#!/usr/bin/env bash
# 一次性开场码领取与场馆核销调用示例。启动服务后执行：bash examples/demo_opening_codes.sh
#
# 本脚本实时运行：以"当前时刻 + 少量秒数"构造计划开场时刻，
# 完整演示 领取 → 查询只见状态 → 到场核销 → 回执重放 → 取消失效 全链路。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-opening-demo"

# 相对当前时刻的 UTC ISO-8601 时间（秒），如 ts 30 -> 30 秒后
ts() {
  python3 -c "
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(seconds=$1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
"
}

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入授权（影片 $FILM / hall-1，窗口覆盖当下） =="
FROM=$(ts -60); TO=$(ts 86400)
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"$FROM\", \"valid_to\": \"$TO\",
  \"max_sessions\": 5, \"note\": \"开场码演示授权\"
}" | pretty

echo
echo "== 2. 核准场次 A（开场时刻 = 当前 + 4 秒） =="
START_A=$(ts 4)
echo "    start_time = $START_A"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"$START_A\", \"idempotency_key\": \"oc-demo-A\"
}" | pretty

echo
echo "== 3. 运营方领取一次性开场码（首次领取，code 仅此一次返回） =="
CLAIM_A=$(curl -s -X POST "$BASE/verifications/oc-demo-A/opening-code")
echo "$CLAIM_A" | pretty
CODE_A=$(echo "$CLAIM_A" | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])")

echo
echo "== 4. 重复领取：replay=true、code=null，只见状态不泄露原码 =="
curl -s -X POST "$BASE/verifications/oc-demo-A/opening-code" | pretty

echo
echo "== 5. 状态查询（GET）：不含 code 字段，当前 issued =="
curl -s "$BASE/verifications/oc-demo-A/opening-code" | pretty

echo
echo "== 6. 开场未到，场馆提前核销被拒绝：422 REDEMPTION_TOO_EARLY，无回执 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/redemptions" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE_A\", \"venue_id\": \"hall-1\"}"

echo
echo "== 7. 错场馆核销被拒绝：422 VENUE_MISMATCH，无回执 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/redemptions" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE_A\", \"venue_id\": \"hall-9\"}"

echo
echo "== 8. 等待到达计划开场时刻（约 5 秒）..."
sleep 5

echo "== 9. hall-1 在 30 分钟窗口内核销：返回场次、实际核销时间、关联授权 =="
RECEIPT=$(curl -s -X POST "$BASE/redemptions" -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE_A\", \"venue_id\": \"hall-1\"}")
echo "$RECEIPT" | pretty
RECEIPT_ID=$(echo "$RECEIPT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo
echo "== 10. 同一码重试核销（即使之后已过窗口）：返回首次回执 replay=true，不重复记场次 =="
curl -s -X POST "$BASE/redemptions" -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE_A\", \"venue_id\": \"hall-1\"}" | pretty

echo
echo "== 11. 已核销码用错场馆重试：仍 422 VENUE_MISMATCH，不泄露回执 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/redemptions" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE_A\", \"venue_id\": \"hall-9\"}"

echo
echo "== 12. 状态查询变为 redeemed 并附首次回执（仍无 code） =="
curl -s "$BASE/verifications/oc-demo-A/opening-code" | pretty
echo "-- 全库回执只有一条（id=$RECEIPT_ID），核销不改变额度占用 --"
curl -s "$BASE/licenses?film_id=$FILM" | pretty

echo
echo "== 13. 未知码核销：404 OPENING_CODE_NOT_FOUND，不产生回执 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/redemptions" \
  -H 'Content-Type: application/json' \
  -d '{"code": "OC-000000000000000000000000", "venue_id": "hall-1"}'

echo
echo "== 14. 场次 B：领取码后取消，码立即失效（voided），核销被拒 =="
START_B=$(ts 600)
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"$START_B\", \"idempotency_key\": \"oc-demo-B\"
}" >/dev/null
CLAIM_B=$(curl -s -X POST "$BASE/verifications/oc-demo-B/opening-code")
CODE_B=$(echo "$CLAIM_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])")
echo "    已领码: $CODE_B"
curl -s -X POST "$BASE/verifications/oc-demo-B/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "导演缺席，场次取消"}' >/dev/null
echo "-- 状态查询：voided，含 cancelled_at，无 code --"
curl -s "$BASE/verifications/oc-demo-B/opening-code" | pretty
echo "-- 重复领取：仍 replay=true、code=null、status=voided --"
curl -s -X POST "$BASE/verifications/oc-demo-B/opening-code" | pretty
echo "-- 用已失效的码核销：422 SESSION_CANCELLED --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/redemptions" \
  -H 'Content-Type: application/json' \
  -d "{\"code\": \"$CODE_B\", \"venue_id\": \"hall-1\"}"
echo "-- 取消后重复领取：已领过码，只回 voided 状态（200，replay=true，code=null） --"
echo "-- （取消前从未领过码的场次，首次领取才返回 422 SESSION_CANCELLED） --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/oc-demo-B/opening-code"

echo
echo "== 15. 未领取码的已核准场次：状态 not_issued =="
START_C=$(ts 1800)
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"$START_C\", \"idempotency_key\": \"oc-demo-C\"
}" >/dev/null
curl -s "$BASE/verifications/oc-demo-C/opening-code" | pretty

echo
echo "== 16. 已到/过开场时刻的场次不可再领码：422 SESSION_ALREADY_STARTED =="
PAST=$(ts -1)
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"$PAST\", \"idempotency_key\": \"oc-demo-past\"
}" >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/oc-demo-past/opening-code"

echo
echo "== 17. 被拒绝的流水无开场码：422 VERIFICATION_NOT_APPROVED =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"no-such-film\", \"venue_id\": \"no-hall\",
  \"start_time\": \"$START_C\", \"idempotency_key\": \"oc-demo-rej\"
}" >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/oc-demo-rej/opening-code"

echo
echo "演示完成。"
echo
echo "提示：核销窗口为左闭右开 [start_time, start_time+30min)——恰在开场时刻核销"
echo "      成功，早于开场 REDEMPTION_TOO_EARLY，开场后第 30 分钟起"
echo "      REDEMPTION_WINDOW_CLOSED（边界由 tests/test_opening_codes.py 覆盖）。"
echo "      已成功核销的码不受窗口关闭影响：重试恒返回首次回执（replay=true）。"
