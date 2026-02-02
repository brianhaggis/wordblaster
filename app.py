import os
import time
import copy
import traceback
import base64
from threading import Lock
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import anthropic
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- CONFIGURATION ---
MIN_WORD_LEN = 5
POINTS_BY_LEN = {5: 3, 6: 5, 7: 8, 8: 13}

# Claude API for OCR
claude_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

# PAIRINGS are now just for display/admin purposes, 
# since we allow ANY word in the bonus round.
PAIRINGS = [
    ("FESTIVAL", "PASSPORT"),
    ("PLAYTIME", "CAMPFIRE"),
]

def tier_for_len(n: int):
    return str(n) if n in (5, 6, 7, 8) else None

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
lock = Lock()

# --- LOAD DICTIONARY ---
words = set()
try:
    with open("data/words.txt", "r", encoding="utf-8") as f:
        words = {line.strip().lower() for line in f if len(line.strip()) >= MIN_WORD_LEN}
    print(f"Dictionary Loaded: {len(words)} words")
except Exception as e:
    print(f"Dictionary Error: {e}")

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
    "round_id": 0  # Increments each countdown - prevents stale timers
}

# Track last emitted state to avoid redundant emissions
_last_emitted_state = None

def emit_state(force=False):
    """Emit game state only if it has changed"""
    global _last_emitted_state
    
    # Create the safe export version
    safe_state = state.copy()
    safe_state["used_words"] = list(state["used_words"])
    
    # BUG FIX: Use deepcopy for the comparison snapshot. 
    # Otherwise, nested dicts (like team scores) update by reference 
    # and equality checks always return True.
    if not force and _last_emitted_state is not None:
        if _last_emitted_state == safe_state:
            return
    
    # Deepcopy ensures we save the VALUES at this moment, not references
    _last_emitted_state = copy.deepcopy(safe_state)
    
    socketio.emit("game_state", safe_state)
    
    try:
        curr = PAIRINGS[state["pair_index"] % len(PAIRINGS)]
        socketio.emit("admin_secrets", {"A": curr[0], "B": curr[1]})
    except Exception:
        pass

def transition_to_game_over():
    """Wait for bonus reveal animations, then transition to game over"""
    # Wait 12 seconds to allow for Drumroll (5s) + Reveal (6s) + Buffer
    time.sleep(12) 
    with lock:
        state["phase"] = "game_over"
    emit_state()

def clear_result_after_delay():
    """Clear result display after 5 seconds"""
    time.sleep(5)
    with lock:
        if state["phase"] == "idle":
            state["last_result"] = None
    emit_state()

@app.route("/")
def index(): return render_template("index.html", dict_size=len(words))

@app.route("/admin")
def admin(): return render_template("admin.html")

@app.route("/scan")
def scan(): return render_template("scan.html", min_len=MIN_WORD_LEN)

@app.route("/board")
def board(): return render_template("board.html")

@app.route("/diagnostic")
def diagnostic(): return render_template("diagnostic.html")

@app.route("/test")
def test(): return render_template("test.html")

@app.route("/ocr", methods=["POST"])
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
        
        # Call Claude Vision
        message = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
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
                            "text": "This image shows large black letters on white paper or background. Read the letters from left to right. Only these letters are possible: A, C, E, F, I, L, M, O, P, R, S, T, V, Y. Return ONLY the letters you see, no spaces, no punctuation, no explanation. If you see no letters, return NONE."
                        }
                    ],
                }
            ],
        )
        
        # Extract the letters from Claude's response
        response_text = message.content[0].text.strip().upper()
        
        # Clean - only keep valid letters
        valid_letters = set("ACEFILMOPRSTVY")
        letters = "".join(c for c in response_text if c in valid_letters)
        
        print(f"[OCR] Claude saw: '{response_text}' -> cleaned: '{letters}'")
        
        return jsonify({"letters": letters, "raw": response_text})
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e), "letters": ""}), 500

@app.route("/start_game", methods=["POST"])
def start_game():
    """Initialize a new game with fresh state - COMPLETE RESET"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        with lock:
            state["pair_index"] = (state["pair_index"] + 1) % len(PAIRINGS)
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
        emit_state(force=True)
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/init_bonus", methods=["POST"])
def init_bonus():
    """Initialize bonus round for the winning team"""
    with lock:
        scoreA = state["teamA"]["score"]
        scoreB = state["teamB"]["score"]
        
        if scoreA >= scoreB: 
            state["winning_team"] = "A"
            state["current_team"] = "A"
        else: 
            state["winning_team"] = "B"
            state["current_team"] = "B"
            
        state["phase"] = "bonus_intro"
        state["last_result"] = None
        state["bonus_submitted"] = False  # NEW: Reset bonus submission flag
    emit_state()
    return jsonify({"ok": True})

@app.route("/reset_game", methods=["POST"])
def reset_game():
    """Reset game to intro state"""
    with lock:
        state["phase"] = "intro"
        state["teamA"]["score"] = 0
        state["teamB"]["score"] = 0
        state["winning_team"] = None
        state["last_result"] = None
        state["bonus_submitted"] = False  # NEW: Reset bonus flag
    emit_state()
    return jsonify({"ok": True})

@app.route("/submit", methods=["POST"])
def submit():
    """Process word submission for both standard and bonus rounds"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        word = (data.get("word") or "").strip().lower()
        n = len(word)
        
        # DEBUG: Log every submission
        print(f"[SUBMIT] Word: '{word}' (len={n}), Phase: {state['phase']}")
        
        with lock:
            team = state["current_team"]
            valid = False
            reason = "unknown"
            pts = 0
            tier = tier_for_len(n)
            
            # --- BONUS ROUND LOGIC ---
            if state["phase"] in ["bonus_active", "bonus_scanning", "bonus_scan_failed"]:
                
                # Check if bonus already submitted
                if state["bonus_submitted"]:
                    return jsonify({"valid": False, "points": 0, "reason": "already_submitted"}), 400
                
                # 1. Check Length (Must be at least 5)
                if n < 5:
                    reason = "too_short"
                # 2. Check Dictionary (Using words.txt)
                elif word not in words:
                    reason = "not_in_dictionary"
                else:
                    valid = True
                    reason = "BONUS_CLEARED"
                    
                    # TIERED SCORING FOR BONUS
                    if n == 5: pts = 7
                    elif n == 6: pts = 9
                    elif n == 7: pts = 13
                    elif n >= 8: pts = 20
                    
                    if team == "A": state["teamA"]["score"] += pts
                    else: state["teamB"]["score"] += pts

                # Result for Board
                state["last_result"] = {
                    "id": time.time(), "word": word, "valid": valid, 
                    "len": n, "tier": str(n), "points": pts, "reason": reason
                }
                
                # FIXED: Mark bonus as submitted and trigger end sequence
                state["bonus_submitted"] = True
                state["phase"] = "bonus_intro"
                socketio.start_background_task(transition_to_game_over)

            # --- STANDARD GAME LOGIC ---
            else:
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
                    if team == "A": state["teamA"]["score"] += pts
                    else: state["teamB"]["score"] += pts
                    reason = "ok"

                state["last_result"] = {
                    "id": time.time(),
                    "word": word, "valid": valid, "len": n, "tier": tier, 
                    "points": pts, "reason": reason
                }
                state["phase"] = "idle"
                state["current_team"] = "B" if team == "A" else "A"
                socketio.start_background_task(clear_result_after_delay)

        emit_state()
        print(f"[RESULT] Valid: {valid}, Points: {pts}, Reason: {reason}")
        return jsonify({"valid": valid, "points": pts, "reason": reason})
    except Exception:
        traceback.print_exc()
        return jsonify({"valid": False}), 500

@socketio.on("game_trigger")
def on_trigger():
    """Handle button press / trigger events"""
    now = time.time()
    with lock:
        # Debounce triggers (2 second cooldown)
        if now - state["last_trigger_at"] < 2.0: 
            return
        state["last_trigger_at"] = now
        
        if state["phase"] == "intro":
            state["phase"] = "idle"
        elif state["phase"] == "idle":
            state["phase"] = "countdown"
            state["last_result"] = None
            state["round_id"] += 1  # New round - invalidates old timers
            socketio.start_background_task(do_countdown)
        
        # BONUS TRIGGER
        elif state["phase"] == "bonus_intro":
            state["phase"] = "bonus_countdown"
            state["bonus_submitted"] = False
            state["round_id"] += 1  # New round - invalidates old timers
            socketio.start_background_task(do_bonus_round)
            
        elif state["phase"] in ["active", "bonus_active"]:
            new_phase = "bonus_scanning" if state["phase"] == "bonus_active" else "scanning"
            state["phase"] = new_phase
            socketio.emit("snapshot")
            socketio.start_background_task(scan_watchdog)
            
    emit_state()

@socketio.on("trigger_snapshot")
def on_trigger_snapshot():
    """Manual snapshot trigger (keyboard shortcut)"""
    with lock:
        if state["phase"] in ["active", "countdown"]:
            state["phase"] = "scanning"
            socketio.emit("snapshot")
            socketio.start_background_task(scan_watchdog)
        elif state["phase"] in ["bonus_active", "bonus_countdown"]:
            state["phase"] = "bonus_scanning"
            socketio.emit("snapshot")
            socketio.start_background_task(scan_watchdog)
    emit_state()

@socketio.on("scan_timeout")
def on_scan_timeout():
    """Client reported scan failure"""
    with lock: 
        if "bonus" in state["phase"]: 
            state["phase"] = "bonus_scan_failed"
        else: 
            state["phase"] = "scan_failed"
    emit_state()

@socketio.on("scan_complete")
def on_scan_complete():
    """Client reported successful scan (word submitted via HTTP)"""
    # This is informational - the actual state change happens in /submit
    # But we can use this to cancel the watchdog timer logic
    print("Scanner reported successful scan")

@socketio.on("connect")
def on_connect(auth=None): 
    """Send current state to newly connected clients"""
    emit_state(force=True)

# --- TIMERS ---
def do_countdown():
    """3-2-1 countdown before round starts"""
    # Capture round_id at start - if it changes, this timer is stale
    with lock:
        my_round_id = state["round_id"]
    
    time.sleep(3)
    with lock:
        if state["phase"] == "countdown" and state["round_id"] == my_round_id: 
            state["phase"] = "active"
    emit_state()
    
    # 30 second round timer
    time.sleep(30)
    with lock:
        # Only timeout if this is still the same round
        if state["phase"] == "active" and state["round_id"] == my_round_id:
            state["phase"] = "idle"
            state["last_result"] = {
                "id": time.time(), 
                "word": "", 
                "valid": False, 
                "points": 0, 
                "reason": "TIMEOUT"
            }
            state["current_team"] = "B" if state["current_team"] == "A" else "A"
            socketio.start_background_task(clear_result_after_delay)
    emit_state()

def do_bonus_round():
    """Bonus round timer with 60 second limit"""
    # Capture round_id at start
    with lock:
        my_round_id = state["round_id"]
    
    time.sleep(3)  # 3-2-1 countdown
    with lock:
        if state["phase"] == "bonus_countdown" and state["round_id"] == my_round_id: 
            state["phase"] = "bonus_active"
    emit_state()
    
    # 60 second bonus round
    time.sleep(60) 
    
    with lock:
        # Only timeout if still same round and not submitted
        if state["phase"] == "bonus_active" and state["round_id"] == my_round_id and not state["bonus_submitted"]:
            state["last_result"] = {
                "id": time.time(), 
                "word": "", 
                "valid": False, 
                "points": 0, 
                "reason": "TIME_EXPIRED"
            }
            state["bonus_submitted"] = True
            state["phase"] = "bonus_intro"
            socketio.start_background_task(transition_to_game_over)
    emit_state()

def scan_watchdog():
    """Force scan failure after 11 seconds if no response"""
    with lock:
        my_round_id = state["round_id"]
    
    time.sleep(11)
    with lock:
        # Only apply if still same round
        if state["round_id"] != my_round_id:
            return
        if state["phase"] == "scanning": 
            state["phase"] = "scan_failed"
        elif state["phase"] == "bonus_scanning": 
            state["phase"] = "bonus_scan_failed"
        else: 
            return  # Scan already completed
    emit_state()

if __name__ == "__main__":
    # For local development only - Render uses gunicorn
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)
