import hashlib, hmac, os, contextlib, sqlite3, urllib.request, json
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

DB             = os.getenv("DB_PATH", "/data/cycle.db")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GH_TOKEN       = os.getenv("GH_TOKEN", "")
GH_GRAPHQL_URL = os.getenv("GH_GRAPHQL_URL", "https://api.github.com/graphql")
STATE_X        = os.getenv("STATE_X", "In Progress")

# Allowlist project node IDs (PVT_...), oddzielone przecinkami.
# Jeśli puste — akceptuje wszystkie projekty (tryb dev).
ALLOWED_PROJECTS = {
    p.strip() for p in os.getenv("ALLOWED_PROJECTS", "").split(",") if p.strip()
}


# ── DB ────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS cycle_times (
                issue_node_id    TEXT NOT NULL,
                project_node_id  TEXT NOT NULL,
                issue_number     INTEGER,
                repo             TEXT,
                left_x_at        TEXT,
                closed_at        TEXT,
                duration_seconds INTEGER,
                PRIMARY KEY (issue_node_id, project_node_id)
            )""")


# ── GitHub GraphQL ────────────────────────────────────────────────────────────

def gh_graphql(query, variables):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GH_GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def resolve_issue_from_item(item_node_id):
    """Zwraca (issue_node_id, issue_number, repo) na podstawie ProjectV2Item node_id."""
    data = gh_graphql("""
        query($id: ID!) {
          node(id: $id) {
            ... on ProjectV2Item {
              content {
                ... on Issue {
                  id
                  number
                  repository { nameWithOwner }
                }
              }
            }
          }
        }
    """, {"id": item_node_id})
    content = data["data"]["node"]["content"]
    return (
        content["id"],
        content["number"],
        content["repository"]["nameWithOwner"],
    )


# ── Webhook signature ─────────────────────────────────────────────────────────

def verify_signature(payload_bytes):
    if not WEBHOOK_SECRET:
        return
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        abort(401, "Invalid webhook signature")


def check_project_allowed(project_node_id):
    """Zwraca False jeśli projekt nie jest na allowliście (→ drop)."""
    if not ALLOWED_PROJECTS:
        return True  # allowlist pusta = tryb dev, akceptuj wszystko
    return project_node_id in ALLOWED_PROJECTS


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook")
def webhook():
    raw = request.get_data()
    verify_signature(raw)

    event = request.headers.get("X-GitHub-Event", "")
    d     = request.json

    # ── projects_v2_item edited ───────────────────────────────────────────────
    if event == "projects_v2_item" and d.get("action") == "edited":
        project_node_id = d["projects_v2_item"]["project_node_id"]

        if not check_project_allowed(project_node_id):
            return jsonify(ok=False, reason="project not in allowlist, dropped"), 200

        changes   = d.get("changes", {}).get("field_value", {})
        prev_name = (changes.get("from") or {}).get("name", "")

        if prev_name != STATE_X:
            return jsonify(ok=False, reason=f"transition not from '{STATE_X}', skipping"), 200

        item_node_id                        = d["projects_v2_item"]["node_id"]
        issue_node_id, issue_number, repo   = resolve_issue_from_item(item_node_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with db() as con:
            con.execute("""
                INSERT INTO cycle_times
                    (issue_node_id, project_node_id, issue_number, repo, left_x_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(issue_node_id, project_node_id) DO UPDATE SET
                    left_x_at        = excluded.left_x_at,
                    closed_at        = NULL,
                    duration_seconds = NULL
            """, (issue_node_id, project_node_id, issue_number, repo, now))

        return jsonify(ok=True, recorded="left_x",
                       issue_number=issue_number, project=project_node_id, left_x_at=now)

    # ── issues closed ─────────────────────────────────────────────────────────
    if event == "issues" and d.get("action") == "closed":
        issue      = d["issue"]
        node_id    = issue["node_id"]
        closed_at  = issue["closed_at"]
        issue_num  = issue["number"]
        repo       = d["repository"]["full_name"]

        with db() as con:
            rows = con.execute(
                "SELECT project_node_id, left_x_at FROM cycle_times WHERE issue_node_id=?",
                (node_id,)
            ).fetchall()

            if not rows:
                return jsonify(ok=False, reason="no left_x recorded for any project"), 200

            fmt     = "%Y-%m-%dT%H:%M:%SZ"
            end     = datetime.strptime(closed_at, fmt).replace(tzinfo=timezone.utc)
            updated = []

            for row in rows:
                if not row["left_x_at"]:
                    continue
                start = datetime.strptime(row["left_x_at"], fmt).replace(tzinfo=timezone.utc)
                dur   = int((end - start).total_seconds())

                con.execute("""
                    UPDATE cycle_times
                    SET closed_at=?, duration_seconds=?, issue_number=?, repo=?
                    WHERE issue_node_id=? AND project_node_id=?
                """, (closed_at, dur, issue_num, repo, node_id, row["project_node_id"]))
                updated.append({"project": row["project_node_id"], "duration_seconds": dur})

        return jsonify(ok=True, recorded="closed", projects=updated)

    return jsonify(ok=False, reason="unhandled event"), 200


# ── Query endpoints ───────────────────────────────────────────────────────────

# GET /issue/<node_id>?project=PVT_...
# GET /issue?number=42&repo=org/repo&project=PVT_...   (project opcjonalny)
@app.get("/issue")
@app.get("/issue/<node_id>")
def get_issue(node_id=None):
    project = request.args.get("project")

    with db() as con:
        if node_id:
            if project:
                row = con.execute(
                    "SELECT * FROM cycle_times WHERE issue_node_id=? AND project_node_id=?",
                    (node_id, project)
                ).fetchone()
            else:
                # bez project — zwróć wszystkie wpisy dla tego issue
                rows = con.execute(
                    "SELECT * FROM cycle_times WHERE issue_node_id=?", (node_id,)
                ).fetchall()
                if not rows:
                    abort(404)
                return jsonify([dict(r) for r in rows])
        else:
            num  = request.args.get("number", type=int)
            repo = request.args.get("repo")
            if not num or not repo:
                abort(400, "Provide node_id or ?number=N&repo=org/repo")
            if project:
                row = con.execute(
                    "SELECT * FROM cycle_times WHERE issue_number=? AND repo=? AND project_node_id=?",
                    (num, repo, project)
                ).fetchone()
            else:
                rows = con.execute(
                    "SELECT * FROM cycle_times WHERE issue_number=? AND repo=?",
                    (num, repo)
                ).fetchall()
                if not rows:
                    abort(404)
                return jsonify([dict(r) for r in rows])

    if not row:
        abort(404)
    return jsonify(dict(row))


# GET /issues?repo=org/repo&project=PVT_...   (oba opcjonalne)
@app.get("/issues")
def list_issues():
    repo    = request.args.get("repo")
    project = request.args.get("project")

    clauses = ["closed_at IS NOT NULL"]
    params  = []
    if repo:
        clauses.append("repo=?")
        params.append(repo)
    if project:
        clauses.append("project_node_id=?")
        params.append(project)

    sql = "SELECT * FROM cycle_times WHERE " + " AND ".join(clauses)

    with db() as con:
        rows = con.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)
