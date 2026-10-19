"""
PeerFlow prototype: intake agent for academic peer review.

Flow (each step is logged with a timestamp, which feeds the VSM tracker):
  1. received      -> file uploaded through the web page
  2. validated     -> file type checked, pages counted
  3. id_assigned   -> unique document ID created
  4. email_sent    -> confirmation email sent to the author
  5. triage        -> long-document trigger evaluated (editor notified if too long)
  6. ready_for_review -> document handed to the next stage (reviewer assignment)
"""
import json
import os
import smtplib
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

from flask import Flask, jsonify, redirect, render_template, request, url_for
from pypdf import PdfReader
from werkzeug.utils import secure_filename

# ---------------- Configuration ----------------
# Priority: environment variables (used on a hosting service) > local_settings.py (your own computer) > defaults.
# local_settings.py holds secrets such as the Zapier URL: it is listed in .gitignore and must NEVER be uploaded to GitHub.
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import local_settings  # optional; copy local_settings.example.py to local_settings.py
except ImportError:
    local_settings = None


def setting(name, default=""):
    value = os.getenv(name)
    if value not in (None, ""):
        return value
    return str(getattr(local_settings, name, default))


PAGE_LIMIT = int(setting("PAGE_LIMIT", "10"))  # trigger if pages > PAGE_LIMIT
EDITOR_EMAIL = setting("EDITOR_EMAIL", "")
SMTP_HOST = setting("SMTP_HOST", "")  # e.g. smtp.gmail.com (many hosts block SMTP; Zapier avoids that)
SMTP_PORT = int(setting("SMTP_PORT", "587"))
SMTP_USER = setting("SMTP_USER", "")
SMTP_PASS = setting("SMTP_PASS", "")  # for Gmail use an "app password"
MAIL_FROM = setting("MAIL_FROM", SMTP_USER or "peerflow@example.com")
WEBHOOK_URL = setting("WEBHOOK_URL", "https://hooks.zapier.com/hooks/catch/29155576/4183jjx/")  # Zapier "Catch Hook" URL (acts like a password)
REAL_EMAILS_ENABLED = setting("REAL_EMAILS_ENABLED", "1") != "0"  # "0" = never send, emails are only simulated
EMAIL_DAILY_LIMIT = int(setting("EMAIL_DAILY_LIMIT", "20"))  # protects your Zapier task quota

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "peerflow.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

STAGES = ["received", "validated", "id_assigned", "email_sent", "triage", "ready_for_review"]

# Minutes a person would take to do each step by hand. These are EXAMPLE values:
# measure your own manual process and replace them from the tracker page.
DEFAULT_MANUAL_MIN = {
    "received": 2, "validated": 3, "id_assigned": 2,
    "email_sent": 4, "triage": 2, "ready_for_review": 1,
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB


# ---------------- Database ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                filename TEXT, author_name TEXT, author_email TEXT, title TEXT,
                pages INTEGER, flagged_long INTEGER DEFAULT 0, created_at REAL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT, stage TEXT, started_at REAL, ended_at REAL, note TEXT
            )"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS email_log (ts REAL)")
        cols = [r[1] for r in c.execute("PRAGMA table_info(documents)")]
        if "status" not in cols:  # older databases: existing documents count as accepted
            c.execute("ALTER TABLE documents ADD COLUMN status TEXT DEFAULT 'accepted'")
        if "reject_reason" not in cols:
            c.execute("ALTER TABLE documents ADD COLUMN reject_reason TEXT DEFAULT ''")


init_db()


def log_stage(doc_id, stage, started_at, note=""):
    """Record a stage with its start and end time (needed for VSM metrics)."""
    with db() as c:
        c.execute(
            "INSERT INTO events (doc_id, stage, started_at, ended_at, note) VALUES (?,?,?,?,?)",
            (doc_id, stage, started_at, time.time(), note),
        )


def register_document(doc_id, filename, name, email, title, t0):
    """Create the document record and log stage 1 (received)."""
    with db() as c:
        c.execute(
            "INSERT INTO documents (doc_id, filename, author_name, author_email, title, pages, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (doc_id, filename, name, email, title, 0, t0),
        )
    log_stage(doc_id, "received", t0, filename)


def reject_document(doc_id, started_at, reason, note):
    """Keep the failed attempt in the metrics (pass rate) instead of deleting it."""
    with db() as c:
        c.execute("UPDATE documents SET status='rejected', reject_reason=? WHERE doc_id=?", (reason, doc_id))
    log_stage(doc_id, "validated", started_at, "RECHAZADO: " + note)


# ---------------- Helpers ----------------
def new_doc_id():
    year = datetime.now(timezone.utc).year
    return f"PR-{year}-{uuid.uuid4().hex[:6].upper()}"


def post_webhook(payload):
    """POST a JSON payload to WEBHOOK_URL (e.g. a Zapier 'Catch Hook'). Never crashes the pipeline."""
    import urllib.request

    req = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return "sent via webhook"
    except Exception as e:
        print("Webhook failed:", e, flush=True)
        return "webhook FAILED (see console)"


def log_email():
    """Remember a real email so the daily limit can be enforced."""
    with db() as c:
        c.execute("INSERT INTO email_log (ts) VALUES (?)", (time.time(),))


def emails_sent_today():
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM email_log WHERE ts >= ?", (start,)).fetchone()[0]


def email_decision(requested):
    """Decide whether REAL emails go out for this submission. Returns (send?, reason when not)."""
    if not REAL_EMAILS_ENABLED:
        return False, "el envío de correos está desactivado en el servidor"
    if not requested:
        return False, "no se solicitó el envío"
    if emails_sent_today() >= EMAIL_DAILY_LIMIT:
        return False, "se alcanzó el límite diario de correos"
    return True, ""


def send_email(to, subject, body, extra=None, real=True, why=""):
    """Deliver an email. If real=False it is only simulated (printed in the console, nothing is sent).
    Otherwise, first option configured: 1) SMTP  2) webhook (Zapier sends it)  3) console (demo mode)."""
    if not real:
        print(f"\n--- [CORREO SIMULADO: {why}] to={to}\nsubject={subject}\n{body}\n---\n", flush=True)
        return "simulado: " + why
    if SMTP_HOST:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log_email()
        return "sent via SMTP"
    if WEBHOOK_URL:
        payload = {"to": to, "subject": subject, "body": body}
        payload.update(extra or {})
        result = post_webhook(payload)
        if result.startswith("sent"):
            log_email()
        return result
    print(f"\n--- [DEMO EMAIL] to={to}\nsubject={subject}\n{body}\n---\n", flush=True)
    return "demo (printed to console)"


def fire_long_doc_trigger(doc_id, title, pages, real=True, why=""):
    """The trigger: alert the editor by email (SMTP, Zapier webhook, or simulated)."""
    body = (
        f"El documento {doc_id} (\"{title}\") tiene {pages} páginas, por encima del límite de {PAGE_LIMIT}.\n"
        "Fue marcado para revisión editorial inicial manual."
    )
    extra = {"event": "long_document", "doc_id": doc_id, "pages": pages}
    send_email(EDITOR_EMAIL, f"[PeerFlow] Documento largo marcado: {doc_id}", body, extra, real, why)
    if real and SMTP_HOST and WEBHOOK_URL:  # SMTP sent the email; also notify the webhook (Slack, Sheets...)
        post_webhook({"text": body, **extra})


def get_baselines():
    """Return ({stage: manual minutes}, customized?). Falls back to the example values."""
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='baselines'").fetchone()
    if row:
        try:
            saved = json.loads(row["value"])
            return {s: float(saved.get(s, DEFAULT_MANUAL_MIN[s])) for s in STAGES}, True
        except (ValueError, TypeError):
            pass
    return dict(DEFAULT_MANUAL_MIN), False


# ---------------- Routes ----------------
@app.context_processor
def inject_email_settings():
    return {"real_emails_enabled": REAL_EMAILS_ENABLED, "email_daily_limit": EMAIL_DAILY_LIMIT}


@app.route("/")
def index():
    return render_template("index.html", page_limit=PAGE_LIMIT)


@app.route("/submit", methods=["POST"])
def submit():
    t0 = time.time()
    file = request.files.get("document")
    name = request.form.get("author_name", "").strip()
    email = request.form.get("author_email", "").strip()
    title = request.form.get("title", "").strip() or "Untitled"

    send_requested = request.form.get("send_email") == "on"  # the author chose to send real emails (Zapier)
    if not file or not file.filename or (send_requested and not email):
        return render_template(
            "index.html", page_limit=PAGE_LIMIT,
            error="Falta el archivo PDF, o tu correo si quieres recibir la confirmación.",
        ), 400
    doc_id = new_doc_id()
    if not file.filename.lower().endswith(".pdf"):
        # counted as a rejected submission in the metrics; the file itself is not stored
        register_document(doc_id, file.filename, name, email, title, t0)
        reject_document(doc_id, time.time(), "not_pdf", "el archivo no es PDF")
        return render_template("index.html", page_limit=PAGE_LIMIT, error="Solo se aceptan archivos PDF."), 400

    # Stage 1: received
    path = os.path.join(UPLOAD_DIR, f"{doc_id}_{secure_filename(file.filename)}")
    file.save(path)
    register_document(doc_id, file.filename, name, email, title, t0)

    # Stage 2: validate + count pages
    t = time.time()
    try:
        pages = len(PdfReader(path).pages)
    except Exception:
        os.remove(path)  # the unreadable file is not kept, but the attempt stays in the metrics
        reject_document(doc_id, t, "unreadable_pdf", "PDF ilegible")
        return render_template("index.html", page_limit=PAGE_LIMIT, error="No se pudo leer ese PDF. Intenta con otro archivo."), 400
    with db() as c:
        c.execute("UPDATE documents SET pages=? WHERE doc_id=?", (pages, doc_id))
    log_stage(doc_id, "validated", t, f"{pages} pages")

    # Stage 3: ID assigned (already generated; logged as its own step for the VSM)
    log_stage(doc_id, "id_assigned", time.time(), doc_id)

    # Stage 4: confirmation email
    t = time.time()
    real, why = email_decision(send_requested)
    status = send_email(
        email or "(sin correo)",
        f"Envío recibido: {doc_id}",
        f"Hola {name or 'autor/a'},\n\nRecibimos tu manuscrito \"{title}\" ({pages} páginas).\n"
        f"Tu ID de documento es: {doc_id}\n\nConserva este ID para dar seguimiento a tu envío.\n\n- PeerFlow",
        {"event": "submission_received", "doc_id": doc_id, "pages": pages},
        real, why,
    )
    log_stage(doc_id, "email_sent", t, status)

    # Stage 5: long-document trigger
    t = time.time()
    flagged = pages > PAGE_LIMIT
    if flagged:
        fire_long_doc_trigger(doc_id, title, pages, real, why)
        with db() as c:
            c.execute("UPDATE documents SET flagged_long=1 WHERE doc_id=?", (doc_id,))
    log_stage(doc_id, "triage", t, "FLAGGED long document" if flagged else "ok")

    # Stage 6: ready for next step (reviewer matching would plug in here)
    log_stage(doc_id, "ready_for_review", time.time(), "")

    return redirect(url_for("done", doc_id=doc_id))


@app.route("/done/<doc_id>")
def done(doc_id):
    with db() as c:
        doc = c.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if not doc:
        return "Not found", 404
    with db() as c:
        ev = c.execute("SELECT note FROM events WHERE doc_id=? AND stage='email_sent'", (doc_id,)).fetchone()
    email_status = ev["note"] if ev else ""
    return render_template("done.html", doc=doc, page_limit=PAGE_LIMIT, email_status=email_status)


@app.route("/tracker")
def tracker():
    return render_template("tracker.html", stages=STAGES, page_limit=PAGE_LIMIT)


@app.route("/api/baselines", methods=["GET", "POST"])
def api_baselines():
    """Read or save the manual minutes per stage (used to compute time saved)."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        clean = {}
        for s in STAGES:
            try:
                v = float(data[s])
            except (KeyError, TypeError, ValueError):
                return jsonify(error=f"Invalid value for {s}"), 400
            if not (0 <= v <= 600):  # also rejects NaN
                return jsonify(error=f"Value for {s} must be between 0 and 600"), 400
            clean[s] = v
        with db() as c:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('baselines', ?)", (json.dumps(clean),))
        return jsonify(ok=True, baselines=clean, customized=True)
    baselines, customized = get_baselines()
    return jsonify(baselines=baselines, customized=customized)


@app.route("/api/vsm")
def api_vsm():
    """Live data for the tracker: per-stage averages + per-document timelines."""
    with db() as c:
        docs = c.execute("SELECT * FROM documents ORDER BY created_at DESC LIMIT 500").fetchall()
        events = c.execute("SELECT * FROM events ORDER BY id").fetchall()

    by_doc = {}
    for e in events:
        by_doc.setdefault(e["doc_id"], []).append(dict(e))

    baselines, customized = get_baselines()
    manual_total_s = sum(baselines.values()) * 60  # what a person would take for one document
    stage_proc = {s: [] for s in STAGES}  # process time per stage
    stage_wait = {s: [] for s in STAGES}  # wait time before each stage
    out_docs = []
    for d in docs:
        evs = by_doc.get(d["doc_id"], [])
        prev_end = d["created_at"]
        timeline = []
        for e in evs:
            proc = max(e["ended_at"] - e["started_at"], 0)
            wait = max(e["started_at"] - prev_end, 0)
            stage_proc[e["stage"]].append(proc)
            stage_wait[e["stage"]].append(wait)
            timeline.append({"stage": e["stage"], "process_s": proc, "wait_s": wait, "note": e["note"]})
            prev_end = e["ended_at"]
        lead = (evs[-1]["ended_at"] - d["created_at"]) if evs else 0
        is_done = bool(evs) and evs[-1]["stage"] == STAGES[-1]
        manual = manual_total_s if is_done else 0
        saved = (manual - lead) if is_done else 0
        out_docs.append(
            {
                "doc_id": d["doc_id"],
                "title": d["title"],
                "pages": d["pages"],
                "flagged_long": bool(d["flagged_long"]),
                "created_at": d["created_at"],
                "stage_reached": evs[-1]["stage"] if evs else "-",
                "complete": bool(evs) and evs[-1]["stage"] == STAGES[-1],
                "lead_time_s": lead,
                "manual_s": manual,
                "status": d["status"] or "accepted",
                "reject_reason": d["reject_reason"] or "",
                "saved_s": saved,
                "timeline": timeline,
            }
        )

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0

    stages = [
        {"stage": s, "avg_process_s": avg(stage_proc[s]), "avg_wait_s": avg(stage_wait[s]), "count": len(stage_proc[s])}
        for s in STAGES
    ]
    total_proc = sum(x["avg_process_s"] for x in stages)
    total_wait = sum(x["avg_wait_s"] for x in stages)
    efficiency = (total_proc / (total_proc + total_wait) * 100) if (total_proc + total_wait) else 0
    return jsonify(
        {
            "stages": stages,
            "docs": out_docs,
            "totals": {
                "documents": len(docs),
                "flagged": sum(1 for d in out_docs if d["flagged_long"]),
                "avg_lead_time_s": avg([d["lead_time_s"] for d in out_docs if d["complete"]]),
                "process_time_s": total_proc,
                "wait_time_s": total_wait,
                "flow_efficiency_pct": efficiency,
                "accepted": sum(1 for d in out_docs if d["status"] != "rejected"),
                "rejected": sum(1 for d in out_docs if d["status"] == "rejected"),
                # approved = readable PDF AND within the page limit (no rejection, no long-document flag)
                "passed": sum(1 for d in out_docs if d["status"] != "rejected" and not d["flagged_long"]),
                "pass_rate_pct": (
                    sum(1 for d in out_docs if d["status"] != "rejected" and not d["flagged_long"])
                    / len(out_docs) * 100 if out_docs else 0
                ),
                "rejected_by_reason": {
                    r: sum(1 for d in out_docs if d["reject_reason"] == r)
                    for r in sorted({d["reject_reason"] for d in out_docs if d["reject_reason"]})
                },
                "completed": sum(1 for d in out_docs if d["complete"]),
                "manual_s": sum(d["manual_s"] for d in out_docs),
                "saved_s": sum(d["saved_s"] for d in out_docs),
                "manual_per_doc_s": manual_total_s,
            },
            "baselines": baselines,
            "baselines_customized": customized,
            "page_limit": PAGE_LIMIT,
        }
    )


def startup_report():
    """Print how emails will be delivered, so a wrong setup is obvious (the URL itself is never printed)."""
    if SMTP_HOST:
        mode = "SMTP"
    elif WEBHOOK_URL:
        mode = "webhook de Zapier"
    else:
        mode = "MODO DEMO: los correos se imprimen aquí y NO se envían"
    print("\n=== PeerFlow iniciado ===", flush=True)
    print(f"  Correos:           {mode}", flush=True)
    print(f"  Correo del editor: {EDITOR_EMAIL}", flush=True)
    print(f"  Límite de páginas: {PAGE_LIMIT}\n", flush=True)


if __name__ == "__main__":
    startup_report()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
