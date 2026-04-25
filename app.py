"""
Revision Radar — Web Interface
Flask app for Railway deployment.

Routes:
  GET  /          → upload page
  POST /run        → process two PDFs, return report
  POST /signup     → beta signup (name + email)
  GET  /stats      → usage count JSON
  GET  /health     → Railway health check
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template, g

from revision_radar import parse_script, diff_scripts
from revision_radar.classifier import classify_all
from revision_radar.report import render_report, render_all_dept_reports

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

# Use Postgres on Railway (DATABASE_URL set), SQLite locally otherwise.
_DATABASE_URL = os.environ.get("DATABASE_URL")
_USE_POSTGRES = bool(_DATABASE_URL)

if _USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
else:
    import sqlite3
    _SQLITE_PATH = Path(__file__).parent / "local_dev.db"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Return a db connection, reusing within a request."""
    if "db" not in g:
        if _USE_POSTGRES:
            url = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
            g.db = psycopg2.connect(url, cursor_factory=RealDictCursor)
        else:
            g.db = sqlite3.connect(str(_SQLITE_PATH))
            g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist yet (Postgres or SQLite)."""
    db = get_db()
    if _USE_POSTGRES:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage_counter (
                    id      SERIAL PRIMARY KEY,
                    count   BIGINT NOT NULL DEFAULT 0
                );
                INSERT INTO usage_counter (count)
                SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM usage_counter);

                CREATE TABLE IF NOT EXISTS beta_signups (
                    id         SERIAL PRIMARY KEY,
                    name       TEXT NOT NULL,
                    email      TEXT NOT NULL UNIQUE,
                    signed_up  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS issues (
                    id          SERIAL PRIMARY KEY,
                    description TEXT NOT NULL,
                    email       TEXT,
                    submitted   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            db.commit()
    else:
        cur = db.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS usage_counter (
                id    INTEGER PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO usage_counter (id, count) VALUES (1, 0);

            CREATE TABLE IF NOT EXISTS beta_signups (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                email     TEXT NOT NULL UNIQUE,
                signed_up TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS issues (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                email       TEXT,
                submitted   TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        db.commit()


def increment_usage():
    db = get_db()
    if _USE_POSTGRES:
        with db.cursor() as cur:
            cur.execute("UPDATE usage_counter SET count = count + 1 RETURNING count;")
            row = cur.fetchone()
            db.commit()
            return row["count"] if row else 0
    else:
        cur = db.cursor()
        cur.execute("UPDATE usage_counter SET count = count + 1 WHERE id = 1;")
        cur.execute("SELECT count FROM usage_counter WHERE id = 1;")
        row = cur.fetchone()
        db.commit()
        return row["count"] if row else 0


def get_usage_count():
    db = get_db()
    if _USE_POSTGRES:
        with db.cursor() as cur:
            cur.execute("SELECT count FROM usage_counter LIMIT 1;")
            row = cur.fetchone()
            return row["count"] if row else 0
    else:
        cur = db.cursor()
        cur.execute("SELECT count FROM usage_counter WHERE id = 1;")
        row = cur.fetchone()
        return row["count"] if row else 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/")
def index():
    try:
        count = get_usage_count()
    except Exception:
        count = 0
    return render_template("index.html", usage_count=count)


@app.route("/stats")
def stats():
    try:
        count = get_usage_count()
        return jsonify({"runs": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/run", methods=["POST"])
def run():
    """Accept two PDF uploads, run Revision Radar, return the output PDF."""
    if "old_script" not in request.files or "new_script" not in request.files:
        return jsonify({"error": "Please upload both the old and new script PDFs."}), 400

    old_file = request.files["old_script"]
    new_file = request.files["new_script"]

    if not old_file.filename or not new_file.filename:
        return jsonify({"error": "Both files must be selected."}), 400

    all_depts = request.form.get("all_depts") == "1"

    # Write uploads to temp files
    tmp_dir = tempfile.mkdtemp()
    old_path = Path(tmp_dir) / f"old_{uuid.uuid4().hex}.pdf"
    new_path = Path(tmp_dir) / f"new_{uuid.uuid4().hex}.pdf"
    out_path = Path(tmp_dir) / "revision_radar_report.pdf"

    try:
        old_file.save(str(old_path))
        new_file.save(str(new_path))

        # Run core logic
        old_script = parse_script(str(old_path))
        new_script = parse_script(str(new_path))
        changes = diff_scripts(old_script, new_script)
        classify_all(changes)

        if all_depts:
            # Generate master + all dept reports, zip them
            render_report(old_script, new_script, changes, str(out_path))
            dept_files = render_all_dept_reports(
                old_script, new_script, changes,
                Path(tmp_dir), "revision_radar"
            )
            # Zip everything
            import zipfile
            zip_path = Path(tmp_dir) / "revision_radar_reports.zip"
            with zipfile.ZipFile(str(zip_path), "w") as zf:
                zf.write(str(out_path), "revision_radar_master.pdf")
                for f in dept_files:
                    zf.write(str(f), f.name)

            # Increment counter
            try:
                increment_usage()
            except Exception:
                pass

            return send_file(
                str(zip_path),
                mimetype="application/zip",
                as_attachment=True,
                download_name="revision_radar_reports.zip",
            )
        else:
            render_report(old_script, new_script, changes, str(out_path))

            # Increment counter
            try:
                increment_usage()
            except Exception:
                pass

            # Build a clean download filename from the new script title
            title_slug = (new_script.title or "report").replace(" ", "_").replace("/", "-")
            download_name = f"{title_slug}_Revision_Radar.pdf"

            return send_file(
                str(out_path),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=download_name,
            )

    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500


@app.route("/signup", methods=["POST"])
def signup():
    """Save a beta signup (name + email)."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()

    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required."}), 400

    try:
        db = get_db()
        if _USE_POSTGRES:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO beta_signups (name, email) VALUES (%s, %s) "
                    "ON CONFLICT (email) DO NOTHING RETURNING id;",
                    (name, email),
                )
                row = cur.fetchone()
                db.commit()
            already = row is None
        else:
            cur = db.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO beta_signups (name, email) VALUES (?, ?);",
                (name, email),
            )
            already = cur.rowcount == 0
            db.commit()
        return jsonify({"ok": True, "already_registered": already})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/report-issue", methods=["POST"])
def report_issue():
    """Save a user-submitted issue report."""
    data = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip()
    email = (data.get("email") or "").strip().lower() or None

    if not description:
        return jsonify({"error": "Description is required."}), 400

    try:
        db = get_db()
        if _USE_POSTGRES:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO issues (description, email) VALUES (%s, %s);",
                    (description, email),
                )
                db.commit()
        else:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO issues (description, email) VALUES (?, ?);",
                (description, email),
            )
            db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"[WARNING] Could not init DB on startup (may be fine locally): {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
