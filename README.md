Lead Pulse Guard

Human-in-the-loop social comment triage. Catch every inbound comment on your Facebook Pages, draft a reply with an LLM, and let a human approve it before anything is published.


Most comment-automation tools are fully automatic. That is fine for a restaurant replying "thanks!". It is not fine when a real buyer asks about delivery time, certification, or price — an unsupervised model will invent a number, and that number is now public and attributed to your company.


Lead Pulse Guard is built around one rule: the model proposes, a human disposes.



Why this exists

Three failure modes killed our first attempts at automating comment replies. All three are handled structurally here, not by prompt engineering.


Failure	What happens without a guard	How it is stopped
Machine loop	Your bot replies to a comment. That reply arrives as a new webhook event. The bot replies to itself. Forever.	Identity check: if the commenter is one of your own Pages, the event is never eligible.
Politeness loop	A customer replies "thanks" to your answer. The bot answers again. They say "ok". It answers again.	Closing-phrase filter: whole-message matching, question-wins bias.
Confident fabrication	The model is asked about a spec it does not know, so it produces a plausible number.	Knowledge-base gating: if the fact is not in the approved KB, the model is instructed to defer to a human.



Architecture

   Facebook Page(s)
          │  webhook (POST, HMAC signed)
          ▼
 ┌──────────────────────┐
 │  meta-gateway        │  verify signature → tag page → decide eligibility
 │  (FastAPI, read-only)│  → append normalised JSON line to spool file
 └──────────────────────┘
          │  local file only. Zero outbound network capability.
          ▼
 ┌──────────────────────┐
 │  ai-worker           │  tail spool → load KB for that page → call LLM
 │  (systemd service)   │  → push an approval card to chat
 └──────────────────────┘
          │  chat card with [Send] [Edit & Send] [Ignore]
          ▼
      Human operator
          │  clicks Send
          ▼
   Graph API  ──►  reply published as the Page

The two services never talk over the network. The gateway writes a JSON line to a file; the worker tails it. That means:



The gateway holds no credential for any downstream system.

Compromising the gateway gets an attacker nothing but a write to a spool file.

The worker can be stopped, rewritten, or replaced without touching the webhook endpoint.

Every event is on disk and replayable.



What is in the box

Gateway (gateway/)



 X-Hub-Signature-256 HMAC verification over the raw request body

 Timing-safe comparison (hmac.compare_digest)

 Refuses to start if no App Secret is available — never degrades to accepting unverified traffic

 Page-ID tagging from an explicit registry

 Duplicate suppression (comment_id:verb, TTL)

 Thread-depth ceiling (default 3)

 Self-page filter (machine-loop prevention)

 Closing-phrase filter with question-wins bias (politeness-loop prevention)

 Append-only JSONL spool with size cap and rotation

 No outbound HTTP calls anywhere in the codebase


Worker (worker/)



 Spool tailer with persisted offset (survives restart, handles rotation)

 Per-page knowledge-base loading

 LLM call with a retry chain (cheap pool first, paid fallback second)

 Strict JSON output with a self-check block (answers_question, invented_specifics)

 Approval card push with three buttons

 Response-before-acting callback handling (avoids the "server didn't respond" retry that causes double-posting)

 Dry-run switch, on by default



Quick start

git clone https://github.com/<you>/lead-pulse-guard.git
cd lead-pulse-guard

cp .env.example .env
# fill in META_APP_SECRET, META_VERIFY_TOKEN, PAGE_REGISTRY

docker compose up -d gateway
docker compose logs -f gateway

Point your Meta app's webhook at https://your-domain/webhook, subscribe to the feed and leadgen fields at both levels (app-level and page-level — see docs/security.md, this trips up almost everyone), and hit Verify.


Then start the worker:


python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
AIW_POST_ENABLED=false ./venv/bin/python run.py

With AIW_POST_ENABLED=false the Send button reports "simulated send" and nothing reaches Facebook. Flip it to true only after you have watched a few drafts come through.



Documentation

Document	What it covers
docs/architecture.md	Service boundaries, data flow, why the spool is a file and not a queue
docs/security.md	Signature verification, the two-loop problem, KB gating, secrets layout
docs/deployment.md	systemd units, sandboxing, credential file modes


Non-goals

This is not a chatbot, not a CRM, and not a replacement for a human sales team. It is a triage layer. It decides whether a human should look at this comment, drafts something to save that human two minutes, and gets out of the way.


It also does not do lead scraping, does not touch Instagram DMs, and does not attempt to bypass any platform rate limit.



Status

Running in production on two industrial B2B Pages since September 2026. Roughly 30k events processed. The design decisions in docs/security.md are all answers to something that actually broke.


Treat this as reference code, not a turnkey product. The gatekeeping logic is worth copying; the deployment specifics will need adapting.



License

MIT. See LICENSE.

