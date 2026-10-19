# Security & production hardening

Every item below is an answer to something that actually broke, or something we nearly shipped. Where a claim is measured rather than reasoned, it says so.

---

## 1. Verify the signature, over the raw body

Meta signs the request body with the App Secret and puts the digest in `X-Hub-Signature-256`.

```python
raw = await request.body()                      # RAW bytes, not parsed-then-reserialised
expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, header):
    raise HTTPException(status_code=403)
```

Three things that are each independently wrong if you get them wrong:

| Mistake | Consequence |
|---|---|
| Compute HMAC over `json.dumps(await request.json())` | Whitespace and key order change → digest never matches → every event rejected. You will debug this for hours. |
| Compare with `==` | Timing side channel. Use `hmac.compare_digest`. |
| Log the body on failure | Lets an attacker probe the endpoint and have their payloads written to your logs. Log the fact of the failure, not the content. |

**Fail closed on startup.** If the App Secret file is missing or empty, the process must refuse to start. A service that starts without a secret and "temporarily" accepts unverified traffic is a service that will do so permanently.

```python
if not secret:
    sys.exit("FATAL: Meta App Secret not available. Refusing to start.")
```

### The distinction that matters when reporting security status

> "No signature failures in the logs" means something **only if signature verification is implemented.**

Before verification existed, a log scan for `signature` returned zero hits. That is not evidence of safety — it is evidence the word never appeared. Reporting it as "no signature failures" would have turned an unknown into a clean bill of health.

The rule: **any conclusion of the form "Y did not occur" requires first proving that the mechanism which would record Y is actually running.**

Same trap applies to: "no 401s" when there is no auth, "no errors" when errors are not caught, "disk not growing" when no baseline was taken.

### End-to-end tests that bypass the broken link

We once ran a passing end-to-end test against a webhook integration that had never worked in production.

The test POSTed a signed payload directly at the gateway's internal port. It exercised signature verification, page tagging, eligibility, spool write, LLM call, and card push. All green.

But real traffic reaches the gateway through **nginx**, and the broken piece was Meta's delivery to the public callback URL. By short-circuiting to the internal port, the test skipped exactly the segment that was failing.

**Check:** after an end-to-end test, ask *"did this request travel the same path as real traffic?"* If the nginx access log has no record of your request, you tested something else.

---

## 2. Webhook subscription is configured at two levels

This one cost us weeks.

| Level | Endpoint | Meaning |
|---|---|---|
| App | `GET /{app-id}/subscriptions` | Which fields this app **wants** |
| Page | `GET /{page-id}/subscribed_apps` | Which fields this page **permits** the app to receive |

A page can show `["feed", "leadgen"]` while the app-level subscription lists only `["feed"]`. In that state `feed` events (comments, reactions) arrive normally and `leadgen` events silently do not. There is no error anywhere.

**Symptom:** comments work, lead forms produce nothing.

**Fix:**

```
POST /{app-id}/subscriptions
  object=page
  callback_url=https://your-domain/webhook
  fields=feed,leadgen
  verify_token=<the same token your gateway checks>
  access_token={app-id}|{app-secret}
```

Then re-read `GET /{app-id}/subscriptions` and confirm `fields` is what you expect. Do not trust the page-level view alone.

---

## 3. The self-reply problem

A comment bot that replies to its own replies is the single most common way these systems embarrass themselves in public.

The mechanism: Meta delivers Page-authored comments as ordinary `comment`/`add` events. `author.id` equals your Page ID. Nothing else distinguishes them.

**Structural fix** — before any other eligibility logic:

```python
if author_id in OUR_PAGE_IDS:
    return "self_page"          # never eligible, no counter, no prompt engineering
```

This must be a set lookup, not a heuristic. No model call, no confidence score, no "if the text looks like our style".

We verified this end to end: a reply published through the Graph API came back as a webhook event **4.9 seconds later** with `author.id` equal to the Page, and the gateway logged `reply=no reason=self_page`. The loop is broken at the identity layer.

**Consequence for depth counting:** because self-replies *do* arrive, thread depth can be tracked. If your subscription does not deliver them, depth stays at zero and only the phrase filter is protecting you.

---

## 4. Duplicate suppression has two layers, and they protect different things

| Layer | Key | TTL | Protects against |
|---|---|---|---|
| Gateway | `comment_id:verb` | 1 hour | Meta redelivering the same event |
| Worker | `comment_id` | 6 hours | Human clicking Send twice |
| Approval store | `pending.json` entry | until resolved | Same comment presented twice |

Meta retries webhook delivery. Without layer 1 you will generate the same card five times.

Layer 3 is not redundant with the async callback fix (§5) — it is the backstop for when the callback is slow enough that the human retries anyway.

**Known limit:** all three are process memory or a small JSON file. A restart clears them. Meta's retry window is longer than a restart cycle, so a crash mid-delivery can produce a duplicate. At our volume this has not happened; at higher volume, put the dedup keys in SQLite.

---

## 5. Acknowledge the callback before doing the work

Chat platforms (Feishu, Slack, Teams) use a request-response model for interactive card buttons. The client waits for a response object. If none arrives in roughly 3 seconds, it shows an error.

Graph API calls take 2–4 seconds. So:

```python
def on_button_click(payload):
    record = pending_take(comment_id)      # 1. claim it
    if not record:
        return toast("Already handled")
    threading.Thread(target=do_send, args=(record,), daemon=True).start()   # 3. async
    return toast("Sending...")             # 2. immediate ack
```

**What happens if you skip this:** the human sees "server did not respond", concludes the send failed, and clicks again. Two public replies. We shipped this bug and produced a duplicate reply on a live Page within a day.

Note the ordering: claim first, ack second, work third. Any other order has a window where a double-click doubles the post.

---

## 6. Secrets: files, not environment variables

| Location | Mode | Owner |
|---|---|---|
| `/etc/app/secrets/` | `0700` | root |
| `.../meta-app-secret` | `0600` | root |
| `.../page-token-<id>` | `0640` | `root:<service-group>` |
| `.../llm-api-key` | `0600` | service user |

Why files and not env vars:

- `docker inspect` and `/proc/<pid>/environ` expose the full environment of any process you can signal. File permissions are checked per-read.
- Env vars leak into crash dumps and child processes.
- A file can be rotated without restarting the service (as long as the code does not cache it).

**Never log a secret.** Every script that handles tokens should pipe its output through a redactor before printing. A cheap `sed` for the token prefix (`EAA…`, `sk-…`, `Bearer …`) catches the accidental `print(response)`.

### On credential scope

A **Page** token cannot act as a **User**. If you are publishing as a brand, verify this before your first post:

```
GET /me  with the Page token
→ must return the Page ID, not a personal ID
```

We check this at startup and refuse to send if the identity is wrong. Getting it wrong means a marketing intern's personal profile posts as your company.

### On permission propagation

Adding or removing a permission in the developer console changes the effective scope of **every existing token issued by that app**. It is not scoped to the token you are currently generating.

We watched this happen: five long-lived tokens, untouched on disk, went from 7 to 9 scopes overnight after a console edit.

**Consequence:** after any console change, immediately run `debug_token` against every production token and diff the scopes. A removed `pages_manage_engagement` will silently break publishing on a Sunday.

---

## 7. Network boundaries

The gateway's value as a security boundary comes from what it **cannot** do:

- No outbound HTTP client imported
- No credential for any downstream system on disk
- No database connection

Verify this rather than assuming it:

```bash
grep -rnE 'requests|urllib|httpx|aiohttp|http\.client' gateway/
```

If that returns hits in the webhook path, the boundary no longer exists.

### Loopback is not a security boundary by itself

Hosting a service on `127.0.0.1` protects it from the internet. It does **not** protect it from another local process. On a typical host, loopback also carries the Chrome DevTools port, a database admin socket, and whatever else is running.

If process A must be prevented from reaching local services other than B, an address allowlist is insufficient because it is per-address, not per-port. Options, in order of preference:

1. **Dedicated loopback range** — bind B to `127.9.0.x`, allow only `127.9.0.0/24`. Requires a local route so the source address is preserved.
2. **eBPF cgroup network policy** — if your systemd version supports `IPAddressAllow`/`IPAddressDeny`, this is a kernel-enforced boundary with no iptables rules to maintain.
3. **iptables `--uid-owner`** — precise, but it is firewall state that must be persisted and reviewed.

Whichever you pick, test it from inside the restricted process. "It should be blocked" is not a test.

---

## 8. Log hygiene

Things that must never appear in logs:

- Request bodies of signature failures
- Any token, key, or secret value
- Full customer message text at DEBUG in production (it is PII)

Things that should appear:

- Every decision, with its reason code
- Every outbound API call: endpoint, HTTP status, duration
- Every credential load: which file, success/failure — never the value

**Rotation:** container json-file logging has no default size cap. A chatty service will fill the disk. Set `max-size` and `max-file` on every service from day one — retrofitting after an incident means the evidence you need has already been rotated away.

**Reading logs:** use `--since` rather than `--tail N`. On a public IP, scanner traffic (`.env`, `wp-json`, `.git/config` probes) will push your events out of any fixed tail window within minutes. A `--tail 2000` scan once told us "Meta never delivered anything" when the events were present the whole time.

---

## 9. Reply-length limits must agree across the pipeline

Three separate limits exist, and they must be ordered:

| Layer | Limit | Failure mode |
|---|---|---|
| Prompt instruction | 600 chars | Model ignores it |
| Card input `default_value` | ~500 chars | Platform rejects the card |
| Hard truncation | 500 chars | Content silently cut |

If the truncation threshold is *below* the prompt instruction, the tail is cut off **in the draft the human is about to send**. They click Send, and the customer receives a sentence that stops mid-word.

**Fix:** tell the model a shorter target than the truncation limit (we use "150–350 characters" with a hard cap at 400, truncating at 500). Then the truncation path should never fire — and if it does, log loudly and label the draft *"too long, do not send directly"* rather than quietly clipping.

Side effect worth knowing: constraining length also cut generation time roughly in half, because the model stopped producing bulleted lists.

---

## 10. Signals that lie

Some monitoring output looks healthy while the service is doing nothing.

| Signal | Looks like | Actually means |
|---|---|---|
| `systemctl is-active` = active | working | process exists |
| Startup banner "knowledge base: 0 facts" | KB not loaded | banner read the fallback root, real KBs load per-page later |
| No `ERR` lines in journal | healthy | errors may be swallowed by `try/except` |
| `pgrep -c <name>` returns 1 | one process running | matched the grep command itself |

**Runaway retry loops are the worst case.** A `try/except` inside a polling loop that fails every iteration produces a service that is `active`, logs the same exception every 2 seconds forever, and consumes nothing. The process is alive; the function is dead.

Mitigation: count consecutive failures and exit after a threshold, letting the supervisor restart. Fifteen is a reasonable number — high enough to survive a transient outage, low enough that a permanent bug does not go unnoticed overnight.

For "is it actually working", measure throughput, not liveness: offset advancing, queue depth, cards per hour. A service that processes zero events and a service that is down look identical from `systemctl status`.

---

## 11. Iterating over a file while tracking position

Python's `for line in file` uses an internal read-ahead buffer and **disables `tell()`**. Calling it inside the loop raises `OSError: telling position disabled by next() call`.

Wrapped in a `try/except`, this produces the runaway-retry pattern from §10: the loop throws on every iteration, the offset never advances, and the service appears healthy.

```python
with open(path) as f:
    f.seek(offset)
    while True:
        line = f.readline()
        if not line:
            break
        if not line.endswith("\n"):     # half-written line
            break
        offset = f.tell()               # legal under readline()
        handle(line)
```

Also handle log rotation: store the inode alongside the offset, and reset the offset to zero if the inode changes.

---

## 12. Content filtering: closing phrases

Covered in detail in `architecture.md`. The security-relevant summary:

- **Whole-message matching.** Substring matching on "thanks" will swallow "thanks, what's the lead time?".
- **Question detection runs on the raw string**, before normalisation removes the punctuation.
- **Bias toward replying.** A missed question is a lost sale; an unnecessary "you're welcome" is a minor annoyance.

---

## Checklist before going live

- [ ] Signature verification implemented, and a tampered body returns 403
- [ ] Missing header returns 403
- [ ] Service refuses to start without the App Secret
- [ ] App-level *and* page-level subscriptions both include every field you care about
- [ ] Self-page filter active; confirm by publishing a reply and watching it come back as `self_page`
- [ ] Duplicate event produces exactly one card
- [ ] Button click returns an ack in under 1 second, and the Graph call happens in the background
- [ ] `POST_ENABLED=false` until you have reviewed at least ten drafts
- [ ] `GET /me` with the Page token returns the Page ID, not a personal ID
- [ ] Log rotation configured with an explicit size cap
- [ ] Secret scan across the repo and the logs returns zero hits
