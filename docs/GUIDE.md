# Flowdesk guide

How to run Flowdesk, send it tickets, review what it decides, and operate it when things go
wrong. For why it is built this way, see the [README](../README.md).

- [Setup](#setup)
- [Configuration](#configuration)
- [Sending tickets](#sending-tickets)
- [Ticket lifecycle](#ticket-lifecycle)
- [Reviewing tickets](#reviewing-tickets)
- [Dead letters and replay](#dead-letters-and-replay)
- [Operations](#operations)
- [Local development](#local-development)
- [Troubleshooting](#troubleshooting)

## Setup

You need Docker with Compose, and a Gemini API key from
[Google AI Studio](https://aistudio.google.com/apikey).

```bash
cp .env.example .env
# edit .env: set LLM_API_KEY, and change WEBHOOK_SECRET
docker compose up
```

Compose starts Postgres, Redis, the API (which applies database migrations first) and one
worker. The API is at http://localhost:8000, and its interactive docs are at
http://localhost:8000/docs.

If a host port is already taken, set `POSTGRES_PORT`, `REDIS_PORT` or `API_PORT` in `.env`.
These only change the port on your machine; the services still reach each other inside
Compose on the standard ports.

## Configuration

Everything is set with environment variables, normally in `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | required | `postgresql+asyncpg://…` (Compose sets this for its containers) |
| `REDIS_URL` | required | `redis://…` (Compose sets this for its containers) |
| `WEBHOOK_SECRET` | required | Shared secret the webhook expects in `X-Webhook-Secret` |
| `LLM_API_KEY` | required | Gemini API key; if blank, tickets are dead-lettered |
| `LLM_MODEL` | `gemini-3.5-flash` | Gemini model used to classify and summarise; a Pro model handles harder tickets at higher latency and cost. Pin a version rather than a `-latest` alias, so the model can't change underneath the cache. |
| `PROMPT_VERSION` | `v1` | Part of the cache key; bump it whenever the prompts change |
| `CONFIDENCE_THRESHOLD` | `0.70` | Below this, tickets go to human review |
| `MAX_ATTEMPTS` | `5` | LLM attempts per ticket before it is dead-lettered |
| `RETRY_BASE_DELAY` | `0.5` | Backoff base, in seconds |
| `RETRY_MAX_DELAY` | `30.0` | Backoff cap, in seconds |
| `QUEUE_MAX_DEPTH` | `1000` | Above this queue depth, the webhook returns 503 |
| `WORKER_CONCURRENCY` | `4` | Tickets each worker process handles at once |
| `CACHE_TTL_SECONDS` | `86400` | How long a cached decision lives |
| `BREAKER_THRESHOLD` | `5` | Consecutive transient failures before the breaker opens |
| `BREAKER_RESET_SECONDS` | `60.0` | How long the breaker stays open before a trial call |
| `POSTGRES_PORT`, `REDIS_PORT`, `API_PORT` | `5432`, `6379`, `8000` | Host ports Compose publishes |

## Sending tickets

Post a ticket to the webhook with the shared secret:

```bash
curl -i -X POST http://localhost:8000/webhooks/ticket \
  -H "X-Webhook-Secret: change-me" \
  -H "Content-Type: application/json" \
  -d '{"external_id": "zendesk-88213", "subject": "Charged twice",
       "body": "My card was billed twice for March.", "source": "zendesk"}'
```

| Field | Required | Notes |
| --- | --- | --- |
| `external_id` | yes | The ticket's ID in the source system, up to 128 characters. Used for de-duplication. |
| `body` | yes | The ticket text. |
| `subject` | no | Up to 512 characters. |
| `source` | no | Where it came from, up to 64 characters. |

Responses:

| Status | Meaning |
| --- | --- |
| `202` | Accepted. The body is `{"ticket_id", "status", "location", "duplicate"}`, and the `Location` header points at the ticket. |
| `202` with `"duplicate": true` | This `external_id` was already received. You get the original ticket back; nothing new is created or queued. Resending is always safe. |
| `401` | Missing or wrong `X-Webhook-Secret`. |
| `422` | The payload failed validation. The body says which field. |
| `503` | The queue is full. Retry after the number of seconds in `Retry-After`. |

The webhook never waits for the LLM. Check the result afterwards:

```bash
curl http://localhost:8000/tickets/41
curl "http://localhost:8000/tickets?status=awaiting_review&limit=20&offset=0"
```

`scripts/fire.py` sends many tickets at once and reports latency, for example
`python scripts/fire.py 50`. Add `--repeat` to send the same `external_id` every time.

## Ticket lifecycle

```
queued ──► processing ──► routed             confident, or approved by a reviewer
                     ├──► awaiting_review ──► routed / rejected
                     └──► failed           retries exhausted or permanent error (see dead letters)
```

Once a ticket is classified, its `classification` holds:

| Field | Meaning |
| --- | --- |
| `category` | `billing`, `technical`, `account`, `feature_request`, `complaint` or `other` |
| `summary` | One or two sentences for the agent |
| `confidence` | 0.0 to 1.0 |
| `routed_to` | The team. For tickets awaiting review, this is a suggestion. |
| `decided_by` | `llm`, `cache` (an identical ticket was seen recently) or `rules` (the provider was down) |
| `prompt_version` | The `PROMPT_VERSION` in force when it was decided |

`attempts` counts LLM attempts, and `last_error` holds the most recent failure.

## Reviewing tickets

Tickets wait in `awaiting_review` when the model's confidence is below `CONFIDENCE_THRESHOLD`,
and also when the breaker was open and they were routed by rules (those always have confidence
0.0).

```bash
# Everything waiting for a decision
curl "http://localhost:8000/tickets?status=awaiting_review"

# Accept the suggested team
curl -X POST http://localhost:8000/tickets/41/approve

# Accept, but send it somewhere else
curl -X POST http://localhost:8000/tickets/41/approve \
  -H "Content-Type: application/json" -d '{"routed_to": "customer-success"}'

# Reject it
curl -X POST http://localhost:8000/tickets/41/reject
```

The teams are `billing-team`, `engineering-support`, `account-team`, `product-team`,
`customer-success` and `general-support`; any other name returns `422`. Approving or rejecting a
ticket that isn't awaiting review returns `409`, so two reviewers can't both act on the same
ticket.

## Dead letters and replay

A ticket is dead-lettered when every attempt fails with a transient error (timeouts, 429, 5xx),
or straight away on a permanent error (other 4xx, a blocked prompt, a missing API key). Its status
becomes `failed`, and a row is added to `dead_letters`.

```bash
curl http://localhost:8000/dead-letters
curl -X POST http://localhost:8000/dead-letters/7/replay    # re-queue with attempts reset to 0
```

Fix the cause before you replay. For example, if the error says `LLM_API_KEY is not set`, set
the key and restart the worker (`docker compose up -d worker`), then replay.

## Operations

**Health.** `GET /healthz` reports that the process is up. `GET /readyz` checks Postgres and
Redis and returns the queue depth, or `503` if either one is unreachable.

**Scaling workers.** Run more worker processes: `docker compose up -d --scale worker=4`.
Each one handles up to `WORKER_CONCURRENCY` tickets at a time.

**Stopping workers.** On `SIGTERM` (which `docker compose stop` sends), a worker stops claiming
new jobs and finishes the ones it has. If it is killed outright, its jobs stay in its processing
list and another worker reclaims them within about 30 seconds of its heartbeat expiring.

**Changing the prompts.** Edit the prompts in `app/llm.py` and bump `PROMPT_VERSION`. Cached
decisions from the old prompt stop matching, and every classification records the version it
was made with.

**Inspecting Redis.** These keys show what the queue and breaker are doing:

```bash
docker compose exec redis redis-cli llen flowdesk:ready              # queue depth
docker compose exec redis redis-cli --scan --pattern 'flowdesk:processing:*'
docker compose exec redis redis-cli exists flowdesk:breaker:open     # 1 = provider considered down
docker compose exec redis redis-cli get flowdesk:breaker:failures
```

**Stuck tickets.** A ticket left untouched in `queued` or `processing` for 15 minutes is
re-enqueued automatically by the sweeper. This covers an API crash between saving a ticket and
queueing it.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
docker compose up -d postgres redis

# Point DATABASE_URL and REDIS_URL in .env at localhost, then:
alembic upgrade head
uvicorn app.main:app --reload       # API
python -m app.worker                # worker, in another terminal

pytest                              # tests (uses a separate flowdesk_test database)
ruff check . && ruff format .       # lint and format
```

The tests read `POSTGRES_PORT` and `REDIS_PORT` from `.env`. Set `TEST_DATABASE_URL` or
`TEST_REDIS_URL` to point them somewhere else.

The code is organised like this:

| Module | Responsibility |
| --- | --- |
| `app/main.py` | HTTP API: webhook, tickets, review, dead letters, health |
| `app/queue.py` | Redis queue: enqueue, claim (`BLMOVE`), ack, heartbeat, reaper |
| `app/worker.py` | Worker process: claim loop, concurrency limit, maintenance |
| `app/processor.py` | One ticket: cache, retries, breaker, fallback, persistence |
| `app/flow.py` | LangGraph triage flow |
| `app/llm.py` | Gemini client, prompts, error classification, routing table |
| `app/cache.py`, `app/breaker.py`, `app/rules.py`, `app/retry.py` | Decision cache, circuit breaker, rules fallback, backoff |
| `app/models.py`, `migrations/` | Database schema |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `docker compose up` fails with "port is already allocated" | Set `POSTGRES_PORT`, `REDIS_PORT` or `API_PORT` in `.env`. |
| Every ticket ends up `failed` with `LLM_API_KEY is not set` | Set the key in `.env`, run `docker compose up -d worker`, then replay the dead letters. |
| Errors starting with `400:`, `403:` or `404:` in dead letters | An invalid API key (400 "API key not valid"), a key without access (403) or an unknown `LLM_MODEL` (404). These are permanent errors, so they aren't retried. |
| `last_error` shows `503: This model is currently experiencing high demand` | Gemini is overloaded. This is transient, so the ticket is retried with backoff; if it persists, the breaker opens and tickets route by rules to review. |
| Everything lands in review with `decided_by: rules` | The breaker is open because the provider is failing. It retries on its own after `BREAKER_RESET_SECONDS`. |
| The webhook returns `503` | The queue has reached `QUEUE_MAX_DEPTH`. Add workers, or check that the worker is running. |
| Tickets stay `queued` | No worker is running: check `docker compose ps` and `docker compose logs worker`. |
