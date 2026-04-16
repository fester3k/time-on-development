import hashlib, hmac, os, contextlib, sqlite3, urllib.request, json
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

DB             = os.getenv("DB_PATH", "/data/cycle.db")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")   # opcjonalny — ustaw jeśli chcesz weryfikować podpis
GH_TOKEN       = os.getenv("GH_TOKEN", "")         # PAT lub token z uprawnieniem read:project
GH_API_URL     = os.getenv("GH_API_URL", "https://api.github.com")  # dla GHES: https://ghes.example.com/api/v3  -- ale GraphQL jest na /api/graphql
GH_GRAPHQL_URL = os.getenv("GH_GRAPHQL_URL", "https://api.github.com/graphql")
STATE_X        = os.getenv("STATE_X", "In Progress")  # nazwa stanu X w Project


# ── DB helpers ────────────────────────────────────────────────────────────────

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
                issue_node_id    TEXT PRIMARY KEY,
                issue_number     INTEGER,
                repo             TEXT,
                left_x_at        TEXT,
                closed_at        TEXT,
                duration_seconds INTEGER
            )""")


# ── GitHub GraphQL helper ─────────────────────────────────────────────────────

def gh_graphql(query, variables):
    """Wywołuje GitHub GraphQL API i zwraca dict z .data"""
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
    """Zwraca (issue_node_id, issue_number, repo) na podstawie ProjectV2Item node_id"""
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


# ── Webhook signature verification ───────────────────────────────────────────

def verify_signature(payload_bytes):
    if not WEBHOOK_SECRET:
        return  # weryfikacja wyłączona
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        abort(401, "Invalid webhook signature")


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.post("/webhook")
def webhook():
    raw = request.get_data()
    verify_signature(raw)

    event = request.headers.get("X-GitHub-Event", "")
    d = request.json

    # ── projects_v2_item edited → opuszczenie stanu X ─────────────────────────
    if event == "projects_v2_item" and d.get("action") == "edited":
        changes = d.get("changes", {}).get("field_value", {})
        prev_name = (changes.get("from") or {}).get("name", "")

        if prev_name != STATE_X:
            return jsonify(ok=False, reason=f"transition not from '{STATE_X}', skipping"), 200

        item_node_id = d["projects_v2_item"]["node_id"]
        issue_node_id, issue_number, repo = resolve_issue_from_item(item_node_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with db() as con:
            con.execute("""
                INSERT INTO cycle_times (issue_node_id, issue_number, repo, left_x_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(issue_node_id) DO UPDATE SET
                    left_x_at = excluded.left_x_at,
                    closed_at = NULL,
                    duration_seconds = NULL
            """, (issue_node_id, issue_number, repo, now))

        return jsonify(ok=True, recorded="left_x", issue_number=issue_number, left_x_at=now)

    # ── issues closed ─────────────────────────────────────────────────────────
    if event == "issues" and d.get("action") == "closed":
        issue      = d["issue"]
        node_id    = issue["node_id"]
        closed_at  = issue["closed_at"]
        issue_num  = issue["number"]
        repo       = d["repository"]["full_name"]

        with db() as con:
            row = con.execute(
                "SELECT left_x_at FROM cycle_times WHERE issue_node_id=?", (node_id,)
            ).fetchone()

            if not row or not row["left_x_at"]:
                # Issue zamknięte bez wcześniejszego przejścia przez X — ignoruj
                return jsonify(ok=False, reason="no left_x recorded"), 200

            fmt   = "%Y-%m-%dT%H:%M:%SZ"
            start = datetime.strptime(row["left_x_at"], fmt).replace(tzinfo=timezone.utc)
            end   = datetime.strptime(closed_at,        fmt).replace(tzinfo=timezone.utc)
            dur   = int((end - start).total_seconds())

            con.execute("""
                UPDATE cycle_times
                SET closed_at=?, duration_seconds=?, issue_number=?, repo=?
                WHERE issue_node_id=?
            """, (closed_at, dur, issue_num, repo, node_id))

        return jsonify(ok=True, recorded="closed", duration_seconds=dur)

    return jsonify(ok=False, reason="unhandled event"), 200


# ── Query endpoints ───────────────────────────────────────────────────────────

# GET /issue/<node_id>
# GET /issue?number=42&repo=org/repo
@app.get("/issue")
@app.get("/issue/<node_id>")
def get_issue(node_id=None):
    with db() as con:
        if node_id:
            row = con.execute(
                "SELECT * FROM cycle_times WHERE issue_node_id=?", (node_id,)
            ).fetchone()
        else:
            num  = request.args.get("number", type=int)
            repo = request.args.get("repo")
            if not num or not repo:
                abort(400, "Provide node_id or ?number=N&repo=org/repo")
            row = con.execute(
                "SELECT * FROM cycle_times WHERE issue_number=? AND repo=?", (num, repo)
            ).fetchone()

    if not row:
        abort(404)
    return jsonify(dict(row))


# GET /issues?repo=org/repo
@app.get("/issues")
def list_issues():
    repo = request.args.get("repo")
    with db() as con:
        if repo:
            rows = con.execute(
                "SELECT * FROM cycle_times WHERE repo=? AND closed_at IS NOT NULL", (repo,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM cycle_times WHERE closed_at IS NOT NULL"
            ).fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)
