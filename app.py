import os
import time
import hmac
import secrets
import traceback
from datetime import timedelta
from functools import wraps
from threading import Lock
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO
import anthropic
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- CONFIGURATION ---
MIN_WORD_LEN = 5
POINTS_BY_LEN = {5: 3, 6: 5, 7: 8, 8: 13}
BONUS_POINTS_BY_LEN = {5: 7, 6: 9, 7: 13, 8: 20}

COUNTDOWN_SECONDS = 3
ROUND_SECONDS = 30
BONUS_SECONDS = 60
SCAN_WATCHDOG_SECONDS = 11
RESULT_DISPLAY_SECONDS = 5
MAX_SCAN_ATTEMPTS = 2  # first scan + one retry

OCR_MODEL = "claude-haiku-5-5"

# Optional PIN that protects every station page and API. Leave unset for local dev.
ADMIN_PIN = os.environ.get("ADMIN_PIN", "").strip()

# Claude API for OCR
claude_client = anthropic.Anthropic()

# Secret target words shown on the admin page; one pair per game, in order.
PAIRINGS = [
    ("FESTIVAL", "PASSPORT"),
    ("PLAYTIME", "CAMPFIRE"),
]

STANDARD_ACCEPTING = {"active", "scanning", "scan_failed"}
BONUS_ACCEPTING = {"bonus_active", "bonus_scanning", "bonus_scan_failed"}
SCANNING_PHASES = {"scanning", "bonus_scanning"}
SCAN_FAILED_PHASES = {"scan_failed", "bonus_scan_failed"}

app = Flask(__name__)
# Without a SECRET_KEY, use a random one: logins reset on restart, but sessions can't be forged.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
socketio = SocketIO(app, async_mode="threading")
lock = Lock()

# --- LOAD DICTIONARY ---
words = set()
try:
    with open("data/words.txt", "r", encoding="utf-8") as f:
        words = {line.strip().lower() for line in f if len(line.strip()) >= MIN_WORD_LEN}
    print(f"Dictionary Loaded: {len(words)} words")
except Exception as e:
    print(f"Dictionary Error: {e}")

# Letters the OCR may return - derived from the dictionary so swapping word lists just works
OCR_LETTERS = "".join(sorted({c.upper() for w in words for c in w if c.isalpha()}))

if not ADMIN_PIN:
    print("WARNING: ADMIN_PIN is not set - all stations are open to anyone with the URL")

# --- GAME STATE ---
state = {
    "pair_index": 0,
    "teamA": {"name": "Team A", "score": 0},
    "teamB": {"name": "Team B", "score": 0},
    "used_words": set(),
    "current_team": "A",
    "phase": "intro",
    "last_result": None,
    "last_trigger_at": 0.0,
    "winning_team": None,
    "bonus_submitted": False,
    "round_id": 0,        # Increments each countdown - prevents stale timers
    "scan_id": 0,         # Increments each scan - prevents stale watchdogs
    "scan_attempts": 0,   # Scans used this round
    "deadline": None,     # Epoch seconds when the current countdown/round ends
    "tiebreak": False,    # Sudden death in progress
    "tiebreak_turns": 0,  # Sudden-death turns played (one per team)
}
games_started = 0

# Track last emitted state to avoid redundant emissions
_last_emitted_state = None


def export_state():
    """JSON-safe copy of the state for clients"""
    safe = dict(state)
    safe["teamA"] = dict(state["teamA"])
    safe["teamB"] = dict(state["teamB"])
    safe["used_words"] = sorted(state["used_words"])
    safe["can_retry_scan"] = (state["phase"] in SCAN_FAILED_PHASES
                              and state["scan_attempts"] < MAX_SCAN_ATTEMPTS)
    return safe


def emit_state(force=False, to=None):
    """Emit game state (to everyone, or one client) - skips broadcasts that change nothing"""
    global _last_emitted_state
    with lock:
        safe_state = export_state()
        pair = PAIRINGS[state["pair_index"] % len(PAIRINGS)]

    if to is None:
        if not force and _last_emitted_state == safe_state:
            return
        _last_emitted_state = safe_state

    # server_now lets boards sync their countdown to the server clock
    socketio.emit("game_state", {**safe_state, "server_now": time.time()}, to=to)
    socketio.emit("admin_secrets", {"A": pair[0], "B": pair[1]}, to=to)


# --- AUTH ---
def is_authed():
    return not ADMIN_PIN or session.get("authed") is True


def require_pin(view):
    """Pages redirect to the PIN screen; API calls get a 401"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if is_authed():
            return view(*args, **kwargs)
        if request.method == "GET":
            return redirect(url_for("login", next=request.path))
        return jsonify({"error": "unauthorized"}), 401
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    error = None
    if request.method == "POST":
        pin = request.form.get("pin", "")
        if ADMIN_PIN and hmac.compare_digest(pin.encode(), ADMIN_PIN.encode()):
            session["authed"] = True
            session.permanent = True
            return redirect(next_url)
        error = "Wrong PIN"
    return render_template("login.html", next_url=next_url, error=error)


# --- PAGES ---
@app.route("/")
def index(): return render_template("index.html", dict_size=len(words))

@app.route("/admin")
@require_pin
def admin(): return render_template("admin.html")

@app.route("/scan")
@require_pin
def scan(): return render_template("scan.html", min_len=MIN_WORD_LEN)

@app.route("/board")
@require_pin
def board(): return render_template("board.html")

@app.route("/diagnostic")
@require_pin
def diagnostic(): return render_template("diagnostic.html")


# --- GAME FLOW HELPERS (call with lock held) ---
def other_team(team):
    return "B" if team == "A" else "A"


def start_scan_locked():
    """Move into the scanning phase and tell the scanner to take a photo"""
    bonus = state["phase"].startswith("bonus")
    state["phase"] = "bonus_scanning" if bonus else "scanning"
    state["scan_attempts"] += 1
    state["scan_id"] += 1
    scan_id = state["scan_id"]
    socketio.emit("snapshot", {"scan_id": scan_id})
    socketio.start_background_task(scan_watchdog, scan_id)


def can_retry_scan_locked():
    return state["phase"] in SCAN_FAILED_PHASES and state["scan_attempts"] < MAX_SCAN_ATTEMPTS


def finish_turn_locked():
    """End a standard turn: switch teams, and settle sudden death once both teams have played"""
    state["phase"] = "idle"
    state["deadline"] = None
    state["current_team"] = other_team(state["current_team"])
    socketio.start_background_task(clear_result_after_delay, state["last_result"]["id"])
    if state["tiebreak"]:
        state["tiebreak_turns"] += 1
        if state["tiebreak_turns"] >= 2:
            socketio.start_background_task(resolve_tiebreak, state["round_id"])


def begin_bonus_locked():
    """Send the leader to the bonus round, or go to sudden death on a tie"""
    scoreA = state["teamA"]["score"]
    scoreB = state["teamB"]["score"]
    state["last_result"] = None
    state["bonus_submitted"] = False
    state["deadline"] = None

    if scoreA == scoreB:
        state["phase"] = "tie"
        state["tiebreak"] = True
        state["tiebreak_turns"] = 0
        state["winning_team"] = None
        state["current_team"] = "A"
        return

    state["tiebreak"] = False
    state["tiebreak_turns"] = 0
    state["winning_team"] = "A" if scoreA > scoreB else "B"
    state["current_team"] = state["winning_team"]
    state["phase"] = "bonus_intro"


# --- API ---
@app.route("/ocr", methods=["POST"])
@require_pin
def ocr():
    """Use Claude Vision to read letters from camera image"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        image_data = data.get("image", "")

        # Remove data URL prefix if present
        if "," in image_data:
            image_data = image_data.split(",")[1]

        if not image_data:
            return jsonify({"error": "No image data", "letters": ""}), 400

        message = claude_client.messages.create(
            model=OCR_MODEL,
            max_tokens=50,
            thinking={"type": "disabled"},  # Simple read - skip reasoning for speed
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "This image shows large black letters on white paper or background. "
                                "Read the letters from left to right. "
                                f"Only these letters are possible: {', '.join(OCR_LETTERS)}. "
                                "Return ONLY the letters you see, no spaces, no punctuation, no explanation. "
                                "If you see no letters, return NONE."
                            ),
                        },
                    ],
                }
            ],
        )

        # Refusals come back with no text block - treat as "no letters"
        response_text = next((b.text for b in message.content if b.type == "text"), "").strip().upper()

        # Clean - only keep valid letters
        letters = "".join(c for c in response_text if c in OCR_LETTERS)

        print(f"[OCR] Claude saw: '{response_text}' -> cleaned: '{letters}'")

        return jsonify({"letters": letters, "raw": response_text})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e), "letters": ""}), 500


@app.route("/start_game", methods=["POST"])
@require_pin
def start_game():
    """Initialize a new game with fresh state - COMPLETE RESET"""
    global games_started
    try:
        data = request.get_json(force=True, silent=True) or {}
        with lock:
            state["pair_index"] = games_started % len(PAIRINGS)
            games_started += 1
            if data.get("teamA"): state["teamA"]["name"] = data["teamA"]
            if data.get("teamB"): state["teamB"]["name"] = data["teamB"]
            state["teamA"]["score"] = 0
            state["teamB"]["score"] = 0
            state["used_words"].clear()
            state["current_team"] = "A"
            state["phase"] = "intro"
            state["last_result"] = None
            state["winning_team"] = None
            state["bonus_submitted"] = False
            state["last_trigger_at"] = 0.0  # Reset debounce timer
            state["round_id"] += 1  # Invalidate any running timers
            state["scan_attempts"] = 0
            state["deadline"] = None
            state["tiebreak"] = False
            state["tiebreak_turns"] = 0
        emit_state(force=True)
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/init_bonus", methods=["POST"])
@require_pin
def init_bonus():
    """Start the bonus round for the leader (or sudden death on a tie)"""
    with lock:
        if state["phase"] != "idle":
            return jsonify({"ok": False, "error": "Finish the current round first"}), 409
        state["round_id"] += 1  # Cancel any pending tiebreak resolution
        begin_bonus_locked()
        phase = state["phase"]
    emit_state()
    return jsonify({"ok": True, "phase": phase})


@app.route("/submit", methods=["POST"])
@require_pin
def submit():
    """Process word submission for both standard and bonus rounds"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        word = (data.get("word") or "").strip().lower()
        n = len(word)

        print(f"[SUBMIT] Word: '{word}' (len={n}), Phase: {state['phase']}")

        with lock:
            phase = state["phase"]
            team = state["current_team"]
            valid = False
            pts = 0

            # --- BONUS ROUND LOGIC ---
            if phase in BONUS_ACCEPTING:
                if state["bonus_submitted"]:
                    return jsonify({"valid": False, "points": 0, "reason": "already_submitted"}), 409

                if n < MIN_WORD_LEN:
                    reason = "too_short"
                elif word not in words:
                    reason = "not_in_dictionary"
                else:
                    valid = True
                    reason = "BONUS_CLEARED"
                    pts = BONUS_POINTS_BY_LEN.get(min(n, 8), 0)
                    state["team" + team]["score"] += pts

                state["last_result"] = {
                    "id": time.time(), "word": word, "valid": valid,
                    "len": n, "tier": str(n), "points": pts, "reason": reason
                }

                # Mark bonus as submitted and trigger end sequence
                state["bonus_submitted"] = True
                state["phase"] = "bonus_intro"
                state["deadline"] = None
                socketio.start_background_task(transition_to_game_over, state["round_id"])

            # --- STANDARD GAME LOGIC ---
            elif phase in STANDARD_ACCEPTING:
                if n < MIN_WORD_LEN:
                    reason = "too_short"
                elif word in state["used_words"]:
                    reason = "duplicate"
                elif word not in words:
                    reason = "not_in_dictionary"
                else:
                    valid = True
                    pts = POINTS_BY_LEN.get(n, 0)
                    state["used_words"].add(word)
                    state["team" + team]["score"] += pts
                    reason = "ok"

                state["last_result"] = {
                    "id": time.time(),
                    "word": word, "valid": valid, "len": n, "tier": str(n) if n in POINTS_BY_LEN else None,
                    "points": pts, "reason": reason
                }
                finish_turn_locked()

            # --- NO ROUND RUNNING ---
            else:
                return jsonify({"valid": False, "points": 0, "reason": "not_accepting"}), 409

        emit_state()
        print(f"[RESULT] Valid: {valid}, Points: {pts}, Reason: {reason}")
        return jsonify({"valid": valid, "points": pts, "reason": reason})
    except Exception:
        traceback.print_exc()
        return jsonify({"valid": False}), 500


# --- SOCKET EVENTS ---
@socketio.on("connect")
def on_connect(auth=None):
    """Reject unauthenticated clients; send current state to the new client only"""
    if not is_authed():
        return False
    emit_state(force=True, to=request.sid)


@socketio.on("game_trigger")
def on_trigger():
    """Handle button press / trigger events"""
    now = time.time()
    with lock:
        # Debounce triggers (2 second cooldown)
        if now - state["last_trigger_at"] < 2.0:
            return
        state["last_trigger_at"] = now
        phase = state["phase"]

        if phase in ("intro", "tie"):
            state["phase"] = "idle"

        elif phase == "idle":
            # Sudden death is over once both teams have played - wait for the result
            if state["tiebreak"] and state["tiebreak_turns"] >= 2:
                return
            state["phase"] = "countdown"
            state["last_result"] = None
            state["round_id"] += 1
            state["scan_attempts"] = 0
            state["deadline"] = now + COUNTDOWN_SECONDS
            socketio.start_background_task(do_round, state["round_id"], False)

        elif phase == "bonus_intro" and not state["bonus_submitted"]:
            state["phase"] = "bonus_countdown"
            state["round_id"] += 1
            state["scan_attempts"] = 0
            state["deadline"] = now + COUNTDOWN_SECONDS
            socketio.start_background_task(do_round, state["round_id"], True)

        elif phase in ("active", "bonus_active") or can_retry_scan_locked():
            start_scan_locked()

        else:
            return

    emit_state()


@socketio.on("trigger_snapshot")
def on_trigger_snapshot():
    """Board keyboard shortcut: scan now (also retries a failed scan once)"""
    with lock:
        if state["phase"] in ("active", "countdown", "bonus_active", "bonus_countdown") or can_retry_scan_locked():
            start_scan_locked()
        else:
            return
    emit_state()


@socketio.on("scan_timeout")
def on_scan_timeout(data=None):
    """Scanner reported a failed scan"""
    scan_id = (data or {}).get("scan_id")
    with lock:
        if state["phase"] not in SCANNING_PHASES or scan_id != state["scan_id"]:
            return  # Stale report, or a test scan outside a round
        state["phase"] = "bonus_scan_failed" if state["phase"] == "bonus_scanning" else "scan_failed"
    emit_state()


# --- TIMERS ---
def do_round(my_round_id, bonus):
    """3-2-1 countdown, then the round timer"""
    countdown_phase = "bonus_countdown" if bonus else "countdown"
    active_phase = "bonus_active" if bonus else "active"
    seconds = BONUS_SECONDS if bonus else ROUND_SECONDS

    socketio.sleep(COUNTDOWN_SECONDS)
    with lock:
        if state["round_id"] != my_round_id or state["phase"] != countdown_phase:
            return
        state["phase"] = active_phase
        state["deadline"] = time.time() + seconds
    emit_state()

    socketio.sleep(seconds)
    with lock:
        # Only time out if this round is still waiting for a scan
        if state["round_id"] != my_round_id or state["phase"] != active_phase:
            return
        if bonus:
            state["last_result"] = {
                "id": time.time(), "word": "", "valid": False, "points": 0, "reason": "TIME_EXPIRED"
            }
            state["bonus_submitted"] = True
            state["phase"] = "bonus_intro"
            state["deadline"] = None
            socketio.start_background_task(transition_to_game_over, state["round_id"])
        else:
            state["last_result"] = {
                "id": time.time(), "word": "", "valid": False, "points": 0, "reason": "TIMEOUT"
            }
            finish_turn_locked()
    emit_state()


def scan_watchdog(my_scan_id):
    """Force scan failure if the scanner never answers"""
    socketio.sleep(SCAN_WATCHDOG_SECONDS)
    with lock:
        if state["scan_id"] != my_scan_id or state["phase"] not in SCANNING_PHASES:
            return  # Scan already finished, or a newer scan is running
        state["phase"] = "bonus_scan_failed" if state["phase"] == "bonus_scanning" else "scan_failed"
    emit_state()


def clear_result_after_delay(result_id):
    """Clear the result display after a few seconds"""
    socketio.sleep(RESULT_DISPLAY_SECONDS)
    with lock:
        result = state["last_result"]
        if state["phase"] != "idle" or not result or result["id"] != result_id:
            return
        state["last_result"] = None
    emit_state()


def resolve_tiebreak(my_round_id):
    """After both sudden-death turns, show the last result, then go to bonus (or tie again)"""
    socketio.sleep(RESULT_DISPLAY_SECONDS)
    with lock:
        if state["round_id"] != my_round_id or state["phase"] != "idle" or not state["tiebreak"]:
            return
        begin_bonus_locked()
    emit_state()


def transition_to_game_over(my_round_id):
    """Wait for bonus reveal animations, then transition to game over"""
    # Drumroll (5s) + Reveal (6s) + Buffer
    socketio.sleep(12)
    with lock:
        if state["round_id"] != my_round_id or state["phase"] != "bonus_intro":
            return  # A new game started meanwhile
        state["phase"] = "game_over"
    emit_state()


if __name__ == "__main__":
    # For local development only - Render uses gunicorn
    socketio.run(app, host="0.0.0.0", port=8000, debug=True, allow_unsafe_werkzeug=True)
