import os
import time
import json
import re
import threading
import datetime
import requests
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from src.tools.pdf_export import generate_notes_pdf
from apscheduler.triggers.cron import CronTrigger

# --- Load environment ---
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))

# --- Local imports ---
from src.db import (
    get_conn,
    create_task,
    init_db,
    create_note,
    list_notes,
    delete_note,
    pin_note,
    unpin_note,
)
from src.tools.messaging import send_message
from src.tools import orders
from src.planner import call_ollama, extract_json_from_text
from src.tools import gmail_oauth

# --- Init DB and Scheduler ---
init_db()
TZ = pytz.timezone("Asia/Kolkata")
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.start()

# --- Environment and Telegram setup ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN missing in .env")

TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET_FILE = os.path.join(os.path.dirname(__file__), ".tg_offset")


# ---------------------------------------------------
# Helper Utilities
# ---------------------------------------------------
def normalize_rrule(rr):
    """Fixes common LLM or user-generated RRULE typos."""
    if not rr:
        return rr
    rr = rr.strip()
    rr = rr.replace("FREQ=MINUTE", "FREQ=MINUTELY")
    rr = rr.replace("FREQ=MINUTES", "FREQ=MINUTELY")
    rr = rr.replace("FREQ=HOUR", "FREQ=HOURLY")
    rr = rr.replace("FREQ=HOURS", "FREQ=HOURLY")
    rr = rr.replace("FREQ=DAY", "FREQ=DAILY")
    rr = rr.replace("FREQ=DAYS", "FREQ=DAILY")
    rr = rr.replace("EVERYDAY", "")
    rr = re.sub(r";?UNTIL=[^;]+", "", rr)
    if not rr.startswith("RRULE:"):
        rr = "RRULE:" + rr
    return rr


def load_offset():
    try:
        with open(OFFSET_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def save_offset(offset):
    try:
        with open(OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except Exception:
        pass


def register_user(chat_id, name, username):
    """Auto-register/update a user."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT UNIQUE,
            name TEXT,
            username TEXT,
            last_seen TEXT
        )
    """
    )
    conn.commit()
    now = datetime.datetime.now(TZ).isoformat()
    cur.execute("""
        INSERT OR REPLACE INTO user_registry (chat_id, name, username, last_seen)
        VALUES (?, ?, ?, ?)
    """,
        (str(chat_id), name, username, now),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------
# RRULE Parsing + Scheduling
# ---------------------------------------------------
def parse_rrule_to_interval_kwargs(rrule_str: str):
    """Parses iCalendar RRULE strings and returns Interval or Cron triggers."""
    if not rrule_str or not rrule_str.startswith("RRULE:"):
        return None
    try:
        parts = {}
        rule = rrule_str.replace("RRULE:", "")
        for kv in rule.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                parts[k.strip().upper()] = v.strip().upper()

        freq = parts.get("FREQ", "MINUTELY")
        interval = int(parts.get("INTERVAL", 1))
        byhour = parts.get("BYHOUR")
        byminute = parts.get("BYMINUTE")
        byday = parts.get("BYDAY")

        # Specific time → CronTrigger
        if byhour or byminute or byday:
            hour = int(byhour) if byhour else 9
            minute = int(byminute) if byminute else 0
            day_of_week = byday if byday else "*"
            cron = CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=TZ)
            print(f"🗓️ CronTrigger parsed: {day_of_week} at {hour}:{minute}")
            return cron

        # Interval → simple repetition
        mapping = {
            "SECONDLY": {"seconds": interval},
            "MINUTELY": {"minutes": interval},
            "HOURLY": {"hours": interval},
            "DAILY": {"days": interval},
            "WEEKLY": {"weeks": interval},
        }
        print(f"⏱️ IntervalTrigger parsed: every {interval} {freq.lower()[:-2]}")
        return mapping.get(freq)
    except Exception as e:
        print(f"⚠️ parse_rrule_to_interval_kwargs error: {e}")
        return None


def schedule_job_for_task(task_id: int, params: dict, schedule_rule: str):
    """Schedules a job with APScheduler and MCP execution."""
    job_id = f"reminder-{task_id}"
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)

    def _run_plan(p=params):
        from src.mcp import run_call
        try:
            for call in p.get("calls", []):
                print(f"⚙️ Scheduler dispatching via MCP: {call}")
                run_call(call)
        except Exception as e:
            print(f"⚠️ _run_plan error: {e}")

    print(f"🧩 Scheduling rule parsing: {schedule_rule}")
    try:
        # One-time rule
        if "FREQ=ONCE" in schedule_rule:
            run_dt = datetime.datetime.now(TZ) + datetime.timedelta(seconds=60)
            scheduler.add_job(_run_plan, trigger=DateTrigger(run_date=run_dt), id=job_id, replace_existing=True)
            return True

        kw = parse_rrule_to_interval_kwargs(schedule_rule)
        if isinstance(kw, CronTrigger):
            scheduler.add_job(_run_plan, trigger=kw, id=job_id, replace_existing=True)
            print(f"✅ Job scheduled (CronTrigger)")
            return True
        elif kw:
            trig = IntervalTrigger(timezone=TZ, **kw)
            scheduler.add_job(_run_plan, trigger=trig, id=job_id, replace_existing=True)
            print(f"✅ Job scheduled (IntervalTrigger)")
            return True
        else:
            run_dt = datetime.datetime.now(TZ) + datetime.timedelta(seconds=60)
            scheduler.add_job(_run_plan, trigger=DateTrigger(run_date=run_dt), id=job_id, replace_existing=True)
            print(f"⚙️ Fallback job scheduled in 60s")
            return True
    except Exception as e:
        print(f"⚠️ schedule_job_for_task error: {e}")
        return False


def persist_task_and_schedule(user_chat_id: str, plan_obj: dict):
    """Save the task to DB and schedule."""
    internal = {
        "plan": plan_obj.get("task_type", "reminder"),
        "calls": [{
            "tool": "messaging.send_message" if plan_obj.get("task_type") != "order" else "orders.place_order",
            "args": {}
        }]
    }

    if plan_obj.get("task_type") == "order":
        internal["calls"][0]["args"] = {
            "buyer_chat_id": str(user_chat_id),
            "store_identifier": plan_obj.get("extra", {}).get("store") or plan_obj.get("store") or plan_obj.get("store_name") or "",
            "item": plan_obj.get("extra", {}).get("item") or plan_obj.get("item") or plan_obj.get("text") or ""
        }

    else:
        internal["calls"][0]["args"] = {
            "chat_id": str(user_chat_id),
            "text": plan_obj.get("text", "Reminder"),
        }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM user_registry WHERE chat_id=?", (str(user_chat_id),))
    r = cur.fetchone()
    user_id = r[0] if r else 1
    conn.close()

    try:
        tid = create_task(user_id, plan_obj.get("task_type", "reminder"), internal,
                          plan_obj.get("schedule_rule", "RRULE:FREQ=MINUTELY;INTERVAL=1"), 1)
    except Exception:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO task (user_id, task_type, params_json, schedule_rule, enabled) VALUES (?, ?, ?, ?, ?)",
            (user_id, plan_obj.get("task_type", "reminder"), json.dumps(internal),
             plan_obj.get("schedule_rule", "RRULE:FREQ=MINUTELY;INTERVAL=1"), 1)
        )
        conn.commit()
        tid = cur.lastrowid
        conn.close()

    rule = normalize_rrule(plan_obj.get("schedule_rule", ""))
    scheduled = schedule_job_for_task(tid, internal, rule)
    if scheduled:
        send_message(user_chat_id, f"✅ Reminder scheduled and active (task id={tid}).")
    return tid if scheduled else None
def schedule_place_order(delay_seconds, buyer_chat_id, store_identifier, item):
    """
    Schedule a one-time order placement after a given delay (in seconds).
    This runs in a background thread (non-persistent).
    """
    def job():
        try:
            print(f"🕒 Placing scheduled order for '{item}' from '{store_identifier}' after {delay_seconds}s delay.")
            orders.place_order(str(buyer_chat_id), store_identifier, item)
        except Exception as e:
            print("⚠️ scheduled order failed:", e)

    try:
        delay_seconds = max(0, int(delay_seconds))
    except Exception:
        delay_seconds = 0

    t = threading.Timer(delay_seconds, job)
    t.daemon = True
    t.start()
    print(f"✅ Scheduled one-time order for '{item}' in {delay_seconds} seconds.")
    return t


def restore_saved_reminders_from_db():
    """Restore scheduled reminders from DB on startup."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, params_json, schedule_rule FROM task WHERE enabled=1")
        rows = cur.fetchall()
        conn.close()
        for tid, params_json, rule in rows:
            params = json.loads(params_json) if isinstance(params_json, str) else params_json
            schedule_job_for_task(tid, params, rule or "")
    except Exception as e:
        print("⚠️ Failed to restore reminders:", e)


restore_saved_reminders_from_db()

def process_message(msg):
    try:
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id"))
        username = chat.get("username")
        display_name = (
            chat.get("title")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or username
            or ""
        )
        text = msg.get("text") or msg.get("caption") or ""
        if not text:
            return

        # keep user registry logic unchanged
        register_user(chat_id, display_name, username)
        text_lower = text.strip().lower()

        # keep buyer <-> store chat forwarding logic unchanged
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS order_chat_session (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    buyer_chat_id TEXT,
                    store_chat_id TEXT,
                    active INTEGER DEFAULT 1
                )
            """
            )
            conn.commit()
            cur.execute(
                """
                SELECT id, order_id, buyer_chat_id, store_chat_id
                FROM order_chat_session
                WHERE active=1 AND (buyer_chat_id=? OR store_chat_id=?)
            """,
                (chat_id, chat_id),
            )
            sess = cur.fetchone()
            conn.close()
            if sess:
                sess_id, order_id, buyer_cid, store_cid = sess
                # endchat preserved
                if text.strip().lower() == "/endchat":
                    conn = get_conn()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE order_chat_session SET active=0 WHERE id=?",
                        (sess_id,),
                    )
                    conn.commit()
                    conn.close()
                    send_message(buyer_cid, "💬 Chat closed.")
                    send_message(store_cid, "💬 Chat closed.")
                    return
                target = store_cid if chat_id == buyer_cid else buyer_cid
                prefix = "👤 Customer" if chat_id == buyer_cid else "🏪 Store"
                send_message(target, f"{prefix}:\n{text}")
                return
        except Exception:
            pass

        # ---------------- NOTES FEATURE ----------------
        # /note <text>  -> create note
        if text_lower.startswith("/note "):
            parts = text.split(maxsplit=1)
            if len(parts) == 1 or not parts[1].strip():
                send_message(chat_id, "Usage: /note <your note text>")
                return
            note_text = parts[1].strip()
            try:
                nid = create_note(str(chat_id), note_text)
                send_message(chat_id, f"📝 Saved note #{nid}: {note_text}")
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to save note: {e}")
            return

        if text_lower.strip() == "/note":
            send_message(chat_id, "Usage: /note <your note text>")
            return

        # /notes -> list notes
        if text_lower.strip() == "/notes":
            try:
                rows = list_notes(str(chat_id))
                if not rows:
                    send_message(chat_id, "📭 You have no saved notes.")
                    return

                lines = ["🗒 *Your notes:*"]
                for nid, note_text, created_at, pinned in rows:
                    star = "⭐ " if pinned else ""
                    lines.append(f"{star}{nid}) {note_text}")

                lines.append("\nUse /pin_note <id> or /unpin_note <id> to manage pins.")
                send_message(chat_id, "\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to list notes: {e}")
            return


        # /delete_note <id>
        if text_lower.startswith("/delete_note"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1 or not parts[1].strip():
                send_message(chat_id, "Usage: /delete_note <note_id>")
                return
            arg = parts[1].strip()
            if not arg.isdigit():
                send_message(chat_id, "Note id must be a number.")
                return
            note_id = int(arg)
            try:
                ok = delete_note(str(chat_id), note_id)
                if ok:
                    send_message(chat_id, f"🗑 Deleted note #{note_id}.")
                else:
                    send_message(chat_id, f"⚠️ No note #{note_id} found.")
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to delete note: {e}")
            return
        # ---------------- END NOTES FEATURE ----------------

                # /export_notes -> generate and send PDF
        if text_lower.startswith("/export_notes"):
            try:
                notes = list_notes(str(chat_id))
                if not notes:
                    send_message(chat_id, "📭 You have no notes to export.")
                    return

                pdf_path = f"notes_{chat_id}.pdf"
                generate_notes_pdf(notes, pdf_path)

                # Send the PDF file
                files = {
                    "document": open(pdf_path, "rb")
                }
                data = {
                    "chat_id": chat_id,
                    "caption": "📄 Here is your exported notes PDF."
                }

                requests.post(f"{TG_BASE}/sendDocument", data=data, files=files)
                send_message(chat_id, "✅ Notes exported successfully!")

            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to export notes: {e}")

            return


        # ---------------- AGENDA FEATURE ----------------
        # /agenda -> show today's agenda (reminders + notes)
        if text_lower.startswith("/agenda"):
            # Get today's date in your bot's timezone
            now = datetime.datetime.now(TZ)
            date_str = now.strftime("%A, %d %b %Y")

            # 1) Fetch active reminders/orders from task table
            try:
                conn = get_conn()
                cur = conn.cursor()
                # Same approach as /list_reminders (no per-user filter yet)
                cur.execute(
                    "SELECT id, params_json, schedule_rule, enabled "
                    "FROM task WHERE enabled=1"
                )
                task_rows = cur.fetchall()
                conn.close()
            except Exception as e:
                task_rows = []
                print("⚠️ Failed to fetch tasks for agenda:", e)

            # Format reminders / tasks
            task_lines = []
            if task_rows:
                for tid, params_json, rule, enabled in task_rows:
                    try:
                        params = json.loads(params_json)
                        msg_text = params["calls"][0]["args"].get("text", "")
                        plan = params.get("plan", "")
                    except Exception:
                        msg_text = "(unreadable)"
                        plan = "unknown"
                    task_lines.append(f"• [{tid}] ({plan}) {msg_text}  ⏱ {rule}")
            else:
                task_lines.append("• No active reminders or scheduled tasks.")

            # 2) Fetch notes for this chat
            try:
                notes = list_notes(str(chat_id))
            except Exception as e:
                notes = []
                print("⚠️ Failed to fetch notes for agenda:", e)

            note_lines = []
            if notes:
                note_lines.append("Here are your latest notes:")
                # show at most 5
                for nid, note_text, created_at, pinned in notes[:5]:
                    star = "⭐ " if pinned else ""
                    note_lines.append(f"• {star}[{nid}] {note_text}")

                if len(notes) > 5:
                    note_lines.append(f"... and {len(notes) - 5} more. Use /notes to see all.")
            else:
                note_lines.append("You have no saved notes. Use `/note <text>` to add one.")

            # Build final agenda message
            lines = [
                f"📅 *Agenda for {date_str}*",
                "",
                "🕒 *Reminders & Scheduled Tasks*",
                *task_lines,
                "",
                "📝 *Notes*",
                *note_lines,
            ]

            send_message(chat_id, "\n".join(lines), parse_mode="Markdown")
            return
        # ---------------- END AGENDA FEATURE ----------------

        # --- /start (updated manual text) ---
        if text_lower.startswith("/start"):
            welcome_text = (
                "👋 Hello! I’m your *AI Micro Agent* — your smart assistant for reminders, "
                "orders, notes, and Gmail digests.\n\n"
                "Here’s what I can do:\n\n"
                "🕒 *Reminders*\n"
                "• `/remind drink water every 2 hours`\n"
                "• `/list_reminders` — show all reminders\n"
                "• `/delete_reminder <id>` — delete one\n\n"
                "🛒 *Orders*\n"
                "• `/remind order milk from Capital Store` — place an immediate order\n"
                "• `/remind order milk in 2 hours from Capital Store` — one-time delayed order\n"
                "• `/remind order milk every 2 days from Capital Store` — recurring order\n"
                "• Chat continues until `/endchat`\n\n"
                "📝 *Notes*\n"
                "• `/note buy fruits` — save a note\n"
                "• `/notes` — list your notes\n"
                "• `/delete_note <id>` — delete a note\n\n"
                "💌 *Email Digest*\n"
                "• `/link_gmail` — link Gmail\n"
                "• `/emailsummary` — fetch immediate summary\n"
                "• `/emailsummary 10` — fetch last 10 emails\n"
                "• `/emailsummary every day at 10am` — schedule daily digest\n"
                "• `/emailsummary weekly on Mon at 9am` — schedule weekly digest\n"
                "• `/disconnect_gmail` — unlink Gmail\n"
                "• `/check_gmail` — check Gmail link status\n\n"
                "🧾 *Jobs & Info*\n"
                "• `/list_jobs` — show scheduled jobs\n"
                "• `/whoami` — your profile\n"
                "• `/manual` — see this guide again\n\n"
                "Let’s get started! 🚀"
            )
            send_message(chat_id, welcome_text, parse_mode="Markdown")
            return
                # --- /systemcheck command ---
        if text_lower.startswith("/systemcheck"):
            from src.mcp import run_call
            import sqlite3

            send_message(chat_id, "🧠 Running system diagnostic... please wait ⏳")

            # Initialize result dictionary
            results = {
                "Messaging": "⚠️ Failed",
                "Email Summary": "⚠️ Failed",
                "Orders": "⚠️ Failed",
                "Scheduler": "⚠️ Not Running",
                "Database": "⚠️ Connection Failed"
            }

            # 1️⃣ Messaging test
            try:
                run_call({
                    "tool": "messaging.send_message",
                    "args": {"chat_id": chat_id, "text": "✅ Messaging test successful!"}
                })
                results["Messaging"] = "✅ OK"
            except Exception as e:
                results["Messaging"] = f"❌ {e}"

            # 2️⃣ Email summary test
            try:
                run_call({
                    "tool": "email.summary",
                    "args": {"chat_id": chat_id}
                })
                results["Email Summary"] = "✅ OK"
            except Exception as e:
                results["Email Summary"] = f"❌ {e}"

            # 3️⃣ Order system test (dry-run)
            try:
                if hasattr(orders, "place_order"):
                    results["Orders"] = "✅ OK (place_order available)"
                else:
                    results["Orders"] = "⚠️ No place_order function"
            except Exception as e:
                results["Orders"] = f"❌ {e}"

            # 4️⃣ Scheduler check
            try:
                if scheduler.running:
                    results["Scheduler"] = "✅ Active"
                else:
                    results["Scheduler"] = "⚠️ Not running"
            except Exception:
                results["Scheduler"] = "❌ Unknown state"

            # 5️⃣ Database connectivity
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT 1")
                conn.close()
                results["Database"] = "✅ Connected"
            except sqlite3.Error as e:
                results["Database"] = f"❌ {e}"

            # Format message for Telegram
            report = "🧠 *System Check Complete*\n\n"
            for key, val in results.items():
                report += f"{val} {key}\n"

            send_message(chat_id, report, parse_mode="Markdown")
            return
    
    
        # --- /whoami (kept) ---
        if text_lower.startswith("/whoami"):
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT name, username, last_seen FROM user_registry WHERE chat_id=?",
                (chat_id,),
            )
            user_info = cur.fetchone()
            conn.close()
            if user_info:
                name, uname, last_seen = user_info
                send_message(
                    chat_id,
                    f"🆔 *Chat ID:* `{chat_id}`\n"
                    f"👤 *Name:* {name}\n"
                    f"📛 *Username:* @{uname or '—'}\n"
                    f"⏱ *Last Seen:* {last_seen}",
                    parse_mode="Markdown",
                )
            else:
                send_message(
                    chat_id, "⚠️ You’re not registered yet. Try sending /start."
                )
            return
        
        # --- /status (kept) ---
        if text_lower.startswith("/status"):
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM task WHERE enabled=1")
                active_tasks = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM user_registry")
                total_users = cur.fetchone()[0]
                conn.close()

                jobs = scheduler.get_jobs()
                job_count = len(jobs)

                status_msg = (
                    f"🧾 *System Status:*\n\n"
                    f"👥 Total Users: {total_users}\n"
                    f"🕒 Active Tasks: {active_tasks}\n"
                    f"🗓️ Scheduled Jobs: {job_count}\n"
                    f"🕰️ Server Time: {datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                send_message(chat_id, status_msg, parse_mode="Markdown")
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to fetch status: {e}")
            return
        # 🧾 --- list reminders (kept) ---
        if text_lower.startswith("/list_reminders"):
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, params_json, schedule_rule, enabled "
                    "FROM task WHERE enabled=1"
                )
                rows = cur.fetchall()
                conn.close()

                if not rows:
                    send_message(chat_id, "ℹ️ You have no active reminders.")
                    return

                lines = []
                for tid, params_json, rule, enabled in rows:
                    try:
                        params = json.loads(params_json)
                        msg_text = params["calls"][0]["args"].get("text", "")
                        plan = params.get("plan", "")
                    except Exception:
                        msg_text = "(unreadable)"
                        plan = "unknown"
                    lines.append(
                        f"🆔 *{tid}* → ({plan}) {msg_text}\n   ⏱ {rule}"
                    )

                msg_body = (
                    "📋 *Active Reminders & Orders:*\n\n"
                    + "\n\n".join(lines)
                    + "\n\nUse `/delete_reminder <id>` to delete a reminder."
                )
                send_message(chat_id, msg_body, parse_mode="Markdown")
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to list reminders: {e}")
            return

        # ❌ --- delete reminder ---
        if text_lower.startswith("/delete_reminder"):
            parts = text.split()
            if len(parts) < 2:
                send_message(chat_id, "Usage: /delete_reminder <reminder_id>")
                return

            try:
                rid = int(parts[1])
            except ValueError:
                send_message(
                    chat_id, "Please provide a valid numeric reminder ID."
                )
                return

            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("UPDATE task SET enabled=0 WHERE id=?", (rid,))
                conn.commit()
                conn.close()

                job_id = f"reminder-{rid}"
                job = scheduler.get_job(job_id)
                if job:
                    job.remove()

                send_message(
                    chat_id,
                    f"✅ Reminder *{rid}* deleted successfully.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                send_message(chat_id, f"⚠️ Could not delete reminder {rid}: {e}")
            return
        
        

        # --- /link_gmail command ---
        if text_lower.startswith("/link_gmail") or text_lower.startswith(
            "/connect_gmail"
        ):
            try:
                from src.tools.email_summary import start_gmail_oauth

                send_message(chat_id, "🔗 Starting Gmail link process...")
                start_gmail_oauth(chat_id)
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to start Gmail linking: {e}")
            return

        # --- /check_gmail command ---
        if text_lower.startswith("/check_gmail"):
            try:
                from src.tools.email_summary import check_gmail_link

                linked = check_gmail_link(chat_id)
                if linked:
                    send_message(
                        chat_id, "✅ Your Gmail is linked successfully."
                    )
                else:
                    send_message(
                        chat_id,
                        "⚠️ Your Gmail is not linked yet. Use /link_gmail to link.",
                    )
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to check Gmail link: {e}")
            return

        # --- /disconnect_gmail command ---
        if text_lower.startswith("/disconnect_gmail"):
            try:
                from src.tools.email_summary import disconnect_gmail

                disconnect_gmail(chat_id)
                send_message(chat_id, "✅ Your Gmail has been unlinked.")
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to unlink Gmail: {e}")
            return

        # --- /manual command (kept) ---
        if text_lower.startswith("/manual"):
            manual_text = (
                "📖 *AI Micro Agent User Guide*\n\n"
                "I can help you with reminders, orders, notes, and Gmail summaries. Here’s how to use me:\n\n"
                "🕒 *Reminders*\n"
                "• `/remind drink water every 2 hours` — set a reminder\n"
                "• `/list_reminders` — list all your reminders\n"
                "• `/delete_reminder <id>` — delete a reminder by its ID\n\n"
                "🛒 *Orders*\n"
                "• `/remind order milk from Capital Store` — immediate order\n"
                "• `/remind order milk in 2 hours from Capital Store` — one-time delayed order\n"
                "• `/remind order milk every 2 days from Capital Store` — recurring order\n"
                "• Use `/endchat` to finish a buyer<->store chat\n\n"
                "📝 *Notes*\n"
                "• `/note buy fruits` — save a note\n"
                "• `/notes` — list your notes\n"
                "• `/delete_note <id>` — delete a note\n\n"
                "💌 *Email Digest*\n"
                "• `/link_gmail` — link your Gmail account\n"
                "• `/emailsummary` — get an immediate Gmail digest (default 5)\n"
                "• `/emailsummary 10` — get last 10 emails now\n"
                "• `/emailsummary every day at 10am` — schedule daily digest\n"
                "• `/emailsummary weekly on Mon at 9am` — schedule weekly digest\n\n"
                "🧾 *Jobs & Info*\n"
                "• `/list_jobs` — show scheduled jobs (next run times)\n"
                "• `/whoami` — see your profile info\n\n"
                "Feel free to ask for help! 🚀"
            )
            send_message(chat_id, manual_text, parse_mode="Markdown")
            return

        # --- /list_jobs command ---
        if text_lower.startswith("/list_jobs"):
            try:
                jobs = scheduler.get_jobs()
                if not jobs:
                    send_message(chat_id, "ℹ️ No active scheduled jobs.")
                    return

                lines = []
                for job in jobs:
                    nid = job.id
                    try:
                        nrt = job.next_run_time
                        if nrt:
                            nrt_local = nrt.astimezone(TZ).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                        else:
                            nrt_local = "—"
                    except Exception:
                        nrt_local = "—"
                    lines.append(f"🆔 *{nid}*\n⏰ Next run: {nrt_local}")

                msg = "🧾 *Scheduled Jobs:*\n\n" + "\n\n".join(lines)
                send_message(chat_id, msg, parse_mode="Markdown")
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to list jobs: {e}")
            return

        # --- /emailsummary handler (immediate, daily, weekly, N latest) ---
        if text_lower.startswith("/emailsummary"):
            try:
                # immediate with optional count: "/emailsummary 10"
                m_count = re.match(r"^/emailsummary\s+(\d+)\s*$", text_lower)
                if m_count:
                    maxn = int(m_count.group(1))
                    send_message(
                        chat_id,
                        f"📬 Fetching your last {maxn} emails... please wait ⏳",
                    )
                    gmail_oauth.send_daily_email_summary(
                        chat_id, max_results=maxn
                    )
                    return

                # daily schedule: "emailsummary every day at 11am"
                m_daily = re.search(
                    r"every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
                    text_lower,
                )
                if m_daily:
                    hour = int(m_daily.group(1))
                    minute = int(m_daily.group(2) or 0)
                    ampm = m_daily.group(3)
                    if ampm == "pm" and hour < 12:
                        hour += 12
                    elif ampm == "am" and hour == 12:
                        hour = 0
                    rrule = (
                        f"RRULE:FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute}"
                    )
                    plan = {
                        "task_type": "email_summary",
                        "schedule_rule": rrule,
                        "text": "Daily Gmail summary",
                    }
                    tid = persist_task_and_schedule(chat_id, plan)
                    if tid:
                        send_message(
                            chat_id,
                            f"✅ Scheduled Gmail summary every day at {hour:02d}:{minute:02d}. (task id={tid})",
                        )
                    else:
                        send_message(
                            chat_id,
                            "⚠️ Failed to schedule daily Gmail summary.",
                        )
                    return

                # weekly schedule: "emailsummary every week on monday at 9am"
                weekday_map = {
                    "monday": "MO",
                    "mon": "MO",
                    "tuesday": "TU",
                    "tue": "TU",
                    "wednesday": "WE",
                    "wed": "WE",
                    "thursday": "TH",
                    "thu": "TH",
                    "friday": "FR",
                    "fri": "FR",
                    "saturday": "SA",
                    "sat": "SA",
                    "sunday": "SU",
                    "sun": "SU",
                }
                m_weekly = re.search(
                    r"(?:every|weekly)\s*(?:week)?(?:\s*on)?\s*"
                    r"(mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday)\b"
                    r"\s*(?:at\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?",
                    text_lower,
                )
                if m_weekly:
                    day_token = m_weekly.group(1).lower()
                    hour = int(m_weekly.group(2) or 9)
                    minute = int(m_weekly.group(3) or 0)
                    ampm = m_weekly.group(4)
                    if ampm == "pm" and hour < 12:
                        hour += 12
                    elif ampm == "am" and hour == 12:
                        hour = 0
                    byday = weekday_map.get(day_token, day_token.upper()[:2])
                    rrule = (
                        f"RRULE:FREQ=WEEKLY;BYDAY={byday};"
                        f"BYHOUR={hour};BYMINUTE={minute}"
                    )
                    plan = {
                        "task_type": "email_summary",
                        "schedule_rule": rrule,
                        "text": f"Weekly Gmail summary ({byday})",
                    }
                    tid = persist_task_and_schedule(chat_id, plan)
                    if tid:
                        send_message(
                            chat_id,
                            f"✅ Scheduled weekly Gmail summary on "
                            f"{day_token.title()} at {hour:02d}:{minute:02d}. (task id={tid})",
                        )
                    else:
                        send_message(
                            chat_id,
                            "⚠️ Failed to schedule weekly Gmail summary.",
                        )
                    return

                # default immediate fetch
                send_message(
                    chat_id,
                    "📬 Fetching your Gmail summary... please wait ⏳",
                )
                gmail_oauth.send_daily_email_summary(chat_id, max_results=5)
            except Exception as e:
                send_message(chat_id, f"⚠️ Failed to get email summary: {e}")
            return

        # --- /remind command ---
        if text_lower.startswith("/remind"):
            parts = text.split(" ", 1)
            if len(parts) < 2:
                send_message(chat_id, "Usage: /remind <your instruction>")
                return

            nl_original = parts[1].strip()
            nl = nl_original.lower()

            # ----- ORDER branch -----
            if "order" in nl and "from" in nl:
                idx = nl.rfind(" from ")
                if idx == -1:
                    send_message(chat_id, "❌ Couldn't parse store name.")
                    return

                item_part = nl_original[:idx].replace("order", "", 1).strip()
                store_part = nl_original[idx + len(" from ") :].strip()

                # recurring
                m_recurring = re.search(r"every\s*(\d+)?\s*(second|seconds|minute|minutes|hour|hours|day|days|week|weeks)\b", nl)
                if m_recurring:
                    num = int(m_recurring.group(1) or 1)
                    unit = m_recurring.group(2)
                    if "second" in unit:
                        rrule = f"RRULE:FREQ=SECONDLY;INTERVAL={num}"
                    elif "minute" in unit:
                        rrule = f"RRULE:FREQ=MINUTELY;INTERVAL={num}"
                    elif "hour" in unit:
                        rrule = f"RRULE:FREQ=HOURLY;INTERVAL={num}"
                    elif "day" in unit:
                        rrule = f"RRULE:FREQ=DAILY;INTERVAL={num}"
                    else:
                        rrule = "RRULE:FREQ=DAILY;INTERVAL=1"
                    plan = {"task_type": "order", "schedule_rule": rrule, "text": f"Order {item_part} from {store_part}"}
                    tid = persist_task_and_schedule(chat_id, plan)
                    send_message(chat_id, f"✅ Scheduled recurring order (task id={tid}).")
                    return

                # one-time (persistent MCP job)
                m_once = re.search(r"in\s+(\d+)\s*(second|seconds|minute|minutes|hour|hours)\b", nl)
                if m_once:
                    num = int(m_once.group(1))
                    unit = m_once.group(2)

                    # Convert to seconds for timestamp calculation
                    delay_seconds = num if "second" in unit else num * 60 if "minute" in unit else num * 3600
                    run_at = (datetime.datetime.now(TZ) + datetime.timedelta(seconds=delay_seconds)).isoformat()

                    # Build plan for MCP + DB
                    plan = {
                        "task_type": "order",
                        "schedule_rule": f"RRULE:FREQ=ONCE;RUN_AT={run_at}",
                        "text": f"Order {item_part} from {store_part}",
                        "extra": {"store": store_part, "item": item_part}
                    }

                    tid = persist_task_and_schedule(chat_id, plan)
                    if tid:
                        send_message(chat_id, f"✅ Scheduled one-time order (task id={tid}) for *{item_part}* from *{store_part}* in {num} {unit}.")
                    else:
                        send_message(chat_id, "⚠️ Failed to schedule order.")
                    return


                orders.place_order(chat_id, store_part, item_part)
                return

            # ----- REMINDER branch -----
            # Try pattern-based parsing first (simple interval reminders)
            explicit = re.search(r"(?P<action>.+?)\s+every\s+(?P<num>\d+)\s*(?P<unit>second|seconds|minute|minutes|hour|hours|day|days)\b", nl)
            if explicit:
                action = explicit.group("action").strip()
                num = int(explicit.group("num"))
                unit = explicit.group("unit")

                if "second" in unit:
                    rrule = f"RRULE:FREQ=SECONDLY;INTERVAL={num}"
                elif "minute" in unit:
                    rrule = f"RRULE:FREQ=MINUTELY;INTERVAL={num}"
                elif "hour" in unit:
                    rrule = f"RRULE:FREQ=HOURLY;INTERVAL={num}"
                elif "day" in unit:
                    rrule = f"RRULE:FREQ=DAILY;INTERVAL={num}"
                else:
                    rrule = "RRULE:FREQ=HOURLY;INTERVAL=1"

                plan = {
                    "task_type": "reminder",
                    "schedule_rule": rrule,
                    "text": action.capitalize()
                }
                tid = persist_task_and_schedule(chat_id, plan)
                if tid:
                    send_message(chat_id, f"✅ Created reminder (task id={tid}). I’ll remind you to {action} per the schedule.")
                else:
                    send_message(chat_id, "⚠️ Failed to create reminder.")
                return

            # If no explicit time pattern → call Ollama
            send_message(chat_id, f"Got it — I'll create a reminder for: \"{nl_original}\". Processing with Ollama...")

            system_prompt = (
                "You are a JSON-only generator. Convert the user's instruction into a "
                "single JSON object and output only that JSON object and nothing else. "
                "The JSON must have exactly these keys: "
                "\"task_type\" (one of 'reminder'|'bill_link'|'email_summary'), "
                "\"schedule_rule\" (an iCalendar RRULE string like "
                "'RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0'), "
                "\"text\" (the message to send).\n\n"
                f"Input: {nl_original}\nOutput:"
            )

            raw = None
            try:
                raw = call_ollama(system_prompt)
            except Exception as e:
                print("⚠️ Ollama call failed:", e)

            plan = None
            if raw:
                try:
                    print("🔎 Model raw response preview:")
                    print(raw[:500])
                    plan = extract_json_from_text(raw)
                except Exception as e:
                    print("⚠️ Failed to parse LLM response:", e)

            if not plan or not isinstance(plan, dict):
                send_message(chat_id,
                    "⚠️ I couldn’t understand the timing. Please say it clearly, e.g.\n"
                    "`/remind drink water every 15 seconds`\n"
                    "`/remind stretch every 2 hours`",
                    parse_mode="Markdown")
                return


            tid = persist_task_and_schedule(chat_id, plan)
            if tid:
                send_message(chat_id, f"✅ Created reminder (task id={tid}). I’ll remind you per the schedule.")
            else:
                send_message(
                    chat_id,
                    "⚠️ Failed to create reminder. Please try again.",
                )
            return

    except Exception as e:
         print("⚠️ process_message error:", e)


def handle_callback_query(callback_query):
    try:
        data = callback_query.get("data", "")
        from_user = callback_query.get("from", {})
        user_id = str(from_user.get("id"))
        # route callback to orders module (keeps existing behavior)
        if data.startswith(("accept_", "out_")):
            orders.handle_store_callback(data, user_id)
        else:
            orders.handle_buyer_callback(data, user_id)
    except Exception as e:
        print("⚠️ handle_callback_query error:", e)


def main_loop():
    offset = load_offset()
    print("✅ Telegram listener started (polling getUpdates).")
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = requests.get(
                f"{TG_BASE}/getUpdates", params=params, timeout=40
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                save_offset(offset)
                if "message" in upd:
                    process_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback_query(upd["callback_query"])
            time.sleep(0.5)
        except Exception as e:
            print("⚠️ Telegram listener error:", e)
            time.sleep(2)


if __name__ == "__main__":
    main_loop()
