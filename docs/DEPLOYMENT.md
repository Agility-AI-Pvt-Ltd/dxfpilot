# Deploying CadPilot to EC2 with GitHub Actions

Pipeline: `.github/workflows/ci-cd.yml`

| Trigger | What runs |
|---|---|
| Pull request | backend tests (file store **and** PostgreSQL), frontend type-check + build, compose validation + image builds |
| Push to `main`, or **Run workflow** | the same checks, then **deploy** |

The deploy job:

1. **Preflight** — the host has `docker compose`, `/opt/dextool/.env` exists and has a strong `POSTGRES_PASSWORD`.
2. **Database backup** — `pg_dump` to `/opt/dextool/backups/<time>-before-<commit>.sql.gz` (10 newest kept),
   because schema migrations run automatically when the API starts.
3. **Sync** — `rsync` of the source. Never synced: `.env` files, `*.pem`, `.git`, build output, backups.
4. **Build and start** — `docker compose up -d --build --remove-orphans`.
5. **Verify** — waits for `/api/health` and the web page through the gateway; on failure it prints
   container status and logs and the job fails.

Runtime on the host: `db` (PostgreSQL 17, loopback only) → `api` (FastAPI) → `web` (Next.js) → `gateway`
(Caddy on port 80, and 443 when a domain is configured).

## One-time setup

### 1. EC2 instance

- Ubuntu 24.04 or Amazon Linux 2023, **t3.medium or larger** (the web image build needs ~2 GB RAM),
  30 GB disk.
- Security group: inbound **22** from your IP (and GitHub Actions — see note below), **80** (and **443**
  for HTTPS) from where users connect. Do **not** open 5432.

> GitHub-hosted runners use changing IP ranges. Either allow SSH from `0.0.0.0/0` with key-only auth
> (the default on EC2), or use a self-hosted runner / AWS SSM instead of SSH.

### 2. Docker on the host

Ubuntu:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # log out and back in
docker compose version
```

Amazon Linux 2023:

```bash
sudo dnf install -y docker rsync && sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
```

### 3. App directory and settings (on the host)

```bash
sudo mkdir -p /opt/dextool && sudo chown $USER /opt/dextool
cd /opt/dextool
curl -fsSLo .env https://raw.githubusercontent.com/<you>/<repo>/main/.env.example   # or copy it over by hand
nano .env
```

Set at least:

```
POSTGRES_PASSWORD=<openssl rand -base64 32>
OPENAI_API_KEY=<your key>
OPENAI_MODEL=openai/gpt-4.1-mini
OPENAI_BASE_URL=https://openrouter.ai/api/v1
SITE_ADDRESS=:80            # or your domain for automatic HTTPS

# optional: LangSmith tracing
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your LangSmith key>
LANGSMITH_PROJECT=cadpilot
```

`POSTGRES_PASSWORD` is applied when the database volume is **first** created. To change it later:
`docker compose exec db psql -U cadpilot -c "ALTER USER cadpilot PASSWORD '...'"`, then update `.env`
and `docker compose up -d`.

### 4. Deploy key

On your machine:

```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-cadpilot" -f cadpilot_deploy
ssh-copy-id -i cadpilot_deploy.pub <user>@<ec2-host>      # or append it to ~/.ssh/authorized_keys on the host
ssh-keyscan -t ed25519 <ec2-host>                         # output = EC2_SSH_KNOWN_HOSTS
```

### 5. GitHub settings

Repository → Settings → Environments → create **production** (optionally with required reviewers so
every deploy waits for approval). Add these **environment secrets**:

| Secret | Value |
|---|---|
| `EC2_HOST` | public DNS name or IP of the instance |
| `EC2_USER` | `ubuntu` or `ec2-user` |
| `EC2_SSH_PRIVATE_KEY` | contents of `cadpilot_deploy` (the private key) |
| `EC2_SSH_KNOWN_HOSTS` | the `ssh-keyscan` output (pins the host key) |

Optional repository **variable** `DEPLOY_DIR` (default `/opt/dextool`).

The same four secret names are used by the older `cadpilot` repository. If both apps share one host,
give this one its own `DEPLOY_DIR` and, since only one app can own port 80, set `HTTP_PORT=8080`
(and `HTTPS_PORT=8443`) in its `.env`, or put both behind one domain-routing proxy.

### 6. First deploy

Push to `main`, or Actions → **CI and EC2 deployment** → **Run workflow**. Then open
`http://<ec2-host>/` (or `https://<your-domain>/`).

## HTTPS

Point a DNS record at the instance, open port 443, set `SITE_ADDRESS=pid.example.com` in `.env` and
redeploy (or `docker compose up -d gateway`). Caddy obtains and renews the certificate itself.

## Operations

```bash
cd /opt/dextool
docker compose ps                         # status
docker compose logs -f api                # API log (LLM fallbacks are logged as warnings)
docker compose exec db psql -U cadpilot   # SQL shell
ls -lh backups/                           # pre-deploy backups
gunzip -c backups/<file>.sql.gz | docker compose exec -T db psql -U cadpilot -d cadpilot   # restore into an empty DB
```

Rollback: re-run the workflow for an earlier commit (Actions → the older run → **Re-run jobs**),
restoring the matching backup first if that commit predates a schema migration.
