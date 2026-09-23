import imaplib
import email
import re
import os
import sqlite3
import asyncio
import threading
import time
import base64

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from email.header import decode_header

import qrcode

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel


# ============================================================
# CONFIGURATION
# ============================================================

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

UPI_ID = os.getenv("UPI_ID", "beniwalgaurav@fam")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Gaurav Beniwal")

DB_PATH = os.getenv("DB_PATH", "./payments.db")

# How many days of email history should be cached
SEARCH_DAYS = int(os.getenv("SEARCH_DAYS", "3"))

# Background Gmail check interval
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "3"))

# Keep only this many days in temporary DB
CACHE_DAYS = SEARCH_DAYS

if not GMAIL_USER or not GMAIL_APP_PASSWORD:
    raise RuntimeError(
        "Missing GMAIL_USER or GMAIL_APP_PASSWORD environment variables"
    )


# ============================================================
# GLOBAL COLLECTOR STATE
# ============================================================

collector_running = False
collector_started_at = None
collector_last_check = None
collector_last_success = None
collector_last_error = None
collector_last_uid = 0
collector_total_scanned = 0
collector_total_saved = 0
collector_total_duplicates = 0

collector_thread = None
collector_stop_event = threading.Event()

state_lock = threading.Lock()


# ============================================================
# TIME HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def parse_email_date(date_string):
    """
    Convert email Date header into ISO timestamp where possible.
    """
    if not date_string:
        return iso_now()

    try:
        dt = email.utils.parsedate_to_datetime(date_string)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc).isoformat()

    except Exception:
        return iso_now()


# ============================================================
# DATABASE
# ============================================================

def init_db():

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    # --------------------------------------------------------
    # Orders
    # --------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            note TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',
            utr TEXT,
            sender_name TEXT,
            created_at TEXT NOT NULL,
            verified_at TEXT
        )
    """)

    # --------------------------------------------------------
    # Cached payment emails
    # --------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            email_uid INTEGER UNIQUE,

            message_id TEXT,

            utr TEXT,

            amount REAL,

            sender_name TEXT,

            note TEXT,

            subject TEXT,

            sender_email TEXT,

            received_at TEXT,

            cached_at TEXT NOT NULL
        )
    """)

    # --------------------------------------------------------
    # Collector state
    # --------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS collector_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # --------------------------------------------------------
    # Indexes
    # --------------------------------------------------------

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_utr
        ON payments(utr)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_amount
        ON payments(amount)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_received
        ON payments(received_at)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_message_id
        ON payments(message_id)
    """)

    conn.commit()
    conn.close()


init_db()


@contextmanager
def get_db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    try:
        yield conn
    finally:
        conn.close()


# ============================================================
# DATABASE STATE
# ============================================================

def get_state(key, default=None):

    with get_db() as conn:

        row = conn.execute(
            """
            SELECT value
            FROM collector_state
            WHERE key = ?
            """,
            (key,)
        ).fetchone()

        if not row:
            return default

        return row["value"]


def set_state(key, value):

    with get_db() as conn:

        conn.execute(
            """
            INSERT INTO collector_state(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (key, str(value))
        )

        conn.commit()


# ============================================================
# ORDER DATABASE
# ============================================================

def _store_order(order_id, amount, note):

    with get_db() as conn:

        conn.execute(
            """
            INSERT OR REPLACE INTO orders
            (
                order_id,
                amount,
                note,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'PENDING', ?)
            """,
            (
                order_id,
                amount,
                note,
                iso_now()
            )
        )

        conn.commit()


# ============================================================
# GMAIL PAYMENT COLLECTOR
# ============================================================

class FamPayEmailCollector:

    def __init__(self, gmail_user, app_password):

        self.gmail_user = gmail_user
        self.app_password = app_password

        self.mail = None

    # --------------------------------------------------------
    # Connect
    # --------------------------------------------------------

    def connect(self):

        self.disconnect()

        mail = imaplib.IMAP4_SSL(
            "imap.gmail.com",
            993
        )

        mail.login(
            self.gmail_user,
            self.app_password
        )

        status, _ = mail.select("INBOX")

        if status != "OK":
            try:
                mail.logout()
            except Exception:
                pass

            raise RuntimeError("Could not select Gmail inbox")

        self.mail = mail

        return mail

    # --------------------------------------------------------
    # Disconnect
    # --------------------------------------------------------

    def disconnect(self):

        if self.mail:

            try:
                self.mail.close()
            except Exception:
                pass

            try:
                self.mail.logout()
            except Exception:
                pass

        self.mail = None

    # --------------------------------------------------------
    # Gmail connection health
    # --------------------------------------------------------

    def ensure_connection(self):

        if self.mail is None:
            self.connect()
            return

        try:

            status, _ = self.mail.noop()

            if status != "OK":
                self.connect()

        except Exception:

            self.connect()

    # --------------------------------------------------------
    # Decode MIME header
    # --------------------------------------------------------

    def decode_header_value(self, value):

        if not value:
            return ""

        try:

            parts = decode_header(value)

            result = ""

            for part, encoding in parts:

                if isinstance(part, bytes):

                    result += part.decode(
                        encoding or "utf-8",
                        errors="ignore"
                    )

                else:

                    result += str(part)

            return result

        except Exception:

            return str(value)

    # --------------------------------------------------------
    # Extract email body
    # --------------------------------------------------------

    def extract_body(self, msg):

        text_parts = []
        html_parts = []

        if msg.is_multipart():

            for part in msg.walk():

                content_type = (
                    part.get_content_type() or ""
                ).lower()

                disposition = str(
                    part.get("Content-Disposition") or ""
                ).lower()

                if "attachment" in disposition:
                    continue

                try:

                    payload = part.get_payload(
                        decode=True
                    )

                    if not payload:
                        continue

                    charset = (
                        part.get_content_charset()
                        or "utf-8"
                    )

                    decoded = payload.decode(
                        charset,
                        errors="ignore"
                    )

                    if content_type == "text/plain":
                        text_parts.append(decoded)

                    elif content_type == "text/html":
                        html_parts.append(decoded)

                except Exception:
                    continue

        else:

            try:

                payload = msg.get_payload(
                    decode=True
                )

                if payload:

                    charset = (
                        msg.get_content_charset()
                        or "utf-8"
                    )

                    decoded = payload.decode(
                        charset,
                        errors="ignore"
                    )

                    if msg.get_content_type() == "text/html":
                        html_parts.append(decoded)
                    else:
                        text_parts.append(decoded)

            except Exception:
                pass

        if text_parts:
            return "\n".join(text_parts)

        if html_parts:

            html = "\n".join(html_parts)

            html = re.sub(
                r"<br\s*/?>",
                "\n",
                html,
                flags=re.IGNORECASE
            )

            html = re.sub(
                r"</p\s*>",
                "\n",
                html,
                flags=re.IGNORECASE
            )

            html = re.sub(
                r"<[^>]+>",
                " ",
                html
            )

            html = re.sub(
                r"\s+",
                " ",
                html
            )

            return html.strip()

        return ""

    # --------------------------------------------------------
    # Parse payment
    # --------------------------------------------------------

    def parse_payment(self, body):

        if not body:
            return {
                "amount": None,
                "utr": None,
                "sender_name": None,
                "note": None
            }

        # Normalize whitespace
        normalized = re.sub(
            r"[ \t]+",
            " ",
            body
        )

        # ----------------------------------------------------
        # Amount
        # ----------------------------------------------------

        amount = None

        amount_patterns = [

            r'(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)',

            r'([\d,]+(?:\.\d{1,2})?)\s*(?:Rs\.?|INR|₹)',

            r'(?:amount|paid|payment|received)[^0-9]{0,30}'
            r'([\d,]+(?:\.\d{1,2})?)'

        ]

        for pattern in amount_patterns:

            match = re.search(
                pattern,
                normalized,
                re.IGNORECASE
            )

            if match:

                try:

                    amount = float(
                        match.group(1).replace(",", "")
                    )

                    break

                except Exception:
                    pass

        # ----------------------------------------------------
        # UTR
        # ----------------------------------------------------

        utr = None

        utr_patterns = [

            r'(?:UTR|UPI\s*REF(?:ERENCE)?|REF(?:ERENCE)?'
            r'(?:\s*NO|\s*NUMBER)?|transaction\s*id)'
            r'[\s:#-]*(\d{8,20})',

            r'\b(\d{12})\b',

            r'\b(\d{16})\b'

        ]

        for pattern in utr_patterns:

            match = re.search(
                pattern,
                normalized,
                re.IGNORECASE
            )

            if match:

                utr = match.group(1)

                break

        # ----------------------------------------------------
        # Sender name
        # ----------------------------------------------------

        sender_name = None

        name_patterns = [

            r'(?:from|by|paid\s*by|received\s*from)'
            r'[\s:,-]+([A-Za-z][A-Za-z ._-]{1,50})',

            r'(?:sender|payer|customer)'
            r'[\s:,-]+([A-Za-z][A-Za-z ._-]{1,50})'

        ]

        for pattern in name_patterns:

            match = re.search(
                pattern,
                normalized,
                re.IGNORECASE
            )

            if match:

                candidate = match.group(1).strip()

                candidate = re.split(
                    r'\s+(?:via|on|using|through|for)\s+',
                    candidate,
                    flags=re.IGNORECASE
                )[0]

                sender_name = candidate[:80].strip()

                if sender_name:
                    break

        if not sender_name:
            sender_name = "Unknown"

        # ----------------------------------------------------
        # Note / reference
        # ----------------------------------------------------

        note = None

        note_patterns = [

            r'(?:note|remark|remarks)'
            r'[\s:=-]+([A-Za-z0-9_.#:/ -]{1,100})',

            r'(?:reference|ref)'
            r'[\s:=-]+([A-Za-z0-9_.#:/ -]{1,100})'

        ]

        for pattern in note_patterns:

            match = re.search(
                pattern,
                normalized,
                re.IGNORECASE
            )

            if match:

                note = match.group(1).strip()

                note = note[:100]

                break

        return {
            "amount": amount,
            "utr": utr,
            "sender_name": sender_name,
            "note": note
        }

    # --------------------------------------------------------
    # Check if email looks like payment email
    # --------------------------------------------------------

    def is_payment_email(
        self,
        sender,
        subject,
        body
    ):

        combined = (
            (sender or "") +
            " " +
            (subject or "") +
            " " +
            (body or "")
        ).lower()

        keywords = [

            "fam",

            "fampay",

            "payment",

            "upi",

            "paid",

            "received",

            "transaction",

            "credited"

        ]

        return any(
            keyword in combined
            for keyword in keywords
        )

    # --------------------------------------------------------
    # Save payment
    # --------------------------------------------------------

    def save_payment(
        self,
        email_uid,
        message_id,
        utr,
        amount,
        sender_name,
        note,
        subject,
        sender_email,
        received_at
    ):

        with get_db() as conn:

            # First check UID
            existing = conn.execute(
                """
                SELECT id
                FROM payments
                WHERE email_uid = ?
                LIMIT 1
                """,
                (email_uid,)
            ).fetchone()

            if existing:

                return False

            # Also prevent duplicate Message-ID
            if message_id:

                existing = conn.execute(
                    """
                    SELECT id
                    FROM payments
                    WHERE message_id = ?
                    LIMIT 1
                    """,
                    (message_id,)
                ).fetchone()

                if existing:

                    return False

            conn.execute(
                """
                INSERT INTO payments
                (
                    email_uid,
                    message_id,
                    utr,
                    amount,
                    sender_name,
                    note,
                    subject,
                    sender_email,
                    received_at,
                    cached_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email_uid,
                    message_id,
                    utr,
                    amount,
                    sender_name,
                    note,
                    subject,
                    sender_email,
                    received_at,
                    iso_now()
                )
            )

            conn.commit()

            return True

    # --------------------------------------------------------
    # Process one email
    # --------------------------------------------------------

    def process_email(self, email_uid):

        try:

            status, msg_data = self.mail.fetch(
                str(email_uid),
                "(RFC822)"
            )

            if status != "OK":
                return False

            raw_message = None

            for item in msg_data:

                if isinstance(item, tuple):

                    raw_message = item[1]

                    break

            if not raw_message:
                return False

            msg = email.message_from_bytes(
                raw_message
            )

            sender = (
                msg.get("From") or ""
            )

            sender_decoded = self.decode_header_value(
                sender
            )

            subject = self.decode_header_value(
                msg.get("Subject") or ""
            )

            body = self.extract_body(msg)

            # Ignore unrelated emails
            if not self.is_payment_email(
                sender_decoded,
                subject,
                body
            ):
                return False

            parsed = self.parse_payment(body)

            # Must contain useful payment information
            if (
                not parsed["utr"]
                and parsed["amount"] is None
            ):
                return False

            message_id = (
                msg.get("Message-ID") or ""
            ).strip()

            received_at = parse_email_date(
                msg.get("Date")
            )

            saved = self.save_payment(
                email_uid=email_uid,
                message_id=message_id,
                utr=parsed["utr"],
                amount=parsed["amount"],
                sender_name=parsed["sender_name"],
                note=parsed["note"],
                subject=subject,
                sender_email=sender_decoded,
                received_at=received_at
            )

            return saved

        except Exception as e:

            print(
                f"[Collector] Email UID {email_uid} "
                f"processing error: {e}"
            )

            return False

    # --------------------------------------------------------
    # Initial 3-day backfill
    # --------------------------------------------------------

    def initial_backfill(self):

        global collector_last_uid
        global collector_total_scanned
        global collector_total_saved
        global collector_total_duplicates

        print(
            f"[Collector] Starting {SEARCH_DAYS}-day backfill..."
        )

        self.ensure_connection()

        since_date = (
            datetime.now() -
            timedelta(days=SEARCH_DAYS)
        ).strftime("%d-%b-%Y")

        status, data = self.mail.search(
            None,
            f'(SINCE "{since_date}")'
        )

        if status != "OK":

            raise RuntimeError(
                "Gmail initial search failed"
            )

        email_ids = data[0].split()

        print(
            f"[Collector] Found "
            f"{len(email_ids)} emails in history"
        )

        highest_uid = 0

        # Process oldest → newest
        for raw_uid in email_ids:

            try:

                uid = int(raw_uid)

            except Exception:
                continue

            highest_uid = max(
                highest_uid,
                uid
            )

            before_count = self.get_payment_count()

            saved = self.process_email(uid)

            after_count = self.get_payment_count()

            collector_total_scanned += 1

            if saved:
                collector_total_saved += 1

            elif after_count == before_count:
                # Not necessarily duplicate; it may simply
                # be an unrelated email.
                pass

        if highest_uid:

            collector_last_uid = highest_uid

            set_state(
                "last_uid",
                highest_uid
            )

        print(
            f"[Collector] Initial backfill complete. "
            f"Cached payments: {self.get_payment_count()}"
        )

    # --------------------------------------------------------
    # Get cached payment count
    # --------------------------------------------------------

    def get_payment_count(self):

        with get_db() as conn:

            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM payments
                """
            ).fetchone()

            return int(row["count"])

    # --------------------------------------------------------
    # Incremental scan
    # --------------------------------------------------------

    def fetch_new_emails(self):

        global collector_last_uid
        global collector_total_scanned
        global collector_total_saved

        self.ensure_connection()

        saved_uid = get_state(
            "last_uid",
            "0"
        )

        try:
            last_uid = int(saved_uid)
        except Exception:
            last_uid = 0

        # Search only UID after our checkpoint
        status, data = self.mail.uid(
            "search",
            None,
            f"UID {last_uid + 1}:*"
        )

        if status != "OK":

            raise RuntimeError(
                "Gmail incremental UID search failed"
            )

        uid_list = data[0].split()

        if not uid_list:
            return 0

        saved_count = 0

        highest_uid = last_uid

        for raw_uid in uid_list:

            try:

                uid = int(raw_uid)

            except Exception:

                continue

            highest_uid = max(
                highest_uid,
                uid
            )

            # UID FETCH requires UID command
            status, msg_data = self.mail.uid(
                "fetch",
                str(uid),
                "(RFC822)"
            )

            if status != "OK":
                continue

            raw_message = None

            for item in msg_data:

                if isinstance(item, tuple):

                    raw_message = item[1]

                    break

            if not raw_message:
                continue

            try:

                msg = email.message_from_bytes(
                    raw_message
                )

                sender = (
                    msg.get("From") or ""
                )

                sender_decoded = self.decode_header_value(
                    sender
                )

                subject = self.decode_header_value(
                    msg.get("Subject") or ""
                )

                body = self.extract_body(msg)

                if not self.is_payment_email(
                    sender_decoded,
                    subject,
                    body
                ):
                    continue

                parsed = self.parse_payment(body)

                if (
                    not parsed["utr"]
                    and parsed["amount"] is None
                ):
                    continue

                message_id = (
                    msg.get("Message-ID") or ""
                ).strip()

                received_at = parse_email_date(
                    msg.get("Date")
                )

                saved = self.save_payment(
                    email_uid=uid,
                    message_id=message_id,
                    utr=parsed["utr"],
                    amount=parsed["amount"],
                    sender_name=parsed["sender_name"],
                    note=parsed["note"],
                    subject=subject,
                    sender_email=sender_decoded,
                    received_at=received_at
                )

                collector_total_scanned += 1

                if saved:

                    collector_total_saved += 1
                    saved_count += 1

            except Exception as e:

                print(
                    f"[Collector] New email parse error: {e}"
                )

        if highest_uid > last_uid:

            collector_last_uid = highest_uid

            set_state(
                "last_uid",
                highest_uid
            )

        return saved_count

    # --------------------------------------------------------
    # Cleanup old temporary payments
    # --------------------------------------------------------

    def cleanup_old_payments(self):

        cutoff = (
            utc_now() -
            timedelta(days=CACHE_DAYS)
        ).isoformat()

        with get_db() as conn:

            cursor = conn.execute(
                """
                DELETE FROM payments
                WHERE received_at < ?
                """,
                (cutoff,)
            )

            deleted = cursor.rowcount

            conn.commit()

        if deleted:

            print(
                f"[Collector] Removed "
                f"{deleted} expired cached payments"
            )

        return deleted

    # --------------------------------------------------------
    # One collector cycle
    # --------------------------------------------------------

    def run_cycle(self):

        self.fetch_new_emails()

        self.cleanup_old_payments()


# ============================================================
# BACKGROUND COLLECTOR
# ============================================================

collector = FamPayEmailCollector(
    GMAIL_USER,
    GMAIL_APP_PASSWORD
)


def collector_worker():

    global collector_running
    global collector_started_at
    global collector_last_check
    global collector_last_success
    global collector_last_error

    collector_running = True
    collector_started_at = iso_now()

    print("=" * 60)
    print("FAMPAY BACKGROUND PAYMENT COLLECTOR")
    print("=" * 60)

    try:

            # ----------------------------------------------------
        # STEP 1
        # Initial 3-day scan
        # ----------------------------------------------------

        collector.initial_backfill()

        # ----------------------------------------------------
        # STEP 2
        # Continuous incremental monitoring
        # ----------------------------------------------------

        print(
            f"[Collector] Monitoring Gmail every "
            f"{POLL_INTERVAL} seconds..."
        )

        while not collector_stop_event.is_set():

            cycle_start = time.time()

            try:

                collector.run_cycle()

                collector_last_success = iso_now()
                collector_last_error = None

                collector_last_check = iso_now()

            except Exception as e:

                collector_last_error = str(e)
                collector_last_check = iso_now()

                print(
                    "[Collector] Cycle error:",
                    str(e)
                )

                # Force reconnect on next cycle
                try:
                    collector.disconnect()
                except Exception:
                    pass

            elapsed = time.time() - cycle_start

            sleep_for = max(
                0,
                POLL_INTERVAL - elapsed
            )

            collector_stop_event.wait(
                sleep_for
            )

    except Exception as e:

        collector_last_error = str(e)

        print(
            "[Collector] Fatal error:",
            str(e)
        )

    finally:

        collector_running = False

        try:
            collector.disconnect()
        except Exception:
            pass

        print(
            "[Collector] Worker stopped"
        )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="FamPay UPI Verification System",
    version="2.0"
)


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

@app.on_event("startup")
async def startup_event():

    global collector_thread

    collector_stop_event.clear()

    collector_thread = threading.Thread(
        target=collector_worker,
        daemon=True,
        name="FamPayPaymentCollector"
    )

    collector_thread.start()

    print(
        "[System] Background payment collector started"
    )


@app.on_event("shutdown")
async def shutdown_event():

    collector_stop_event.set()

    try:
        collector.disconnect()
    except Exception:
        pass

    print(
        "[System] Background collector shutdown requested"
    )


# ============================================================
# REQUEST MODELS
# ============================================================

class CreateOrderRequest(BaseModel):

    amount: float
    note: str


class VerifyRequest(BaseModel):

    order_id: str
    utr: str


class VerifyByAmountRequest(BaseModel):

    order_id: str


# ============================================================
# QR / ORDER HELPERS
# ============================================================

def _make_upi_url(amount, note):

    return (
        f"upi://pay?"
        f"pa={UPI_ID}"
        f"&pn={PAYEE_NAME}"
        f"&am={amount:.2f}"
        f"&cu=INR"
        f"&tn={note}"
    )


def _make_order_id(note):

    safe_note = re.sub(
        r"[^A-Za-z0-9_-]",
        "",
        str(note)
    )

    return (
        f"ORD"
        f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        f"{safe_note[:30]}"
    )


def _make_qr_png(upi_url):

    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4
    )

    qr.add_data(upi_url)

    qr.make(fit=True)

    img = qr.make_image(
        fill_color="black",
        back_color="white"
    )

    from io import BytesIO

    buf = BytesIO()

    img.save(
        buf,
        format="PNG"
    )

    return buf.getvalue()


# ============================================================
# HOME PAGE
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def home():

    return """
    <!DOCTYPE html>
    <html>

    <head>

        <meta charset="UTF-8">

        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <title>FamPay UPI Verification System</title>

        <style>

            body {
                font-family:
                    system-ui,
                    -apple-system,
                    BlinkMacSystemFont,
                    sans-serif;

                max-width: 1100px;

                margin: 0 auto;

                padding: 25px;

                background: #f5f7fa;

                color: #222;
            }

            h1 {
                margin-bottom: 5px;
            }

            .subtitle {
                color: #666;
                margin-bottom: 25px;
            }

            .grid {
                display: grid;
                grid-template-columns:
                    repeat(auto-fit, minmax(280px, 1fr));

                gap: 15px;
            }

            .card {
                background: white;
                padding: 20px;
                border-radius: 14px;

                box-shadow:
                    0 3px 15px
                    rgba(0,0,0,.06);
            }

            .card h2 {
                margin-top: 0;
                font-size: 18px;
            }

            a {
                display: block;
                padding: 11px;
                margin-top: 10px;

                background: #f1f5ff;

                border-radius: 8px;

                color: #1457d9;

                text-decoration: none;

                word-break: break-all;
            }

            a:hover {
                background: #e7edff;
            }

            .status {
                padding: 15px;

                background: #ecfdf3;

                border: 1px solid #bbf7d0;

                border-radius: 12px;

                margin-bottom: 20px;
            }

        </style>

    </head>

    <body>

        <h1>FamPay UPI Verification System</h1>

        <div class="subtitle">
            Background Gmail collector + temporary payment cache
        </div>

        <div class="status">

            <strong>Architecture:</strong>

            Gmail → Background Collector →
            Temporary SQLite DB → Instant API Lookup

        </div>

        <div class="grid">

            <div class="card">

                <h2>Health</h2>

                <a href="/health">
                    /health
                </a>

            </div>

            <div class="card">

                <h2>Collector Status</h2>

                <a href="/collector/status">
                    /collector/status
                </a>

            </div>

            <div class="card">

                <h2>Cached Payments</h2>

                <a href="/payments">
                    /payments
                </a>

            </div>

            <div class="card">

                <h2>Payments Page</h2>

                <a href="/payments-page">
                    /payments-page
                </a>

            </div>

            <div class="card">

                <h2>Orders</h2>

                <a href="/orders">
                    /orders
                </a>

            </div>

            <div class="card">

                <h2>Swagger</h2>

                <a href="/docs">
                    /docs
                </a>

            </div>

            <div class="card">

                <h2>ReDoc</h2>

                <a href="/redoc">
                    /redoc
                </a>

            </div>

        </div>

    </body>

    </html>
    """

# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    with get_db() as conn:

        row = conn.execute(
            "SELECT COUNT(*) AS count FROM payments"
        ).fetchone()

        payment_count = row["count"]

    return {
        "status": "ok",
        "service": "fam-pay-verifier",
        "collector_running": collector_running,
        "cached_payments": payment_count,
        "cache_days": CACHE_DAYS,
        "poll_interval_seconds": POLL_INTERVAL
    }


# ============================================================
# COLLECTOR STATUS
# ============================================================

@app.get("/collector/status")
async def collector_status():

    with get_db() as conn:

        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM payments
            """
        ).fetchone()

        payment_count = row["count"]

    return {

        "running": collector_running,

        "started_at": collector_started_at,

        "last_check": collector_last_check,

        "last_success": collector_last_success,

        "last_error": collector_last_error,

        "last_uid": collector_last_uid,

        "total_scanned": collector_total_scanned,

        "total_saved": collector_total_saved,

        "cached_payments": payment_count,

        "cache_days": CACHE_DAYS,

        "poll_interval_seconds": POLL_INTERVAL

    }


# ============================================================
# CREATE ORDER
# ============================================================

@app.post("/create-order")
async def create_order(
    req: CreateOrderRequest
):

    order_id = _make_order_id(
        req.note
    )

    upi_url = _make_upi_url(
        req.amount,
        req.note
    )

    qr_bytes = _make_qr_png(
        upi_url
    )

    qr_b64 = base64.b64encode(
        qr_bytes
    ).decode()

    _store_order(
        order_id,
        req.amount,
        req.note
    )

    return {

        "order_id": order_id,

        "amount": req.amount,

        "note": req.note,

        "upi_url": upi_url,

        "qr_image_base64": qr_b64

    }


@app.get("/create-order-get")
async def create_order_get(
    amount: float,
    note: str
):

    return await create_order(
        CreateOrderRequest(
            amount=amount,
            note=note
        )
    )


@app.get("/create-order-qr")
async def create_order_qr(
    amount: float,
    note: str
):

    order_id = _make_order_id(
        note
    )

    upi_url = _make_upi_url(
        amount,
        note
    )

    qr_bytes = _make_qr_png(
        upi_url
    )

    _store_order(
        order_id,
        amount,
        note
    )

    return Response(
        content=qr_bytes,
        media_type="image/png"
    )


# ============================================================
# VERIFY UTR — DATABASE ONLY
# ============================================================

@app.get("/verify-utr-only")
async def verify_utr_only(
    utr: str
):

    with get_db() as conn:

        payment = conn.execute(
            """
            SELECT
                utr,
                amount,
                sender_name,
                note,
                received_at,
                subject
            FROM payments

            WHERE utr = ?

            ORDER BY received_at DESC

            LIMIT 1
            """,
            (utr,)
        ).fetchone()

    if not payment:

        return {

            "found": False,

            "utr": utr,

            "message":
                "Payment not found in temporary cache"

        }

    return {

        "found": True,

        "utr": payment["utr"],

        "amount": payment["amount"],

        "sender_name":
            payment["sender_name"],

        "note":
            payment["note"],

        "received_at":
            payment["received_at"],

        "subject":
            payment["subject"]

    }


# ============================================================
# VERIFY AMOUNT — DATABASE ONLY
# ============================================================

@app.get("/verify-amount-only")
async def verify_amount_only(
    amount: float
):

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT
                utr,
                amount,
                sender_name,
                note,
                received_at,
                subject,
                sender_email
            FROM payments

            WHERE amount >= ?
              AND amount <= ?

            ORDER BY received_at DESC
            """,
            (
                amount - 0.01,
                amount + 0.01
            )
        ).fetchall()

    matches = [
        dict(row)
        for row in rows
    ]

    if matches:

        return {

            "found": True,

            "amount": amount,

            "match_count":
                len(matches),

            "matches":
                matches

        }

    return {

        "found": False,

        "amount": amount,

        "match_count": 0,

        "matches": [],

        "message":
            "No matching payment in temporary cache"

    }


# ============================================================
# ALL CACHED PAYMENTS
# ============================================================

@app.get("/payments")
async def payments(
    days: int = SEARCH_DAYS
):

    days = min(
        max(days, 1),
        SEARCH_DAYS
    )

    cutoff = (
        utc_now() -
        timedelta(days=days)
    ).isoformat()

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT
                id,
                email_uid,
                message_id,
                utr,
                amount,
                sender_name,
                note,
                subject,
                sender_email,
                received_at,
                cached_at

            FROM payments

            WHERE received_at >= ?

            ORDER BY received_at DESC
            """,
            (cutoff,)
        ).fetchall()

    return {

        "source": "temporary_database",

        "days": days,

        "count": len(rows),

        "payments": [
            dict(row)
            for row in rows
        ]

    }


# ============================================================
# PAYMENTS HTML PAGE
# ============================================================

@app.get(
    "/payments-page",
    response_class=HTMLResponse
)
async def payments_page():

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT
                id,
                utr,
                amount,
                sender_name,
                note,
                subject,
                received_at
            FROM payments
            ORDER BY received_at DESC
            LIMIT 500
            """
        ).fetchall()

    html_rows = ""

    for row in rows:

        amount = (
            f"₹{row['amount']:.2f}"
            if row["amount"] is not None
            else "-"
        )

        utr = row["utr"] or "-"
        sender = row["sender_name"] or "-"
        note = row["note"] or "-"
        received = row["received_at"] or "-"

        html_rows += f"""
        <tr>

            <td>{amount}</td>

            <td>
                <code>{utr}</code>
            </td>

            <td>{sender}</td>

            <td>{note}</td>

            <td>{received}</td>

        </tr>
        """

    if not html_rows:

        html_rows = """
        <tr>
            <td colspan="5">
                No cached payments yet.
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>

    <html>

    <head>

        <meta charset="UTF-8">

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

        <meta
            http-equiv="refresh"
            content="5"
        >

        <title>FamPay Payments</title>

        <style>

            * {{
                box-sizing: border-box;
            }}

            body {{
                margin: 0;

                padding: 20px;

                font-family:
                    system-ui,
                    -apple-system,
                    BlinkMacSystemFont,
                    sans-serif;

                background: #f5f7fa;

                color: #111;
            }}

            .container {{
                max-width: 1200px;

                margin: auto;
            }}

            .header {{
                background: white;

                padding: 20px;

                border-radius: 15px;

                margin-bottom: 15px;

                box-shadow:
                    0 3px 15px
                    rgba(0,0,0,.06);
            }}

            .table-wrap {{
                background: white;

                border-radius: 15px;

                overflow: auto;

                box-shadow:
                    0 3px 15px
                    rgba(0,0,0,.06);
            }}

            table {{
                width: 100%;

                border-collapse: collapse;

                min-width: 800px;
            }}

            th {{
                text-align: left;

                background: #f1f3f5;

                padding: 13px;

                font-size: 13px;
            }}

            td {{
                padding: 13px;

                border-top:
                    1px solid #eee;

                font-size: 14px;
            }}

            code {{
                font-family: monospace;
            }}

            .live {{
                display: inline-block;

                padding: 5px 9px;

                background: #dcfce7;

                color: #166534;

                border-radius: 999px;

                font-size: 12px;
            }}

        </style>

    </head>

    <body>

        <div class="container">

            <div class="header">

                <h2>
                    FamPay Cached Payments
                </h2>

                <span class="live">
                    LIVE DATABASE
                </span>

                <p>
                    Temporary payment cache.
                    Automatically refreshed by
                    background Gmail collector.
                </p>

                <p>
                    Auto-refresh: 5 seconds
                </p>

            </div>

            <div class="table-wrap">

                <table>

                    <thead>

                        <tr>

                            <th>Amount</th>

                            <th>UTR</th>

                            <th>Sender</th>

                            <th>Note</th>

                            <th>Received</th>

                        </tr>

                    </thead>

                    <tbody>

                        {html_rows}

                    </tbody>

                </table>

            </div>

        </div>

    </body>

    </html>
    """

# ============================================================
# VERIFY BY UTR + ORDER
# DATABASE ONLY
# ============================================================

@app.post("/verify-by-utr")
async def verify_by_utr(
    req: VerifyRequest
):

    order_info = None

    with get_db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id = ?
            """,
            (req.order_id,)
        ).fetchone()

        if order:

            order_info = dict(order)

        payment = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE utr = ?
            ORDER BY received_at DESC
            LIMIT 1
            """,
            (req.utr,)
        ).fetchone()

    if not payment:

        return {

            "found": False,

            "utr": req.utr,

            "order": order_info,

            "message":
                "Payment not found in temporary cache"

        }

    return {

        "found": True,

        "utr": req.utr,

        "amount": payment["amount"],

        "sender_name":
            payment["sender_name"],

        "note_in_email":
            payment["note"],

        "received_at":
            payment["received_at"],

        "order":
            order_info

    }


@app.get("/verify-by-utr-get")
async def verify_by_utr_get(
    order_id: str,
    utr: str
):

    return await verify_by_utr(
        VerifyRequest(
            order_id=order_id,
            utr=utr
        )
    )


# ============================================================
# VERIFY BY ORDER AMOUNT
# DATABASE ONLY
# ============================================================

@app.post("/verify-by-amount")
async def verify_by_amount(
    req: VerifyByAmountRequest
):

    with get_db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id = ?
            """,
            (req.order_id,)
        ).fetchone()

        if not order:

            return {

                "found": False,

                "order_id":
                    req.order_id,

                "message":
                    "Order not found",

                "matches": []

            }

        order_info = dict(order)

        rows = conn.execute(
            """
            SELECT
                utr,
                amount,
                sender_name,
                note,
                received_at,
                subject,
                sender_email
            FROM payments

            WHERE amount >= ?
              AND amount <= ?

            ORDER BY received_at DESC
            """,
            (
                float(order_info["amount"]) - 0.01,
                float(order_info["amount"]) + 0.01
            )
        ).fetchall()

    matches = [
        dict(row)
        for row in rows
    ]

    return {

        "found":
            bool(matches),

        "order":
            order_info,

        "match_count":
            len(matches),

        "matches":
            matches

    }


@app.get("/verify-by-amount-get")
async def verify_by_amount_get(
    order_id: str
):

    return await verify_by_amount(
        VerifyByAmountRequest(
            order_id=order_id
        )
    )


# ============================================================
# CHECK ORDER
# ============================================================

@app.get("/order/{order_id}")
async def get_order(
    order_id: str
):

    with get_db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id = ?
            """,
            (order_id,)
        ).fetchone()

    if not order:

        raise HTTPException(
            status_code=404,
            detail="Order not found"
        )

    return dict(order)


# ============================================================
# LIST ORDERS
# ============================================================

@app.get("/orders")
async def list_orders():

    with get_db() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM orders
            ORDER BY created_at DESC
            LIMIT 200
            """
        ).fetchall()

    return {

        "count":
            len(rows),

        "orders": [
            dict(row)
            for row in rows
        ]

    }


# ============================================================
# MANUAL COLLECTOR REFRESH
# ============================================================

@app.post("/collector/refresh")
async def collector_refresh():

    try:

        # This does NOT scan the entire 3-day history.
        # It performs an incremental Gmail check.

        saved = await asyncio.to_thread(
            collector.run_cycle
        )

        return {

            "success": True,

            "new_payments_saved":
                saved,

            "cached_payments":
                collector.get_payment_count()

        }

    except Exception as e:

        return {

            "success": False,

            "error": str(e)

        }


# ============================================================
# RUN DIRECTLY
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
        )
