# cycle-time-monitor

A lightweight service that tracks **how long a GitHub issue spends from the moment it leaves a specific Project status ("State X") until it is closed**. Data is stored in SQLite and exposed via a simple HTTP API that any GitHub Actions workflow can query with `curl`.

---

## How it works

```
┌─────────────────────────────────────────────────────────────────┐
│  GitHub (GHES)                                                  │
│                                                                 │
│  Issue moves away from "In Progress"                            │
│       └─► Organization Webhook ──► POST /webhook ──► SQLite    │
│                                          ▲                      │
│  Issue is closed                         │                      │
│       └─► Organization Webhook ──────────┘                      │
│                                                                 │
│  Your workflow needs the data                                   │
│       └─► curl GET /issue?number=42&repo=org/repo ──► JSON     │
└─────────────────────────────────────────────────────────────────┘
```

1. **GitHub sends webhooks** directly to this service — no GitHub Actions workflow is needed for collection.
2. When an issue **leaves State X** (e.g. "In Progress"), the service records the timestamp.
3. When the issue is **closed**, the service calculates and stores the elapsed time.
4. Any workflow can **query** the service via `curl` and receive the timing data as JSON.

> **Why no collector workflow?**
> GitHub Enterprise Server (GHES) does not support `projects_v2_item` as a workflow trigger.
> Using organization-level webhooks pointing directly at the service is the correct approach.

---

## Repository structure

```
.
├── docker-compose.yml          # Run the service with one command
├── service/
│   ├── app.py                  # The Flask HTTP service (all logic lives here)
│   ├── Dockerfile              # Container definition
│   └── requirements.txt        # Python dependencies (Flask only)
└── .github/
    └── workflows/
        ├── query.yml           # Reusable workflow — call this to fetch timing data
        └── example-consumer.yml  # Example of how another workflow calls query.yml
```

---

## Prerequisites

| What | Why |
|------|-----|
| Docker + Docker Compose | To run the service |
| A host reachable from GitHub runners | The service must accept HTTP on port 8080 |
| GitHub Personal Access Token (PAT) | To resolve issue details via GraphQL API |
| Org-level webhook in GHES | To push events to the service |

---

## Quickstart

### 1. Clone and configure

```bash
git clone https://github.com/fester3k/time-on-development.git
cd time-on-development
```

Open `docker-compose.yml` and fill in the environment variables:

```yaml
environment:
  - STATE_X=In Progress        # The project status name you want to measure FROM
  - GH_TOKEN=ghp_xxxxxxxxxxxx  # PAT with read:org and read:project scopes
  - GH_GRAPHQL_URL=https://your-ghes.example.com/api/graphql  # Your GHES GraphQL endpoint
  - WEBHOOK_SECRET=my-secret   # Optional but recommended — must match the GH webhook config
  - ALLOWED_PROJECTS=PVT_aaa,PVT_bbb  # Comma-separated list of allowed Project node IDs
```

> **How to find a Project node ID (`PVT_...`)**
> ```bash
> gh api graphql -f query='
>   query { organization(login: "YOUR_ORG") {
>     projectsV2(first: 20) { nodes { id title } }
>   }}'
> ```

### 2. Start the service

```bash
docker compose up -d
```

The service starts on port `8080`. SQLite data is persisted in a Docker volume (`cycle-data`).

### 3. Configure the GitHub webhook

In your GHES organization settings → **Webhooks** → **Add webhook**:

| Field | Value |
|-------|-------|
| Payload URL | `http://your-host:8080/webhook` |
| Content type | `application/json` |
| Secret | Same value as `WEBHOOK_SECRET` in docker-compose |
| Events | ✅ **Issues**, ✅ **Projects v2 item** |

That's it — the service will start receiving events immediately.

---

## HTTP API reference

### `POST /webhook`
Receives events from GitHub. You never call this manually — GitHub calls it automatically.

---

### `GET /issue/<node_id>`
Returns timing data for a single issue identified by its GitHub node ID (`I_kw...`).

```bash
curl http://your-host:8080/issue/I_kwDOBnlbAc5abc123
```

**Optional query parameter:** `?project=PVT_...` — filter by specific project.

---

### `GET /issue?number=N&repo=org/repo`
Returns timing data identified by issue number and repository. Easier to use from workflows.

```bash
curl "http://your-host:8080/issue?number=42&repo=myorg/myrepo"
curl "http://your-host:8080/issue?number=42&repo=myorg/myrepo&project=PVT_aaa111"
```

**Response (single project match):**
```json
{
  "issue_node_id": "I_kwDOBnlbAc5abc123",
  "project_node_id": "PVT_aaa111",
  "issue_number": 42,
  "repo": "myorg/myrepo",
  "left_x_at": "2026-03-01T10:00:00Z",
  "closed_at": "2026-03-05T14:30:00Z",
  "duration_seconds": 363000
}
```

> If the issue belongs to **multiple tracked projects** and no `?project=` is provided, the response is an **array** — one entry per project.

---

### `GET /issues?repo=org/repo&project=PVT_...`
Returns all **closed** issues. Both parameters are optional and can be combined.

```bash
# All closed issues in a specific project
curl "http://your-host:8080/issues?project=PVT_aaa111"

# All closed issues in a specific repo
curl "http://your-host:8080/issues?repo=myorg/myrepo"

# Combined
curl "http://your-host:8080/issues?repo=myorg/myrepo&project=PVT_aaa111"
```

---

## Using the query workflow

`query.yml` is a **reusable workflow** (`workflow_call`). Call it from any other workflow to get timing data as outputs.

```yaml
jobs:
  get-cycle-time:
    uses: fester3k/time-on-development/.github/workflows/query.yml@main
    with:
      issue_number: "42"
      repo: "myorg/myrepo"
      # project: "PVT_aaa111"   # optional

  process:
    needs: get-cycle-time
    runs-on: self-hosted
    steps:
      - run: |
          echo "Duration: ${{ needs.get-cycle-time.outputs.duration_seconds }} seconds"
          echo "Left State X at: ${{ needs.get-cycle-time.outputs.left_x_at }}"
          echo "Closed at: ${{ needs.get-cycle-time.outputs.closed_at }}"
          # Full JSON also available:
          echo '${{ needs.get-cycle-time.outputs.result_json }}'
```

### Available outputs

| Output | Description |
|--------|-------------|
| `duration_seconds` | Total seconds between leaving State X and closing |
| `left_x_at` | ISO 8601 timestamp when the issue left State X (last recorded) |
| `closed_at` | ISO 8601 timestamp when the issue was closed |
| `result_json` | Full JSON object as returned by the API |

---

## Multiple projects

The same issue can be tracked independently across multiple projects. The service stores a separate row for each `(issue, project)` pair.

```
Issue #42 in Project A  →  left_x_at: March 1,  duration: 4 days
Issue #42 in Project B  →  left_x_at: March 3,  duration: 2 days
```

**Allowlist** — only projects listed in `ALLOWED_PROJECTS` are tracked. Webhooks from any other project are silently dropped with HTTP 200. If `ALLOWED_PROJECTS` is empty, all projects are accepted (useful during development).

---

## Configuration reference

All configuration is done via environment variables in `docker-compose.yml`.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STATE_X` | ✅ | `In Progress` | Name of the project status to measure FROM |
| `GH_TOKEN` | ✅ | — | PAT with `read:org` and `read:project` scopes |
| `GH_GRAPHQL_URL` | ✅ | `https://api.github.com/graphql` | GraphQL endpoint — change for GHES |
| `ALLOWED_PROJECTS` | — | *(empty = all)* | Comma-separated list of `PVT_...` node IDs to track |
| `WEBHOOK_SECRET` | — | *(empty = disabled)* | Shared secret for verifying webhook signatures |
| `DB_PATH` | — | `/data/cycle.db` | Path to the SQLite database file inside the container |

---

## How timing works

- **`left_x_at`** is recorded every time an issue transitions **away from State X**. If this happens more than once (e.g. issue goes back to X and leaves again), only the **most recent** transition is kept — previous data for that issue is overwritten.
- **`duration_seconds`** is calculated as `closed_at − left_x_at` at the moment the issue is closed.
- Issues closed without ever having left State X are **ignored**.

---

## Local development

```bash
# Run without Docker
cd service
pip install -r requirements.txt
DB_PATH=./dev.db GH_TOKEN=ghp_xxx STATE_X="In Progress" python app.py

# Send a test webhook manually
curl -X POST http://localhost:8080/webhook \
  -H "X-GitHub-Event: issues" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "closed",
    "issue": {
      "node_id": "I_test001",
      "number": 1,
      "closed_at": "2026-04-16T12:00:00Z"
    },
    "repository": { "full_name": "myorg/myrepo" }
  }'
```
