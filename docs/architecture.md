# Architecture

## The shape of the problem

A Facebook Page receives comments. Meta delivers them to a single HTTPS endpoint as webhook events. The vendor wants an LLM to draft a reply, and a human to approve it.

Everything else in this document follows from three constraints:

1. **The webhook endpoint is public.** Anyone on the internet can POST to it. Signature verification is not optional.
2. **The webhook endpoint can be spoofed into triggering a publish.** If it is compromised, the blast radius must be "someone can write a line to a file", not "someone can post as the brand".
3. **Replies are public and permanent.** A wrong number in a public comment costs more than a slow reply. So the model must never be the last step.

## Two processes, one file

```
┌─────────────────────────────────────────────────────────┐
│  gateway (container, read-only filesystem)              │
│                                                          │
│  POST /webhook                                           │
│    ├─ verify X-Hub-Signature-256 over raw body           │
│    ├─ if invalid → 403, body NOT logged                  │
│    ├─ tag page_id from explicit registry                 │
│    ├─ decide: reply candidate? yes / no + reason         │
│    └─ append one JSON line to /spool/events.jsonl        │
│                                                          │
│  network: inbound only. zero outbound calls.             │
│  secrets: the Meta App Secret. nothing else.             │
└─────────────────────────────────────────────────────────┘
                          │
                          │  /spool/events.jsonl   (bind mount)
                          ▼
┌─────────────────────────────────────────────────────────┐
│  worker (systemd service, non-root)                      │
│                                                          │
│  loop every 2s:                                          │
│    ├─ read from persisted offset                         │
│    ├─ for each complete line:                            │
│    │    ├─ dedup (comment_id, 6h window)                 │
│    │    ├─ load KB for this page slug                    │
│    │    ├─ call LLM → strict JSON                        │
│    │    ├─ self-check: did it answer? did it invent?     │
│    │    ├─ store draft in pending.json                   │
│    │    └─ push chat card with [Send][Edit][Ignore]      │
│    └─ persist offset                                     │
│                                                          │
│  on button click:                                        │
│    ├─ verify operator identity against allowlist         │
│    ├─ remove from pending.json  ← BEFORE the API call    │
│    ├─ return ack to chat client  ← BEFORE the API call   │
│    └─ background thread → Graph API → update card        │
└─────────────────────────────────────────────────────────┘
```

### Why a file and not a queue

Redis, RabbitMQ, and SQS are all better queues than a file. We chose the file anyway.

**Because it changes what the gateway is allowed to become.** A file spool means the gateway has no downstream address to be configured with, no broker credential to leak, no connection pool to exhaust. The reader can be down for an hour and the gateway will not notice — it just appends.

The spool is also a free audit log. When something looks wrong, the entire history of what the gateway decided, and why, is one `cat` away. With a queue you would need to add a dead-letter handler and a schema registry to get the same visibility.

The costs are real and worth stating:

- **Single consumer.** More than one worker would need locking; we do not support that.
- **No backpressure.** The gateway will happily write until the size cap, then rotate. A fundamentally broken consumer will lose events rather than slow the producer.
- **Local filesystem only.** This assumes both processes share a host. That is a deliberate deployment constraint.

If you outgrow these, replace the spool with a broker — but keep the gateway's *inability to call downstream services* as an invariant.

### Event schema

```json
{
  "schema": "meta.comment/v1",
  "received_at": "2026-10-13T09:47:12.331Z",
  "page": { "id": "...", "name": "...", "kb": "example-slug", "mapped": true },
  "comment": {
    "id": "...", "parent_id": "...", "post_id": "...",
    "message": "...", "created_time": 1790174884,
    "permalink_url": "...", "item": "comment", "verb": "add"
  },
  "author": { "id": "...", "name": "..." },
  "post": { "id": "...", "status_type": "added_video", "permalink_url": "..." },
  "decision": { "reply": true, "reason": "question", "detail": "" }
}
```

`decision.reason` is one of:

| reason | meaning |
|---|---|
| `question` | contains a question marker — always eligible |
| `content` | substantive text, no question marker — eligible |
| `no_text` | image/empty comment — eligible (a human decides) |
| `closing_phrase` | pure courtesy — not eligible, but still surfaced to the operator as a grey notice |
| `self_page` | commenter is one of our own Pages — hard skip |
| `duplicate` | `comment_id:verb` seen within TTL |
| `depth_limit` | thread already has N replies and not resolved |
| `not_a_new_comment` | `item != comment` or `verb != add` |
| `missing_comment_id` | malformed event |

The gateway logs the decision either way. `reply: false` does not mean the event is dropped — it means no *card with buttons* is raised.

## Eligibility: the two-loop model

This is the part worth stealing even if you throw away the rest.

There are two independent loops that will make a comment bot talk to itself, and **neither check catches the other one.**

### Loop 1 — machine

```
bot replies → Meta delivers the reply as a new webhook event → bot replies to itself
```

A reply published by one of your own Pages is a perfectly valid `comment`/`add` event from a perfectly valid Page. Nothing in the payload says "this is mine" except `author.id`, which equals one of your own Page IDs.

**Fix:** maintain a set of your Page IDs. If `author.id ∈ that set`, the event is `self_page` and can never be a reply candidate.

This is structural. It does not depend on the model, on prompts, or on a counter. It is a set membership test that runs before anything else.

### Loop 2 — human politeness

```
bot answers → customer says "thanks" → bot answers → customer says "ok" → ...
```

Every message in this loop is a genuine new comment from a genuine human. `author.id` is not one of your Pages. The identity check above will pass all of them.

**Fix:** a closing-phrase filter, with a deliberate bias.

The filter normalises the message (strip punctuation, whitespace, emoji, case), then strips known courtesy tokens and filler particles repeatedly until stable. If nothing meaningful remains, the message is a pure acknowledgement.

**The bias matters more than the algorithm.** Two error directions are not symmetric:

| Error | Cost |
|---|---|
| Skip a real question | Customer never gets answered. Revenue lost. **Damaging.** |
| Reply when unnecessary | One extra "you're welcome". **Cosmetic.** |

So the filter is built to fail toward replying:

1. **A question always wins.** If the *original* message contains `?`, `？`, or an interrogative word, it is eligible regardless of the closing-phrase analysis. Normalisation strips the punctuation, so this check runs against the raw string.
2. **Whole-message, not substring.** `"thanks, but what is the radiation dose?"` contains a courtesy token but is obviously a live question. Substring matching would eat it.
3. **Strip iteratively.** `"ok thanks teacher"` → strip `thanks` → strip `ok` → strip `teacher` → empty → not eligible.

Word lists are in `gateway/app.py` as `CLOSING_TOKENS` and `FILLER_TOKENS`. They are data, not logic — extend them freely.

## Thread depth

`MAX_REPLY_DEPTH` (default 3) limits how many times we will answer inside one thread. To count depth, the gateway must know which comment is the thread root.

`_resolve_root()` walks `parent_id` upward through a small in-memory map of comments we have seen. When one of our own replies is published, `_record_self_reply()` increments the counter for that root.

**This only works because a Page's own replies are also delivered as webhook events.** That was an assumption when this was written, and it was later confirmed empirically: a reply published via the Graph API arrived back at the webhook roughly 5 seconds later with `author.id` equal to the Page. If your subscription does not deliver self-replies, depth stays at zero and only the closing-phrase filter is protecting you.

## The LLM layer

### Retry chain

```
attempt 1: free model pool   (cheap, occasionally returns empty or truncated JSON)
attempt 2: free model pool   (different backend — not the same failure repeated)
attempt 3: paid model        (reliable, slower, costs money)
```

Retrying the free pool is worthwhile *because* the router picks a different backend each time. Two failures in a row are close to independent. This took measured success from ~87% to ~98% in our traffic.

### Output contract

The model returns strict JSON:

```json
{
  "intent": "pricing | technical | after_sales | spam | other",
  "stage": "hot | warm | cold",
  "action": "reply | forward_to_sales | ignore",
  "language": "en | zh | ...",
  "reply": "...",
  "self_check": {
    "answers_question": true,
    "cites_only_kb": true,
    "invented_specifics": false
  }
}
```

`self_check` is not decoration. `invented_specifics: true` is treated as a hard failure and downgrades the card to "needs manual handling". It is a cheap second opinion from the model about its own output — not a guarantee, which is why a human still clicks the button.

### Knowledge-base gating

Each Page maps to a KB slug. The worker loads four files:

| File | Injected into the prompt? | Purpose |
|---|---|---|
| `confirmed-facts.md` | yes | Facts the model may state |
| `do-not-state.md` | yes | Explicit prohibitions |
| `style-notes.md` | yes | Tone guidance |
| `internal-notes.md` | **no** | Operator reference only (pricing, negotiation lines) |

**Only lines beginning with `- ` in `confirmed-facts.md` count as facts.** Headings, blockquotes, and prose are ignored. This matters: the file ships with explanatory placeholder text, and a naive parser would treat that text as approved facts — which silently disables the entire fail-closed design.

If the facts list is empty, the system prompt states plainly that the model has no confirmed product information and must not make any specific claim. The result in practice is a reply like *"I'm not able to confirm specifics in a public comment, but I can share detailed information privately."*

That is the correct behaviour. It is also the reason `internal-notes.md` exists — so that the information a human needs never enters the prompt in the first place.

## Approval flow

```
generate → write pending.json → push card → await click
                                                   │
                        ┌──────────────────────────┼──────────────────────────┐
                        ▼                          ▼                          ▼
                     [Send]                 [Edit & Send]                [Ignore]
                        │                          │                          │
                 drop from pending           read form value            drop from pending
                        │                          │                          │
                 ack the client NOW          drop from pending          ack the client
                        │                          │
                 background thread                 │
                        │                     ack the client NOW
                 Graph API POST                     │
                        │                     background thread
                 update card in place               │
                                              Graph API POST
```

Two details are load-bearing and easy to get wrong:

1. **Acknowledge the callback before doing the work.** Chat platforms expect a response within ~3 seconds. A Graph API call takes 2–4. If you call the API first, the client shows "server did not respond", the human assumes it failed, and clicks again. That produces a duplicate public reply. This is not hypothetical; it happened.

2. **Remove from `pending.json` before the API call.** If the same comment is somehow submitted twice, the second attempt finds no record and does nothing. Belt and braces alongside the async fix above.

## What we would change

- The dedup and depth maps are process memory. A restart clears them. Fine at our volume; would need SQLite at 10×.
- The spool has no locking against concurrent producers. Single producer, so it works, but it is an assumption rather than a guarantee.
- Page registry is a Python dict. Should be a config file.
