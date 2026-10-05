#!/usr/bin/env bash
# 核准场次改签（reschedule）调用示例。启动服务后执行：bash examples/demo_reschedules.sh
#
# 演示主线：
#   录入两份授权（早到期 cap1 / 晚到期 cap5）→ 核准 2030-06-01 场次（命中早到期授权）
#   → 改签到 2031-07-01（早授权已失效，重新判定迁移到晚授权；旧授权释放、新授权占用）
#   → 查询当前排片（保留原核验快照）→ 同操作键重放（不重复迁移）→ 异内容 409
#   → 改签到时间窗外被拒（排片与占用不变，留痕可重放）→ 已领码/同时间/过去时间拒绝
#   → 改签历史 → 取消按当前授权释放
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-reschedule-demo"
VENUE="hall-rs"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入两份同影片同场馆授权：早到期(2031-06-01, 上限1) 与 晚到期(2031-12-01, 上限5) =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-06-01T00:00:00Z\",
  \"max_sessions\": 1, \"note\": \"改签演示-早到期授权\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-12-01T00:00:00Z\",
  \"max_sessions\": 5, \"note\": \"改签演示-晚到期授权\"
}" | pretty

echo
echo "== 2. 核准 2030-06-01 场次：优先命中早到期授权（剩余 0） =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"start_time\": \"2030-06-01T19:30:00Z\", \"idempotency_key\": \"rs-demo-1\"
}" | pretty

echo
echo "== 3. 改签（凭原核验键 rs-demo-1 + 操作键 rs-demo-1-move-1）到 2031-07-01 =="
echo "   新时间早授权已失效 -> 重新判定命中晚授权；旧授权释放一次、新授权占用一次"
curl -s -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-07-01T20:00:00Z",
       "operation_key":"rs-demo-1-move-1","reason":"主创行程调整，顺延一年"}' | pretty

echo
echo "== 4. 查询当前排片：顶层为改签后的时间/授权，original_* 为原核验快照，rescheduled=true =="
curl -s "$BASE/verifications/rs-demo-1" | pretty
echo "-- 授权实时占用：早授权已释放 used=0，晚授权占用 used=1 --"
curl -s "$BASE/licenses?film_id=$FILM" | pretty

echo
echo "== 5. 同操作键同内容重放：返回首次记录（replay=true），不重复迁移、不重复释放/占用 =="
curl -s -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-07-01T20:00:00Z","operation_key":"rs-demo-1-move-1"}' | pretty

echo
echo "== 6. 同操作键异内容冲突：409 CONFLICT（排片不变） =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-08-01T20:00:00Z","operation_key":"rs-demo-1-move-1"}'

echo
echo "== 7. 改签到时间窗外（2040 年，两份授权均不覆盖）：decision=rejected，原因 TIME_OUT_OF_WINDOW"
echo "   排片与占用不变（仍是 2031-07-01 / 晚授权），但落一条可追溯、可重放的改签流水"
curl -s -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2040-01-01T20:00:00Z","operation_key":"rs-demo-1-move-bad"}' | pretty
echo "-- 当前排片与占用确认未变 --"
curl -s "$BASE/verifications/rs-demo-1" | pretty
echo "-- 同一被拒操作键重放：replay=true，不新增流水 --"
curl -s -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2040-01-01T20:00:00Z","operation_key":"rs-demo-1-move-bad"}' | pretty

echo
echo "== 8. 门槛校验（均 422，不留改签记录） =="
echo "-- 新时间与当前排片相同：NEW_START_UNCHANGED --"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-07-01T20:00:00Z","operation_key":"rs-demo-1-move-same"}'
echo "-- 新时间不晚于当前时刻：NEW_START_NOT_IN_FUTURE --"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2020-01-01T00:00:00Z","operation_key":"rs-demo-1-move-past"}'
echo "-- 未知核验键：404 NOT_FOUND --"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/verifications/no-such-key/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-09-01T20:00:00Z","operation_key":"rs-demo-x"}'

echo
echo "== 9. 领取开场码后不可改签：先领码，再改签得到 422 OPENING_CODE_ALREADY_ISSUED =="
curl -s -X POST "$BASE/verifications/rs-demo-1/opening-code" | pretty
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/verifications/rs-demo-1/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-09-01T20:00:00Z","operation_key":"rs-demo-1-move-coded"}'

echo
echo "== 10. 改签历史（含被拒留痕的一条），可按原核验键/操作键/决定过滤 =="
curl -s "$BASE/reschedules?idempotency_key=rs-demo-1" | pretty

echo
echo "== 11. 另一场未领码的场次演示：成功改签两次（链式）后取消，按当前授权释放 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$VENUE\",
  \"start_time\": \"2030-06-10T19:30:00Z\", \"idempotency_key\": \"rs-demo-2\"
}" | pretty
curl -s -o /dev/null -X POST "$BASE/verifications/rs-demo-2/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-07-10T20:00:00Z","operation_key":"rs-demo-2-move-1"}'
curl -s -X POST "$BASE/verifications/rs-demo-2/reschedule" \
  -H 'Content-Type: application/json' \
  -d '{"new_start_time":"2031-08-10T20:00:00Z","operation_key":"rs-demo-2-move-2"}' | pretty
echo "-- 取消 rs-demo-2：license_id 为当前（晚到期）授权，晚授权占用回退 --"
curl -s -X POST "$BASE/verifications/rs-demo-2/cancel" \
  -H 'Content-Type: application/json' -d '{"reason":"改签后因故取消"}' | pretty
curl -s "$BASE/licenses?film_id=$FILM" | pretty
