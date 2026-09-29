# Deploying Hookline

Hookline runs on one Linux VM with Docker Compose, using
[docker-compose.prod.yml](../docker-compose.prod.yml): the API (2 uvicorn processes), a
worker, beat, Postgres and Redis, with [Caddy](https://caddyserver.com/) in front for HTTPS.
Caddy gets a Let's Encrypt certificate for your hostname and renews it by itself. Only
Caddy is reachable from outside; Postgres and Redis have no host ports.

These steps assume Ubuntu 24.04 and a user with `sudo`. Any VM with 2 GB of RAM will do.

## 1. Provision the VM

Install Docker from Docker's own repository (Ubuntu's `docker.io` package lacks the Compose
plugin):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out and back in for this to apply
docker compose version
```

Allow only SSH, HTTP and HTTPS in:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp   # HTTP/3
sudo ufw enable
```

Ports that Docker publishes bypass ufw, so the compose file is what keeps Postgres and Redis
private: it publishes only Caddy's 80 and 443. If your cloud provider has its own firewall
(security group), open the same four ports there too. Port 80 must stay open: Let's Encrypt
checks it before issuing a certificate, and Caddy redirects it to HTTPS.

## 2. Clone

```bash
git clone https://github.com/DevOm-AI/Hookline.git ~/hookline
cd ~/hookline
```

The [deploy workflow](#automatic-deploys) expects the checkout at `~/hookline`.

## 3. Create .env

```bash
cp .env.production.example .env
chmod 600 .env
openssl rand -hex 32   # the Postgres password
```

In `.env`, set:

- `DOMAIN`: a hostname that resolves to the VM. With no domain of your own, use the VM's
  public IP with dashes on [sslip.io](https://sslip.io): `203.0.113.7` becomes
  `203-0-113-7.sslip.io`.
- `POSTGRES_PASSWORD`. `DATABASE_URL` picks it up from there.
- `API_KEY_HASH`: see the next step.

Leave `DEBUG=false` and `ALLOWED_INTERNAL_HOSTS` empty. The production compose file always
sets `ENVIRONMENT=production`, whatever `.env` says, and in production Hookline refuses to
start if `DEBUG` is true, `API_KEY_HASH` is missing or is the local dev key's, or
`ALLOWED_INTERNAL_HOSTS` lists anything.

## 4. Generate the API key

Hookline stores only the key's SHA-256 hash, so generate the pair and keep the key
yourself:

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm --no-deps api python -m app.core.security
```

Put the printed `API_KEY_HASH=...` line in `.env`, and the key (`hk_...`) in your password
manager: it is shown only this once. To rotate it later, generate a new pair, replace the
hash in `.env` and run `docker compose -f docker-compose.prod.yml up -d`.

## 5. Start

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The `migrate` service applies the database migrations first; the API, worker and beat start
once it has finished. Then check it:

```bash
docker compose -f docker-compose.prod.yml ps
curl https://$DOMAIN/health            # {"status":"ok",...}
```

The first request can take a few seconds while Caddy gets the certificate. If it fails, see
`docker compose -f docker-compose.prod.yml logs caddy`: usually `DOMAIN` doesn't resolve to
the VM or port 80 is blocked.

The dashboard is at `https://$DOMAIN/dashboard` and the API docs at `https://$DOMAIN/docs`.
Everything but `/health` needs the API key.

## Updating

```bash
cd ~/hookline
git pull
docker compose -f docker-compose.prod.yml up -d --build
docker image prune -f
```

Compose rebuilds the image and recreates only the containers that changed; `migrate` runs
first again. Workers get SIGTERM and up to 60 seconds to finish their sends (see
[Graceful shutdown](../README.md#graceful-shutdown)), and any delivery cut short is sent
again once its lock expires, so updating loses nothing. The API is unavailable for the few
seconds its container restarts.

If an update fails, `docker compose -f docker-compose.prod.yml ps` and `logs` show which
service. To go back to the previous commit (`git log` lists them):

```bash
prev=<the commit to go back to>
# Its newest migration: the revision id in the last file name (files sort by date).
rev=$(git ls-tree --name-only "$prev" alembic/versions/ | grep '\.py$' | sort | tail -1 \
  | cut -d- -f2 | cut -d_ -f1)

# 1. Undo the failed update's migrations first, while the image still has their files.
#    --no-deps keeps `run` from starting the migrate service, which would upgrade again.
docker compose -f docker-compose.prod.yml run --rm --no-deps api alembic downgrade "$rev"

# 2. Then go back to the previous code (.env is untracked, so it stays) and rebuild.
git reset --hard "$prev"
docker compose -f docker-compose.prod.yml up -d --build
```

Downgrading to a revision the database is already at does nothing, so step 1 is safe even
if the failed update added no migration or its migration never applied.

### Automatic deploys

[.github/workflows/deploy.yml](../.github/workflows/deploy.yml) does the same over SSH each
time CI passes on `main`. It deploys the commit CI tested, one deploy at a time, in order.
Set these repository secrets (Settings → Secrets and variables → Actions). With none set,
deploys are skipped; with only some set, the workflow fails and names the missing ones.

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | The VM's IP or hostname |
| `DEPLOY_USER` | The user that owns `~/hookline` and is in the `docker` group |
| `DEPLOY_SSH_KEY` | A private key whose public half is in that user's `~/.ssh/authorized_keys` |
| `DEPLOY_KNOWN_HOSTS` | The VM's SSH host keys, read on the VM itself (below) |

Use a key made only for this, e.g. `ssh-keygen -t ed25519 -f hookline-deploy -N ""`.

The workflow connects only to a server holding one of the host keys in `DEPLOY_KNOWN_HOSTS`,
so a machine impersonating the VM can't receive the deploy. Read the keys from the VM's own
files, not with `ssh-keyscan` from elsewhere, which would trust whoever answers. On the VM,
with `DEPLOY_HOST`'s exact value as the name:

```bash
for f in /etc/ssh/ssh_host_*_key.pub; do echo "203.0.113.7 $(cut -d' ' -f1,2 "$f")"; done
```

Paste the output as the secret. If the VM's host keys ever change (it is rebuilt, say),
deploys fail until you update it.

## Backups

Postgres holds everything: events, deliveries and the attempt log. Redis holds nothing
that needs a backup. Dump it nightly with cron:

```bash
sudo install -d -o "$USER" -m 700 /var/backups/hookline

cat > ~/hookline-backup.sh <<'EOF'
#!/usr/bin/env bash
# Nightly pg_dump of Hookline's database; keeps 14 days.
set -euo pipefail
cd ~/hookline
out=/var/backups/hookline/hookline-$(date +%F).dump
docker compose -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$out.tmp"
mv "$out.tmp" "$out"
find /var/backups/hookline -name 'hookline-*.dump' -mtime +14 -delete
EOF
chmod +x ~/hookline-backup.sh
~/hookline-backup.sh && ls -lh /var/backups/hookline   # try it once

# Every night at 03:00
(crontab -l 2>/dev/null; echo "0 3 * * * $HOME/hookline-backup.sh >> $HOME/hookline-backup.log 2>&1") | crontab -
```

The dump is written to a `.tmp` file first, so a failed run never leaves a truncated dump
under a real name. These backups are on the same disk as the database: copy them off the
VM too (e.g. `rclone` to object storage), or losing the VM loses both.

To restore a dump into the running stack, pick one from `ls /var/backups/hookline`:

```bash
dump=/var/backups/hookline/hookline-YYYY-MM-DD.dump   # replace with the one to restore
docker compose -f docker-compose.prod.yml stop api worker beat
docker compose -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --single-transaction --exit-on-error' \
  < "$dump" \
  && docker compose -f docker-compose.prod.yml up -d
```

The restore runs as one transaction and stops at the first error, so it either replaces the
whole database or changes nothing, and Hookline is started again only if it succeeded. If it
fails, the database is as it was before: fix the cause and run it again, or start Hookline
on the old data with `docker compose -f docker-compose.prod.yml up -d`.

Deliveries that were `in_progress` when the dump was taken are picked up again by the
sweeper once their lock has expired. Events accepted after the dump was taken are not in it.
