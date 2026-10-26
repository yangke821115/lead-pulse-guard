# Lead Pulse Guard

**Human-in-the-loop lead triage.** Catch every inbound lead — Facebook comments, Lead Ads form submissions, and email — draft a reply with an LLM, and let a human approve it before anything is published.

Most comment-automation tools are fully automatic. That is fine for a restaurant replying "thanks!". It is not fine when a real buyer asks about delivery time, certification, or price — an unsupervised model will invent a number, and that number is now public and attributed to your company.

Lead Pulse Guard is built around one rule: **the model proposes, a human disposes.**

> **中文说明：** [docs/README.zh-CN.md](docs/README.zh-CN.md) — 面向业务场景与部署，和技术版的侧重不同。

---

## Why this exists

Three failure modes killed our first attempts at automating comment replies. All three are handled structurally here, not by prompt engineering.

| Failure | What happens without a guard | How it is stopped |
|---|---|---|
| **Machine loop** | Your bot replies to a comment. That reply arrives as a *new* webhook event. The bot replies to itself. Forever. | Identity check: if the commenter is one of your own Pages, the event is never eligible. |
| **Politeness loop** | A customer replies "thanks" to your answer. The bot answers again. They say "ok". It answers again. | Closing-phrase filter: whole-message matching, question-wins bias. |
| **Confident fabrication** | The model is asked about a spec it does not know, so it produces a plausible number. | Knowledge-base gating: if the fact is not in the approved KB, the model is instructed to defer to a human. |

---

## Three ways a lead arrives

The name of the project is the requirement: **do not let an inbound lead fall on the floor.** In practice a B2B seller
gets approached through three unrelated channels, each with its own delivery mechanism, its own failure mode, and its
own idea of what "a message" looks like.

| Channel | Arrives as | Delivery | Silent failure mode |
|---|---|---|---|
| **Page comments** | `feed` / `comment` / `add` | webhook | Missed events; replies arrive as new events (self-loop) |
| **Lead Ads forms** | `leadgen` event, then a Graph fetch | webhook | **Events silently never delivered** — the form fills up, nobody is told |
| **Email** | MIME message in an IMAP folder | polling | Nothing — polling cannot miss what is on the server |

The third column is the part that bites. A webhook that stops arriving looks exactly like a quiet day.

## Architecture

```
                         Facebook                       Mailbox
            ┌────────────────┴──────────────┐               │
            │ comments              Lead Ads │               │  IMAP
            │ feed webhook          leadgen  │               │  read-only,
            └────────────────┬──────────────┘               │  never marks read
                             │                               │
        ╔════════════════════▼═══════════════╗   ╔═══════════▼═══════════╗
        ║  gateway                           ║   ║  mail poller          ║
        ║  verify HMAC over raw body         ║   ║  classify: spam?      ║
        ║  tag page → dedupe → eligibility   ║   ║  which business unit? ║
        ╚════════════════════╤═══════════════╝   ╚═══════════╤═══════════╝
                             │ append-only JSONL             │
                             ▼                               ▼
        ╔════════════════════════════════════════════════════════════════╗
        ║  worker                                                        ║
        ║    comment → KB for that page → draft reply                    ║
        ║    lead    → score + route → row in table + card               ║
        ║    email   → summary + routing → card                          ║
        ╚════════════════════════════════╤═══════════════════════════════╝
                                         │ approval card
                                         ▼
                                   Human operator
                                         │ Send / Edit & Send / Ignore
                                         ▼
                              Graph API ──► published as the Page
```

The three lanes converge on one approval surface: a chat card with the original message, the model's draft, and a
human decision. Nothing is published automatically, on any channel.

### Why the gateway and the worker never talk over the network

The gateway writes a JSON line to a file; the worker tails it. That means:

- The gateway holds **no credential** for any downstream system.
- Compromising the gateway gets an attacker nothing but a write to a spool file.
- The worker can be stopped, rewritten, or replaced without touching the webhook endpoint.
- Every event is on disk and replayable.

### Webhooks are not a delivery guarantee, so there is a second path

While building this we measured a day on which five Lead Ads submissions arrived and **only two produced a webhook
event**. The other three existed on Meta's side and were invisible to us. Nothing was logged, nothing errored — a
quiet day and a broken day are indistinguishable from the receiving end.

So every webhook-fed channel also has a poller:

```
        webhook ──┐
                  ├──► same idempotent entry point ──► one card, never two
        poller  ──┘     (claim by activity id, atomic)
```

- **Lead poller** — every 10 minutes, list forms, page through submissions, feed anything the local store has not
  claimed. Same code path as the webhook, so scoring, routing and card format are identical.
- **Comment poller** — same idea, but it does **not** re-implement eligibility. It re-signs the comment as a webhook
  and POSTs it to the gateway, so the self-loop filter, dedupe and closing-phrase logic run in exactly one place.

The claim is atomic, so whichever path gets there first wins and the other becomes a no-op. The poller is not a
replacement for the webhook — the webhook is still faster. The poller only bounds the damage: worst case moves from
"never" to "ten minutes".

---

## What is in the box

**Gateway** (`gateway/`)
- [x] `X-Hub-Signature-256` HMAC verification over the **raw request body**
- [x] Timing-safe comparison (`hmac.compare_digest`)
- [x] Refuses to start if no App Secret is available — never degrades to accepting unverified traffic
- [x] Page-ID tagging from an explicit registry
- [x] Duplicate suppression (`comment_id:verb`, TTL)
- [x] Thread-depth ceiling (default 3)
- [x] Self-page filter (machine-loop prevention)
- [x] Closing-phrase filter with question-wins bias (politeness-loop prevention)
- [x] Append-only JSONL spool with size cap and rotation
- [x] No outbound HTTP calls anywhere in the codebase

**Worker** (`worker/`)
- [x] Spool tailer with persisted offset (survives restart, handles rotation)
- [x] Lead-event path: field extraction, scoring, business-unit routing, optional table write
- [x] Form option mapping kept in an external JSON file — no customer field names in the source
- [x] Per-page knowledge-base loading
- [x] LLM call with a retry chain (cheap pool first, paid fallback second)
- [x] Strict JSON output with a self-check block (`answers_question`, `invented_specifics`)
- [x] Approval card push with three buttons
- [x] Response-before-acting callback handling (avoids the "server didn't respond" retry that causes double-posting)
- [x] Dry-run switch, on by default

**Fallback pollers** (`worker/leadpoll.py`, `worker/commentpoll.py`)
- [x] Lead poller: list forms → page submissions → feed anything not yet claimed
- [x] Comment poller: re-signs each comment as a webhook and posts it to the gateway, so eligibility stays in one place
- [x] Atomic claim on the activity ID — webhook and poller can race, only one card is produced
- [x] `--dry-run` on both: report what is missing without processing it

**Email channel** (`worker/mail.py`)
- [x] Read-only IMAP polling — every fetch uses `BODY.PEEK`, so mail is never marked read, moved or deleted
- [x] Spam triage first, then business-unit routing by content rather than by inbox
- [x] Dedupe across restarts (UID watermark + Message-ID set); the first run establishes a baseline instead of backfilling history
- [x] Outbound mail detection — our own sent mail is not treated as an inbound lead

---

## Quick start

```bash
git clone https://github.com/<you>/lead-pulse-guard.git
cd lead-pulse-guard

cp .env.example .env
# fill in META_APP_SECRET and META_VERIFY_TOKEN
cp pages.example.json pages.json
# then put your real Page IDs in pages.json

docker compose up -d gateway
docker compose logs -f gateway
```

Point your Meta app's webhook at `https://your-domain/webhook`, subscribe to the `feed` and `leadgen` fields **at both levels** (app-level *and* page-level — see `docs/security.md`, this trips up almost everyone), and hit Verify.

Then start the worker:

```bash
python3 -m venv venv
./venv/bin/pip install -r worker/requirements.txt
./venv/bin/python -m worker.main
```

`worker/requirements.txt` is the only dependency file — the rest is standard library. The worker ships a
minimal Feishu SDK shim so the official client does not load every REST endpoint at import time (measured to
save about 120 MB resident).

With `AIW_POST_ENABLED=false` the Send button reports "simulated send" and nothing reaches Facebook. Flip it to `true` only after you have watched a few drafts come through.

---

### Language

Customer replies follow the commenter's language. If the comment has no detectable language, the Page's `language`
in `pages.json` is used as the fallback.

Rules, prompts, code, comments and logs are English-only (`worker/prompts/*.en.txt`). Operator-facing Lark cards
default to English; set `AIW_CARD_LANG=zh` to render them in Chinese. Enum values stored in the database stay
language-neutral either way, and older Chinese values are accepted on read.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Service boundaries, data flow, why the spool is a file and not a queue |
| [`docs/security.md`](docs/security.md) | Signature verification, the two-loop problem, KB gating, secrets layout |
| [`docs/deployment.md`](docs/deployment.md) | systemd units, sandboxing, credential file modes |

---

## Non-goals

This is not a chatbot, not a CRM, and not a replacement for a human sales team. It is a triage layer. It decides *whether a human should look at this comment*, drafts something to save that human two minutes, and gets out of the way.

It also does not do lead scraping, does not touch Instagram DMs, and does not attempt to bypass any platform rate limit.

---

## Status

Running in production on several industrial B2B Pages since September 2026. Roughly 30k events processed. The design decisions in `docs/security.md` are all answers to something that actually broke.

Treat this as **reference code**, not a turnkey product. The gatekeeping logic is worth copying; the deployment specifics will need adapting.

---

## Sponsorship

This project is MIT-licensed and will stay that way. Sponsorship does not buy a different licence, priority
support, or a vote on the roadmap — everything in this repository is already given away, and paid work is a
separate thing (see below).

Sponsorship pays for the parts that nobody enjoys doing and everybody depends on:

| What it funds | Why it matters |
|---|---|
| **Correctness work** — signature edge cases, poller idempotency, mutation tests | A filter that is silently inverted still returns HTTP 200 and still looks healthy |
| **A demo instance with a Meta test app** | Some failures only appear against a real app: duplicate deliveries, reordered events, a webhook that simply never arrives |
| **Keeping the docs true** | A README describing a system that no longer exists is worse than no README |

### How to sponsor

**Alipay** is live today — send to `yangke821115@gmail.com`. Put "lead-pulse-guard" in the note so it can be
matched up. Any amount, one-off. There is no recurring billing and nothing to sign up for.

| Channel | Status | Suited to |
|---|---|---|
| **Alipay** — `yangke821115@gmail.com` | **available now** | one-off, mainland China |
| GitHub Sponsors | not live yet | recurring, international |
| Invoice / 对公转账 | on request | companies that need a receipt |

Alipay is not a card payment, so it cannot be embedded as a button in the repository. Transfer from the Alipay
app or scan the address at [leadpulseguard.com](https://leadpulseguard.com#sponsor).

For GitHub Sponsors, 爱发电, or an invoice — open an issue and we will follow up. Companies that need a receipt
should ask for an invoice rather than sending an Alipay transfer, so the accounting works on both sides.

### For companies

If you run B2B lead generation on Facebook or by email and want this adapted to your own Pages and mailboxes, we do
paid setup and hosting as a service. That is also where most of the failure modes in this repository came from: real
traffic, not synthetic tests.

**What sponsorship does not include:** a licence change, support guarantees, or influence over what gets built. Those
are not for sale here.

### If you cannot sponsor

Tell us about a failure we have not handled. A report that says *"the event arrived, no card was pushed, here is the
timestamp and the page ID"* is worth more than the money — it turns into a test case that keeps the bug from coming
back.

### Sponsors

None yet. This section will list sponsors once the channels are live.

## License

MIT. See [`LICENSE`](LICENSE).
