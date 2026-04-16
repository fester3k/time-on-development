from flask import Flask, request, jsonify, abort
import sqlite3, os, contextlib

app = Flask(__name__)
DB = os.getenv("DB_PATH", "/data/cycle.db")


def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


@contextlib.contextmanager
def db():
    con = get_db()
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


# POST /event
# body: { "type": "left_x"|"closed", "issue_node_id": "...",
#          "issue_number": 42, "repo": "org/repo", "timestamp": "ISO8601" }
@app.post("/event")
def post_event():
    d = request.json
    if not d or "type" not in d:
        abort(400)

    node_id = d["issue_node_id"]

    if d["type"] == "left_x":
        with db() as con:
            con.execute("""
                INSERT INTO cycle_times (issue_node_id, issue_number, repo, left_x_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(issue_node_id) DO UPDATE SET
                    left_x_at = excluded.left_x_at,
                    closed_at = NULL,
                    duration_seconds = NULL
            """, (node_id, d["issue_number"], d["repo"], d["timestamp"]))
        return jsonify(ok=True)

    if d["type"] == "closed":
        with db() as con:
            row = con.execute(
                "SELECT left_x_at FROM cycle_times WHERE issue_node_id=?", (node_id,)
            ).fetchone()
            if not row or not row["left_x_at"]:
                return jsonify(ok=False, reason="no left_x recorded"), 202

            from datetime import datetime, timezone
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            start = datetime.strptime(row["left_x_at"], fmt).replace(tzinfo=timezone.utc)
            end   = datetime.strptime(d["timestamp"],   fmt).replace(tzinfo=timezone.utc)
            dur   = int((end - start).total_seconds())

            con.execute("""
                UPDATE cycle_times
                SET closed_at=?, duration_seconds=?
                WHERE issue_node_id=?
            """, (d["timestamp"], dur, node_id))
        return jsonify(ok=True, duration_seconds=dur)

    abort(400)


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
                abort(400)
            row = con.execute(
                "SELECT * FROM cycle_times WHERE issue_number=? AND repo=?", (num, repo)
            ).fetchone()

    if not row:
        abort(404)
    return jsonify(dict(row))


# GET /issues?repo=org/repo  (lista wszystkich zamkniętych)
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
