# Deployment

Target: a single Linux host, Docker for the gateway, systemd for the worker.

Two processes, one shared directory. That is the whole topology. It is deliberately boring — the interesting parts are elsewhere.

---

## Layout

```
/opt/leadpulse/
├── gateway/
│   ├── app.py
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── requirements.txt
├── worker/
│   ├── run.py
│   └── worker/{config,spool,llm,chat,graph,kbase,worker}.py
├── kb/
│   └── <slug>/{confirmed-facts,do-not-state,style-notes,internal-notes}.md
└── venv/

/etc/leadpulse/                0700 root:root
├── meta-app-secret            0600 root:root
├── page-token-<page-id>       0640 root:<service-group>
├── chat-app-id                0600 root:root
├── chat-app-secret            0600 root:root
└── operator-allowlist         0600 root:root

/var/lib/leadpulse/
├── spool/events.jsonl         (written by the gateway container)
├── spool-offset.json          (written by the worker)
└── pending.json               (written by the worker)
```

---

## Gateway: Docker

```yaml
services:
  gateway:
    build: .
    restart: unless-stopped
    read_only: true
    volumes:
      - /var/lib/leadpulse/spool:/spool
      - /etc/leadpulse/meta-app-secret:/run/secrets/meta_app_secret:ro
    tmpfs:
      - /tmp
    environment:
      META_VERIFY_TOKEN: ${META_VERIFY_TOKEN}
      META_APP_SECRET_FILE: /run/secrets/meta_app_secret
      EVENT_SPOOL_FILE: /spool/events.jsonl
    ports:
      - "127.0.0.1:9000:9000"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Notes on each decision:

- `read_only: true` — the only writable path is the spool mount. If the process is compromised it cannot drop a binary on the container filesystem.
- `127.0.0.1:9000:9000` — the gateway is published on loopback and reached through a reverse proxy that terminates TLS. Never expose it directly; the proxy is where rate limiting and access logs live.
- `logging.options` — without an explicit cap, json-file logs grow until the disk fills. Set this before the first incident, not after.
- The secret is mounted read-only at `/run/secrets/`. If the file is absent, the service exits. That is intentional.

### Reverse proxy

```nginx
location /webhook {
    proxy_pass http://127.0.0.1:9000/webhook;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 30s;
}
```

Meta's `GET /webhook?hub.mode=subscribe&...` verification must return the `hub.challenge` value as **plain text** with status 200. Returning JSON here is the most common reason verification fails.

---

## Worker: systemd

```ini
[Unit]
Description=Lead Pulse Guard worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=leadpulse
Group=leadpulse
SupplementaryGroups=leadpulse-secrets
WorkingDirectory=/opt/leadpulse/worker
ExecStart=/opt/leadpulse/venv/bin/python /opt/leadpulse/worker/run.py

Environment=AIW_POST_ENABLED=true
Environment=AIW_MODEL=your-model-name

StateDirectory=leadpulse
UMask=0077

NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
MemoryMax=256M

Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

`StateDirectory=leadpulse` creates `/var/lib/leadpulse` owned by the service user. Under `ProtectSystem=strict` this is the **only** writable location — which is why the spool lives there and not in `/opt`.

`SupplementaryGroups=leadpulse-secrets` is how the worker reads the Page token without running as root:

```bash
groupadd -r leadpulse-secrets
usermod -aG leadpulse-secrets leadpulse
chgrp leadpulse-secrets /etc/leadpulse/page-token-*
chmod 0640 /etc/leadpulse/page-token-*
```

That means **no root anywhere in the runtime path.** The trade-off is that any directory-setup script must not hard-code `0700` on `/etc/leadpulse`, or you lock the service out of its own credentials. If a setup script creates the directory, it should leave an existing directory's mode alone.

---

## First run

1. **Verify the secret loads.** `docker compose logs gateway | grep -i secret` — should report a successful load, never the value.

2. **Verify the signature path.** Send a payload with a deliberately wrong signature:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://your-domain/webhook \
     -H 'X-Hub-Signature-256: sha256=deadbeef' \
     -H 'Content-Type: application/json' \
     -d '{"object":"page"}'
   ```
   Expect `403`. If you get `200`, verification is not wired up.

3. **Verify through the proxy, not around it.** Repeat step 2 against the public hostname and then confirm the request appears in the nginx access log. A test that skips the proxy does not test the path real traffic takes.

4. **Subscribe at both levels.** See `security.md` §2. After subscribing, re-read the app-level subscription and confirm the field list.

5. **Keep `AIW_POST_ENABLED=false`.** Review at least ten drafts. Then flip it and send one for real, and confirm by fetching the comment back from the Graph API.

---

## Operating it

| Question | Command |
|---|---|
| Is the worker consuming? | compare `spool-offset.json` offset to the spool file size |
| How many cards are pending? | `jq 'length' /var/lib/leadpulse/pending.json` |
| Did the gateway reject anything? | `docker logs gateway --since 24h \| grep -c 403` |
| Is the LLM degrading? | count `fell_back=True` in the worker journal |
| Disk filling? | `docker system df`, plus the size cap on the log driver |

**A worker that processes zero events and a worker that is down look identical from `systemctl status`.** Measure the offset, not the process.

---

## Upgrades

The gateway and the worker version independently.

- Gateway: `docker compose up -d --build`. **Export the container logs first** — rebuilding destroys them, and json-file logs are not preserved across container replacement.
- Worker: `systemctl restart leadpulse-worker`.

The spool format is versioned (`schema` field). If you change it, add a `v2` value rather than mutating `v1`, so an old worker does not silently mis-parse new events.

---

## Backup

Three things matter, in order:

1. `/etc/leadpulse/` — page tokens and app secret. Losing these means re-running the token exchange.
2. `/opt/leadpulse/kb/` — the knowledge bases. These are the actual product.
3. `/var/lib/leadpulse/pending.json` — outstanding approvals. Losing it means cards in chat become unclickable.

The spool itself does not need backing up; it is transient by design.
