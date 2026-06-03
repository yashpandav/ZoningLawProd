# Deployment Master Plan
## Toronto Zoning AI — Oracle Cloud + Cloudflare + Qdrant Cloud

---

## Deployment Status Tracker

> Update the checkboxes here as you complete each step. Every phase links to its detail section below.

| # | Phase | Status | Notes |
|---|-------|--------|-------|
| — | **[Code preparation](#code-preparation-completed)** | ✅ Done | Dockerfile, requirements, LangSmith, CORS, env templates |
| 0 | **[Pre-flight checks](#phase-0--pre-flight-checks-on-your-local-machine)** | ⬜ Todo | Run before touching Oracle — takes 15 min |
| 1 | **[Oracle Cloud VM setup](#phase-1--oracle-cloud-vm-setup)** | ⬜ Todo | Create ARM VM, install Docker |
| 2 | **[Cloudflare setup](#phase-2--cloudflare-setup)** | ⬜ Todo | Tunnel only — no Access Gate, direct public access |
| 3 | **[Oracle Vault secrets](#phase-3--secrets-management-with-oracle-vault)** | ⬜ Todo | API keys stored securely, never on disk |
| 4 | **[Qdrant Cloud + data migration](#phase-4--qdrant-cloud-setup-and-data-migration)** | ⬜ Todo | Upload ~31K vector snapshots |
| 5 | **[PostGIS data migration](#phase-5--postgis-data-migration-to-oracle-vm)** | ⬜ Todo | Dump → SCP → restore on VM |
| 6 | **[LangSmith integration](#phase-6--langsmith-integration)** | ✅ Done | wrap_openai + @traceable in code |
| 7 | **[Build + push Docker image](#phase-7--build-and-push-the-backend-docker-image)** | ⬜ Todo | ARM64 build → Docker Hub → pull on VM |
| 8 | **[Vercel frontend deploy](#phase-8--frontend-deployment-on-vercel)** | ⬜ Todo | Push to GitHub → Vercel auto-deploys |
| 9 | **[Final launch](#phase-9--final-launch-on-oracle-vm)** | ⬜ Todo | Load secrets → docker compose up → verify |
| 10 | **[Auto-restart](#phase-10--set-up-auto-restart-on-vm-reboot)** | ⬜ Todo | systemd service so VM reboots recover automatically |

**Recommended execution order:** 0 → 1 → 2 → 3 → 4 → 5 (these can overlap; 4 and 5 are independent) → 7 → 8 → 9 → 10

**Estimated time remaining:** ~3 to 4 hours

---

## What to Do Right Now (Next 3 Steps)

You are here: code is ready, nothing is deployed yet. Do these three things in parallel — they don't depend on each other:

**Step A — Run Phase 0 pre-flight checks (15 min, on your machine)**
```bash
# Confirm local app is healthy before migrating anything
curl http://localhost:8000/api/health

# Note the size of your PostGIS data
psql -h localhost -p 5433 -U user -d toronto_zoning \
  -c "SELECT pg_size_pretty(pg_database_size('toronto_zoning'));"

# Note vector counts (you'll verify these match after Qdrant Cloud restore)
curl http://localhost:6333/collections/toronto_zoning_rules | python3 -m json.tool | grep points_count
curl http://localhost:6333/collections/toronto_zoning_exceptions | python3 -m json.tool | grep points_count
```

**Step B — Sign up for the three free services (10 min, in browser)**
| Service | URL | What you get |
|---------|-----|-------------|
| Oracle Cloud | `cloud.oracle.com` | ARM VM (Phase 1) |
| Qdrant Cloud | `cloud.qdrant.io` | Vector DB free tier (Phase 4) |
| LangSmith | `smith.langchain.com` | Trace monitoring (Phase 6.1) |

**Step C — Commit the code changes (5 min)**
```bash
git add backend/requirements.txt backend/.env.example frontend/.env.local.example docker-compose.prod.yml
git commit -m "chore: deployment prep — Dockerfile fix, prod compose, env templates, LangSmith"
git push
```
This enables the Vercel deploy in Phase 8 — the sooner it's pushed, the sooner Vercel picks it up.

---

## Code Preparation (Completed)

The following code changes were made to make the codebase deployment-ready. No further code changes are needed.

| File | What changed | Why |
|------|-------------|-----|
| `backend/Dockerfile` | Fixed `COPY ../requirements.txt` → `COPY requirements.txt`; added `curl` | Broken path caused Docker build failure; `curl` needed for healthcheck |
| `backend/requirements.txt` | Added `python-dotenv`, `PyYAML`, `langsmith` | `query.py` imports both at startup — missing packages crash the container immediately |
| `backend/query.py` | `wrap_openai()` on both OpenAI clients when `LANGCHAIN_TRACING_V2=true` | Auto-traces all OpenAI calls (both streaming and quick mode) in LangSmith |
| `backend/quick_answer.py` | `@traceable(name="quick_answer", tags=["quick_mode"])` | Adds per-call trace context for quick mode in LangSmith UI |
| `backend/app.py` | CORS `allow_origins` reads from `ALLOWED_ORIGINS` env var (default `*`) | Lock down to `https://app.yashpandav.dev` in prod without code changes |
| `docker-compose.prod.yml` | New file — production stack (no Qdrant, backend on `127.0.0.1:8000` only) | Matches Oracle VM architecture; Cloudflare Tunnel connects outbound |
| `backend/.env.example` | New file — all env var templates with comments | Reference for Oracle Vault secret names and `load-secrets.sh` |
| `frontend/.env.local.example` | New file — frontend env template | Documents `NEXT_PUBLIC_API_BASE` for local dev vs Vercel |
| `.gitignore` | Removed duplicate `requirements.txt` entries; added `backend.env`; allowed `*.example` | `backend/requirements.txt` was accidentally excluded from git tracking |

---

## Architecture Overview (What You're Building)

```
Browser
        │
        ▼
┌───────────────────────┐
│  Cloudflare Edge      │  yashpandav.dev
│  ├─ app.yashpandav.dev│──► Vercel (Next.js, always on, free)
│  └─ api.yashpandav.dev│──► Cloudflare Tunnel ──► Oracle VM :8000
└───────────────────────┘
                                    │
                          ┌─────────▼──────────┐
                          │   Oracle Cloud VM   │
                          │   (ARM, 4 OCPU      │
                          │    24GB RAM, free)  │
                          │                     │
                          │  docker-compose.yml │
                          │  ├── backend :8000  │──► Qdrant Cloud
                          │  ├── postgis :5432  │    (vector DB)
                          │  ├── redis   :6379  │
                          │  └── cloudflared    │
                          └─────────────────────┘
                                    │
                          ┌─────────▼──────────┐
                          │   Logging           │
                          │  ├── OpenAI Dashboard│
                          │  └── LangSmith      │
                          └─────────────────────┘
```

**Key decisions baked in:**
- Frontend on Vercel — zero server cost, deploys in 2 min, handles Next.js perfectly
- `cloudflared` runs as a Docker container inside the VM — no open ports on Oracle firewall needed
- PostGIS stays on the VM (data is large, latency to DB must be zero)
- Qdrant moves to Qdrant Cloud free tier (saves ~1.5GB RAM on VM, simpler backups)
- Redis stays on VM (tiny, session data only)

---

## Phase 0 — Pre-Flight Checks on Your Local Machine
*Do this before touching Oracle. Takes 15 minutes. Prevents surprises later.*

**Check 1: PostGIS database size**
```bash
psql -h localhost -p 5433 -U user -d toronto_zoning \
  -c "SELECT pg_size_pretty(pg_database_size('toronto_zoning'));"
```
Note the number. If it is over 5GB the dump/restore will take longer — plan accordingly.

**Check 2: Count your Qdrant vectors**
```bash
curl http://localhost:6333/collections/toronto_zoning_rules | python3 -m json.tool
curl http://localhost:6333/collections/toronto_zoning_exceptions | python3 -m json.tool
```
Note the `points_count` for both collections. You need these numbers to verify the restore worked.

**Check 3: Confirm your app runs end-to-end locally right now**
```bash
curl http://localhost:8000/api/health
```
Make sure everything is green before you start migrating. Never migrate a broken system.

**Check 4: Note all your current env vars**
Open `backend/.env` and list every key. You will re-enter these into Oracle Vault in Phase 3.

---

## Phase 1 — Oracle Cloud VM Setup
*Takes about 30 minutes including Oracle signup.*

### 1.1 Create the VM

Sign up at `cloud.oracle.com`. Use a credit card (identity verification only — you will not be charged for Always Free resources).

Create a **Compute Instance** with these exact settings:

| Setting | Value |
|---------|-------|
| Image | Canonical Ubuntu 22.04 |
| Shape | VM.Standard.A1.Flex (ARM) |
| OCPUs | 4 |
| RAM | 24 GB |
| Boot volume | 100 GB (free up to 200GB total) |
| Region | Pick the one geographically closest to Toronto (us-ashburn-1 or ca-toronto-1 if available) |

Download the SSH key pair Oracle generates. Save it as `~/.ssh/oracle_zoning.pem` on your machine.

### 1.2 Configure Oracle Firewall (Security List)

By default Oracle blocks all inbound traffic. You need to open **only one port** because Cloudflare Tunnel handles routing — you don't expose 8000 or 3000 publicly.

Go to: Networking → Virtual Cloud Networks → your VCN → Security Lists → Default Security List

Add one **Ingress Rule**:
- Source: `0.0.0.0/0`
- Protocol: TCP
- Destination Port: `22` (SSH — already exists, leave it)

That's it. You do NOT open 8000, 3000, 5432, or 6333. Cloudflare Tunnel connects outbound from inside the VM — no inbound port needed. This is a major security win.

### 1.3 First SSH Into the VM

```bash
chmod 400 ~/.ssh/oracle_zoning.pem
ssh -i ~/.ssh/oracle_zoning.pem ubuntu@YOUR_VM_PUBLIC_IP
```

### 1.4 Install Docker and Docker Compose on the VM

```bash
# Update system
sudo apt-get update && sudo apt-get upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu

# Install Docker Compose plugin
sudo apt-get install -y docker-compose-plugin

# Log out and back in so group change takes effect
exit
```

SSH back in, then verify:
```bash
docker --version
docker compose version
```

### 1.5 Install cloudflared on the VM

```bash
# Add Cloudflare's APT repo
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared jammy main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list

sudo apt-get update && sudo apt-get install -y cloudflared
```

---

## Phase 2 — Cloudflare Setup
*Do this before deploying anything — you need the tunnel credentials for docker-compose.*

### 2.1 Add Your Domain to Cloudflare

1. Go to `dash.cloudflare.com` → Add site → enter `yashpandav.dev`
2. Cloudflare scans your DNS records
3. Change your domain nameservers at your registrar to the two Cloudflare nameservers it gives you
4. Wait 5–30 minutes for propagation
5. Confirm: your domain dashboard in Cloudflare shows "Active"

### 2.2 Create the Tunnel

On your Oracle VM:
```bash
cloudflared tunnel login
# Prints a URL — open it in your browser and authorize with your Cloudflare account
# A cert.pem is saved to ~/.cloudflared/

cloudflared tunnel create zoning-prod
# Save the tunnel ID it prints — looks like: a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### 2.3 Create the Tunnel Config File

```bash
mkdir -p ~/.cloudflared
nano ~/.cloudflared/config.yml
```

Paste this — replace `YOUR_TUNNEL_ID` with the ID from above:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: /home/ubuntu/.cloudflared/YOUR_TUNNEL_ID.json

ingress:
  - hostname: api.yashpandav.dev
    service: http://localhost:8000
  - service: http_status:404
```

### 2.4 Create DNS Records

```bash
cloudflared tunnel route dns zoning-prod api.yashpandav.dev
```

This creates a CNAME in Cloudflare DNS: `api.yashpandav.dev → your-tunnel-id.cfargotunnel.com`

For the frontend (`app.yashpandav.dev`), you'll add the DNS record after deploying to Vercel in Phase 8.

> **No Cloudflare Access needed.** The tunnel exposes the API directly — anyone with the URL can reach it. If you want to restrict access later you can add an nginx basic-auth layer on the VM or use Cloudflare's free IP allowlist rules (no card required), but for now the app is publicly reachable at `https://api.yashpandav.dev`.

---

## Phase 3 — Secrets Management with Oracle Vault
*Your API keys never touch a file on disk.*

### 3.1 Create the Vault

In Oracle Cloud Console:
- Identity & Security → Vault → Create Vault
- Name: `zoning-secrets`
- Type: Virtual Private Vault (free)

Create a Master Encryption Key inside it:
- Name: `zoning-master-key`
- Protection Mode: Software (free)
- Algorithm: AES, 256-bit

### 3.2 Store Each Secret

For each of your env vars, create a Secret in the vault:

| Secret Name | Value |
|-------------|-------|
| `OPENAI_API_KEY` | `sk-...` |
| `VOYAGE_API_KEY` | `pa-...` |
| `QDRANT_API_KEY` | from Qdrant Cloud (Phase 4) |
| `QDRANT_URL` | from Qdrant Cloud (Phase 4) |
| `DB_PASSWORD` | a strong password you set |
| `LANGSMITH_API_KEY` | from LangSmith (Phase 6) |

### 3.3 Create a Startup Script That Pulls Secrets

On the Oracle VM, create `/home/ubuntu/load-secrets.sh`:

```bash
#!/bin/bash
# Pulls secrets from Oracle Vault and writes backend/.env
# Run before docker compose up

VAULT_OCID="ocid1.vault.oc1.xxx..."  # your vault OCID

get_secret() {
  local name=$1
  oci secrets secret-bundle get \
    --secret-id $(oci vault secret list --vault-id $VAULT_OCID \
      --query "data[?\"secret-name\"=='$name'].id | [0]" --raw-output) \
    --query "data.\"secret-bundle-content\".content" --raw-output \
    | base64 -d
}

cat > /home/ubuntu/zoning/backend.env << EOF
OPENAI_API_KEY=$(get_secret OPENAI_API_KEY)
VOYAGE_API_KEY=$(get_secret VOYAGE_API_KEY)
QDRANT_URL=$(get_secret QDRANT_URL)
QDRANT_API_KEY=$(get_secret QDRANT_API_KEY)
DB_URL=postgresql://zoning_user:$(get_secret DB_PASSWORD)@postgis:5432/toronto_zoning
CHAT_MODEL=gpt-4.1
QUICK_ANSWER_MODEL=gpt-4.1-mini
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=zoning-demo
LANGSMITH_API_KEY=$(get_secret LANGSMITH_API_KEY)
ENABLE_PACKGEN=false
ENABLE_SOLVER=false
EOF

chmod 600 /home/ubuntu/zoning/backend.env
echo "Secrets loaded."
```

```bash
chmod +x /home/ubuntu/load-secrets.sh
```

You run this once before starting Docker. The `.env` file exists in memory for the session, never committed anywhere.

---

## Phase 4 — Qdrant Cloud Setup and Data Migration
*The vector data moves from your local machine to Qdrant Cloud.*

### 4.1 Create Qdrant Cloud Cluster

1. Go to `cloud.qdrant.io` → Sign up free
2. Create Cluster:
   - Name: `zoning-prod`
   - Plan: Free (1GB storage, 1 node — enough for ~31K vectors at 1024 dim)
   - Region: AWS us-east-1 (closest to Oracle us-ashburn-1)
3. Save the **Cluster URL** and **API Key** — these go into Oracle Vault as `QDRANT_URL` and `QDRANT_API_KEY`

### 4.2 Create Qdrant Snapshots on Your Local Machine

```bash
# Snapshot collection 1
curl -X POST http://localhost:6333/collections/toronto_zoning_rules/snapshots
# Returns: {"result":{"name":"toronto_zoning_rules-TIMESTAMP.snapshot",...}}

# Snapshot collection 2
curl -X POST http://localhost:6333/collections/toronto_zoning_exceptions/snapshots
# Returns: {"result":{"name":"toronto_zoning_exceptions-TIMESTAMP.snapshot",...}}
```

Find the snapshot files:
```bash
ls qdrant_storage/snapshots/
```

### 4.3 Upload Snapshots to Qdrant Cloud

```bash
# Replace YOUR_CLUSTER_URL and YOUR_API_KEY
# Upload and restore collection 1
curl -X POST \
  "https://YOUR_CLUSTER_URL/collections/toronto_zoning_rules/snapshots/upload?priority=snapshot" \
  -H "api-key: YOUR_API_KEY" \
  -H "Content-Type: multipart/form-data" \
  -F "snapshot=@qdrant_storage/snapshots/toronto_zoning_rules-TIMESTAMP.snapshot"

# Upload and restore collection 2
curl -X POST \
  "https://YOUR_CLUSTER_URL/collections/toronto_zoning_exceptions/snapshots/upload?priority=snapshot" \
  -H "api-key: YOUR_API_KEY" \
  -H "Content-Type: multipart/form-data" \
  -F "snapshot=@qdrant_storage/snapshots/toronto_zoning_exceptions-TIMESTAMP.snapshot"
```

### 4.4 Verify the Restore

```bash
# Both should return the same points_count as your local instance
curl -H "api-key: YOUR_API_KEY" \
  "https://YOUR_CLUSTER_URL/collections/toronto_zoning_rules" | python3 -m json.tool

curl -H "api-key: YOUR_API_KEY" \
  "https://YOUR_CLUSTER_URL/collections/toronto_zoning_exceptions" | python3 -m json.tool
```

---

## Phase 5 — PostGIS Data Migration to Oracle VM
*The GIS data moves from your local machine to the VM.*

### 5.1 Dump PostGIS on Your Local Machine

```bash
# This creates a compressed dump file
docker exec toronto_postgis pg_dump \
  -U user \
  -Fc \
  toronto_zoning \
  > toronto_zoning_backup.dump

# Check the file size
ls -lh toronto_zoning_backup.dump
```

### 5.2 Transfer the Dump to Oracle VM

```bash
scp -i ~/.ssh/oracle_zoning.pem \
  toronto_zoning_backup.dump \
  ubuntu@YOUR_VM_PUBLIC_IP:/home/ubuntu/toronto_zoning_backup.dump
```

This will take a few minutes depending on your internet speed and the file size.

### 5.3 Set Up the Project Directory on Oracle VM

SSH into the VM:
```bash
ssh -i ~/.ssh/oracle_zoning.pem ubuntu@YOUR_VM_PUBLIC_IP
mkdir -p /home/ubuntu/zoning
cd /home/ubuntu/zoning
```

### 5.4 Create the Production docker-compose.yml on the VM

```yaml
version: "3.9"

services:

  postgis:
    image: postgis/postgis:16-3.4
    container_name: zoning_postgis
    restart: always
    environment:
      POSTGRES_USER: zoning_user
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
      POSTGRES_DB: toronto_zoning
    volumes:
      - postgis_data:/var/lib/postgresql/data
    networks:
      - zoning_internal
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U zoning_user -d toronto_zoning"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: zoning_redis
    restart: always
    command: redis-server --save 60 1 --loglevel warning --requirepass "${REDIS_PASSWORD}"
    volumes:
      - redis_data:/data
    networks:
      - zoning_internal
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    image: zoning-backend:latest
    container_name: zoning_backend
    restart: always
    env_file: /home/ubuntu/zoning/backend.env
    ports:
      - "127.0.0.1:8000:8000"   # Only localhost — Cloudflare Tunnel connects here
    networks:
      - zoning_internal
    depends_on:
      postgis:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s   # SPLADE loads slowly on first start

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: zoning_tunnel
    restart: always
    command: tunnel --config /etc/cloudflared/config.yml run
    volumes:
      - /home/ubuntu/.cloudflared:/etc/cloudflared
    networks:
      - zoning_internal
    depends_on:
      backend:
        condition: service_healthy

volumes:
  postgis_data:
  redis_data:

networks:
  zoning_internal:
    driver: bridge
```

**Two important notes in this compose file:**
- Backend binds to `127.0.0.1:8000` not `0.0.0.0:8000` — it is invisible to the outside world; only Cloudflare Tunnel (same network) can reach it
- `cloudflared` waits for the backend health check before starting the tunnel — the architect can't hit a cold endpoint

### 5.5 Restore the Dump Into the PostGIS Container

```bash
# Start just PostGIS first
docker compose up -d postgis

# Wait for it to be healthy
docker compose ps

# Restore the dump
docker exec -i zoning_postgis pg_restore \
  -U zoning_user \
  -d toronto_zoning \
  --no-owner \
  --role=zoning_user \
  -Fc \
  < /home/ubuntu/toronto_zoning_backup.dump

# Verify the tables are there
docker exec zoning_postgis psql \
  -U zoning_user \
  -d toronto_zoning \
  -c "\dt"
```

You should see `zoning_area`, `height_overlay`, `lot_coverage_overlay`, and the other 5 spatial tables.

---

## Phase 6 — LangSmith Integration ✅ DONE

> **Code changes already made.** The only remaining action is creating the LangSmith account and adding the API key to Oracle Vault in Phase 3.

### 6.1 Create LangSmith Account ⬜ (do this alongside Phase 3)

Go to `smith.langchain.com` → Sign up free.
Free tier: 5,000 traces/month — more than enough for 1 tester.

Create a project called `zoning-demo`.

Get your API key from Settings → API Keys → Create API Key.

Add it to Oracle Vault as `LANGSMITH_API_KEY` (you'll do this in Phase 3).

### 6.2 What Was Done in Code (already complete — do not re-do)

**`backend/query.py`** — both OpenAI clients wrapped with `langsmith.wrappers.wrap_openai` inside `init_vertex()`. This auto-traces every OpenAI call (analysis streaming + quick answer) when `LANGCHAIN_TRACING_V2=true`. No function changes needed.

**`backend/quick_answer.py`** — `@traceable(name="quick_answer", tags=["quick_mode"])` on the `quick_answer()` function for explicit trace context.

**`backend/requirements.txt`** — `langsmith>=0.1.0` added.

**The 3 env vars in your `backend.env` from Phase 3:**
```bash
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=zoning-demo
LANGSMITH_API_KEY=ls__...   # from smith.langchain.com
```

When `LANGCHAIN_TRACING_V2=true`, LangSmith captures:
- The full prompt sent to OpenAI (parcel context + by-law chunks + question)
- The full streamed response (accumulated)
- Token counts and latency per call
- `quick_mode` vs `analysis_mode` tags

View at `smith.langchain.com` → your project → Traces.

---

## Phase 7 — Build and Push the Backend Docker Image

The Dockerfile already exists in your repo (`backend/Dockerfile`) and pre-bakes the SPLADE model. You build on your local machine (where you have the code) and push to Docker Hub (free).

### 7.1 Create Docker Hub Account

Go to `hub.docker.com` → Sign up free → create repo `zoning-backend` (private repo, free for 1).

### 7.2 Build and Push

```bash
# On your local machine, in project root
docker build \
  --platform linux/arm64 \
  -t yourdockerhubusername/zoning-backend:latest \
  ./backend

# ARM64 is important — Oracle VM is ARM architecture
# This build takes 5-10 minutes (downloads SPLADE model into the image)

docker push yourdockerhubusername/zoning-backend:latest
```

### 7.3 Pull the Image on Oracle VM

```bash
ssh -i ~/.ssh/oracle_zoning.pem ubuntu@YOUR_VM_PUBLIC_IP

docker pull yourdockerhubusername/zoning-backend:latest
docker tag yourdockerhubusername/zoning-backend:latest zoning-backend:latest
```

### 7.4 Copy the Production Compose File to the VM

```bash
# On your local machine
scp -i ~/.ssh/oracle_zoning.pem \
  docker-compose.prod.yml \
  ubuntu@YOUR_VM_PUBLIC_IP:/home/ubuntu/zoning/docker-compose.yml
```

> The repo contains `docker-compose.prod.yml` — this is the production stack (no Qdrant, backend on `127.0.0.1:8000` only). It gets copied to the VM as `docker-compose.yml` so all `docker compose` commands work without a `-f` flag.

---

## Phase 8 — Frontend Deployment on Vercel

### 8.1 Commit and Push to GitHub

```bash
# On your local machine, in the project root
git add backend/requirements.txt   # was accidentally in .gitignore — now tracked
git add backend/.env.example
git add frontend/.env.local.example
git add docker-compose.prod.yml
git commit -m "chore: deployment prep — Dockerfile fix, prod compose, env templates"
git push
```

### 8.2 Deploy to Vercel

1. Go to `vercel.com` → New Project → Import your GitHub repo
2. Set Root Directory to `frontend`
3. Add Environment Variable: `NEXT_PUBLIC_API_BASE` = `https://api.yashpandav.dev`
4. Deploy

Vercel gives you `https://your-project.vercel.app`.

### 8.3 Add Custom Domain

In Vercel project settings → Domains → Add `app.yashpandav.dev`.

Vercel gives you a CNAME record. Add it in Cloudflare DNS:
- Type: CNAME
- Name: `app`
- Target: `cname.vercel-dns.com`
- Proxy status: **DNS only (grey cloud)** — important, let Vercel handle SSL for this one

Now `https://app.yashpandav.dev` is your permanent frontend URL.

---

## Phase 9 — Final Launch on Oracle VM

### 9.1 Load Secrets and Start Everything

```bash
# On Oracle VM
cd /home/ubuntu/zoning

# Pull secrets from Oracle Vault into backend.env
/home/ubuntu/load-secrets.sh

# Start all containers (uses docker-compose.yml = the prod file you SCP'd in Phase 7.4)
docker compose up -d

# Watch the logs during startup (SPLADE takes 60-90s to load on ARM)
docker compose logs -f backend
```

Wait until you see: `Application startup complete.` in the backend logs.

### 9.2 Verify the Full Stack

```bash
# From inside the VM
curl http://localhost:8000/api/health

# Expected:
{
  "status": "ok",
  "database": "connected",
  "qdrant": "connected",
  "openai": "connected",
  "voyage": "connected"
}
```

### 9.3 Test the Tunnel

From your local machine (not the VM):
```bash
curl https://api.yashpandav.dev/api/health
```

Expected: same JSON health response as the local curl above. If you get a 502 or connection error, check `docker compose logs cloudflared` on the VM — the tunnel may still be starting.

### 9.4 Test the Full User Flow

1. Open `https://app.yashpandav.dev` in a private browser window
2. The map loads directly — no login required
3. Click a parcel — parcel data populates the right panel
4. Type a Quick mode question — answer streams back
5. Switch to Analysis mode — test a multi-turn conversation
6. Check LangSmith — you should see the traces appearing live

---

## Phase 10 — Set Up Auto-Restart on VM Reboot

Oracle VMs can reboot for maintenance. Make everything restart automatically:

```bash
# Create a systemd service
sudo nano /etc/systemd/system/zoning.service
```

```ini
[Unit]
Description=Zoning AI Docker Stack
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/ubuntu/zoning
ExecStart=/bin/bash -c '/home/ubuntu/load-secrets.sh && docker compose up -d'
ExecStop=/usr/bin/docker compose down
User=ubuntu
Group=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable zoning.service
sudo systemctl start zoning.service
```

Now if Oracle reboots your VM, all containers come back up automatically within 2 minutes.

---

## Monitoring — What You See and Where

| What You Want to See | Where to Look |
|---------------------|---------------|
| Every AI prompt + response | LangSmith → Traces (real-time) |
| Token usage and cost per call | OpenAI Dashboard → Usage → Filter by date |
| Which parcels the architect clicked | PostGIS → `SELECT * FROM activity_log ORDER BY created_at DESC` |
| Which questions were asked | Same table + LangSmith |
| Container health | `docker compose ps` on VM |
| Live backend logs | `docker compose logs -f backend` on VM |
| VM resource usage | Oracle Cloud → Metrics, or `htop` on VM |

---

## Total Time Estimate

| Phase | Time |
|-------|------|
| Oracle VM setup | 30 min |
| Cloudflare domain + tunnel | 20 min |
| Oracle Vault secrets | 20 min |
| Qdrant Cloud snapshot upload | 30 min (+ transfer time) |
| PostGIS dump + transfer + restore | 30-60 min (depends on data size) |
| Docker build + push + pull | 20 min |
| Vercel frontend deploy | 10 min |
| Final launch + verification | 20 min |
| **Total** | **~3 to 4 hours** |

---

## What to Share With the Architect

One message, two things:

1. **URL:** `https://app.yashpandav.dev`
2. **What to test:** The map, Quick mode chat, and Analysis mode chat for any Toronto property

That's it. No login, no app to install, no API keys to manage — just open the URL and start clicking parcels.