#!/usr/bin/env bash
# Walk through the main path: webhook -> 202 -> worker -> review -> approval.
# Needs the stack running (docker compose up) and LLM_API_KEY set in .env.
set -euo pipefail

URL="${FLOWDESK_URL:-http://localhost:8000}"
SECRET="${WEBHOOK_SECRET:-change-me}"
RUN="demo-$(date +%s)"

post() {
  curl -s -X POST "$URL/webhooks/ticket" \
    -H "X-Webhook-Secret: $SECRET" -H "Content-Type: application/json" -d "$1"
}
field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
pretty() { python3 -m json.tool; }

wait_for() {  # wait until the ticket leaves queued/processing
  for _ in $(seq 1 60); do
    status=$(curl -s "$URL/tickets/$1" | field '["status"]')
    [[ "$status" != queued && "$status" != processing ]] && return
    sleep 1
  done
}

echo "== 1. A clear ticket: accepted immediately with 202"
clear=$(post "{\"external_id\":\"$RUN-clear\",\"subject\":\"Charged twice\",\"body\":\"My card was billed twice for the March invoice. Please refund one charge.\",\"source\":\"demo\"}")
echo "$clear" | pretty
clear_id=$(echo "$clear" | field '["ticket_id"]')

echo "== 2. The same delivery again: idempotent, no second ticket"
post "{\"external_id\":\"$RUN-clear\",\"body\":\"duplicate\"}" | pretty

echo "== 3. A vague ticket the model should be unsure about"
vague=$(post "{\"external_id\":\"$RUN-vague\",\"body\":\"hey so the thing from before, can you sort it? also the other matter\",\"source\":\"demo\"}")
vague_id=$(echo "$vague" | field '["ticket_id"]')

echo "== 4. The worker picks both up"
wait_for "$clear_id"; wait_for "$vague_id"
curl -s "$URL/tickets/$clear_id" | pretty

echo "== 5. Low confidence lands in human review"
curl -s "$URL/tickets?status=awaiting_review&limit=5" | pretty

echo "== 6. A reviewer approves it, choosing the team"
curl -s -X POST "$URL/tickets/$vague_id/approve" \
  -H "Content-Type: application/json" -d '{"routed_to":"general-support"}' | pretty
