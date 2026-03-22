import os
import json
import asyncio
import threading
import edge_tts
import logging
import queue
import time
import datetime
from flask import Flask, request, abort, jsonify, render_template, send_file, make_response
from flask_cors import CORS
from flask_sock import Sock  # Added for real-time broadcast
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, PostbackEvent
from dotenv import load_dotenv

# Load variables from .env if present
load_dotenv()

# --- Configuration & Globals ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PickupRenderServer")

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app) # Enable CORS for cross-domain access
sock = Sock(app) # WebSocket instance

# WebSocket client management (similar to RelayBell_demo.py)
WEB_WS_CLIENTS = []
WEB_WS_LOCK = threading.Lock()

def broadcast_web_audio(audio_url, text):
    """Notify all connected web clients to play the audio immediately."""
    msg = json.dumps({
        "type": "play_audio",
        "url": audio_url,
        "text": text,
        "ts": time.time()
    })
    logger.info(f"📡 [WS-廣播] 正在推送語音通知到所有 Web 端 ({len(WEB_WS_CLIENTS)} 個連線)")
    dead = []
    with WEB_WS_LOCK:
        for ws in WEB_WS_CLIENTS:
            try:
                ws.send(msg)
            except Exception:
                dead.append(ws)
        for d in dead:
            if d in WEB_WS_CLIENTS:
                WEB_WS_CLIENTS.remove(d)

@sock.route('/ws/web')
def ws_web_handler(ws):
    """WebSocket endpoint for billboards/dashboards."""
    with WEB_WS_LOCK:
        WEB_WS_CLIENTS.append(ws)
    logger.info(f"🔌 [WS-連線] 新 Web 端已接入: {request.remote_addr}")
    try:
        while True:
            data = ws.receive()
            if not data: break
            # Keep-alive or interactions
    except Exception: pass
    finally:
        with WEB_WS_LOCK:
            if ws in WEB_WS_CLIENTS:
                WEB_WS_CLIENTS.remove(ws)
        logger.info(f"🔌 [WS-斷線] Web 端已離線")

# Config from Env Vars (CRITICAL CHECK)
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '9825dc29feb8522d4fc1e273411d8f37')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'q7yFoNUKHDPZZfo25zCWWEBUWhayXbnrysiE3w+xZMzHNXMJdeNJf8TpEFam3zNBLpTFgj7dLBPUGK7hrAdcf6DRKL7iDZh8b07n0rCFuypHdIYQ/s2kEHo1X+JnFIMbdSArXv/PylVkuBXpdrTQCgdB04t89/1O/w1cDnyilFU=')

if not CHANNEL_ACCESS_TOKEN:
    logger.critical("❌ [FATAL] LINE_CHANNEL_ACCESS_TOKEN 尚未設定！請至 Render 後台環境變數設定。指令回覆將失效。")

handler = WebhookHandler(CHANNEL_SECRET)

# Audio directory (absolute path)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
if not os.path.exists(AUDIO_DIR):
    os.makedirs(AUDIO_DIR)
    logger.info(f"📁 Created audio dir: {AUDIO_DIR}")

VOICE_CODE = "zh-TW-HsiaoChenNeural" # Default: HsiaoChen
VOICE_RATE = "+0%"
VOICE_VOLUME = "+0%"

speech_queue = queue.Queue()

# --- Database & History (Persistent) ---
PARENTS_FILE = os.path.join(BASE_DIR, "parents.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
PARENTS_DB = {}
pickup_history = []

HELP_TEXT = (
    "🛑 【重要通知：您尚未完成註冊】\n\n"
    "在使用接送廣播功能前，請務必先完成註冊：\n"
    "--------------------------\n"
    "✍️ 註冊方式：直接回覆 #名字\n"
    "範例：#三年二班王小明爸爸\n"
    "--------------------------\n\n"
    "⚠️ 【使用注意事項】：\n"
    "1. 廣播內容將直接顯示於校門口大螢幕並由語音讀出，請勿輸入非必要資訊。\n"
    "2. 一個 LINE 帳號僅能綁定一位學生姓名，若有異動請重新輸入註冊指令。\n"
    "3. 請確保網路收訊良好，避免訊息延遲造成接送困擾。\n"
    "4. 如有任何註冊問題，請聯繫學校教務處 (02-1234-5678)。"
)

# --- Helpers ---
def line_reply(reply_token, text):
    if not CHANNEL_ACCESS_TOKEN:
        logger.warning("No CHANNEL_ACCESS_TOKEN set, cannot reply.")
        return
    try:
        configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        logger.error(f"Failed to reply via LINE: {e}")
def load_parents_db():
    global PARENTS_DB
    if os.path.exists(PARENTS_FILE):
        try:
            with open(PARENTS_FILE, "r", encoding="utf-8") as f:
                PARENTS_DB = json.load(f)
        except Exception as e:
            logger.error(f"Error loading {PARENTS_FILE}: {e}")
            PARENTS_DB = {}
    else: PARENTS_DB = {}

def save_parents_db():
    try:
        with open(PARENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(PARENTS_DB, f, ensure_ascii=False, indent=4)
        logger.info(f"💾 Saved parents DB to {PARENTS_FILE}")
    except Exception as e:
        logger.error(f"Error saving {PARENTS_FILE}: {e}")

def load_history():
    global pickup_history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                pickup_history = json.load(f)
            logger.info(f"📂 Loaded history ({len(pickup_history)} records)")
        except Exception as e:
            logger.error(f"Error loading {HISTORY_FILE}: {e}")
            pickup_history = []
    else: pickup_history = []

def save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(pickup_history, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Error saving {HISTORY_FILE}: {e}")

load_parents_db()
load_history()

# --- Speech worker thread (Generates MP3 for clients) ---
async def generate_speech(text, v, r, vol, audio_path):
    # Robust TTS with fallback voices and sanitized parameters
    # Matches the style in the main RelayBell script
    s_rate = str(r).strip()
    if not (s_rate.startswith('+') or s_rate.startswith('-')): s_rate = "+" + s_rate
    if not s_rate.endswith('%'): s_rate += "%"

    # Define fallback trials
    trials = [
        (v, s_rate),
        (v, "+0%"),
        ("zh-TW-HsiaoChenNeural", "+0%"),
        ("en-US-JennyNeural", "+0%"),
    ]

    last_e = ""
    for trial_voice, trial_rate in trials:
        try:
            logger.info(f"🎤 [TTS嘗試] {trial_voice} ({trial_rate}) 文字: {text[:20]}")
            communicate = edge_tts.Communicate(text, trial_voice, rate=trial_rate, volume=vol)
            await communicate.save(audio_path)
            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                return True
        except Exception as e:
            last_e = str(e)
            logger.warning(f"⚠️ [TTS嘗試失敗] {trial_voice}: {e}")
            continue
    return False

def speech_worker_thread():
    while True:
        task = speech_queue.get()
        if task is None: break
        text, audio_path = task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            logger.info(f"🎤 [背景生成] {text} -> {audio_path}")
            # Use global VOICE_CODE, etc.
            success = loop.run_until_complete(generate_speech(text, VOICE_CODE, VOICE_RATE, VOICE_VOLUME, audio_path))
            if success:
                logger.info(f"✅ [生成成功] 大小: {os.path.getsize(audio_path)} bytes")
                # Immediately broadcast via WebSockets so the board plays it now!
                audio_filename = os.path.basename(audio_path)
                audio_url = f"/get_audio/{audio_filename}"
                broadcast_web_audio(audio_url, text)
            else:
                logger.error(f"❌ [生成失敗] 所有嘗試皆失敗")
        except Exception as e:
            logger.error(f"❌ [背景生成異常]: {e}")
        finally:
            loop.close()
        speech_queue.task_done()

# Start background thread
threading.Thread(target=speech_worker_thread, daemon=True).start()

# --- Web Routes ---

# Home route (Redir to Dashboard)
@app.route("/", methods=['GET'])
def index():
    return jsonify({"status": "running", "uptime": str(datetime.datetime.now())}), 200

# Web Dashboard (For Teachers)
@app.route("/dashboard", methods=['GET'])
@app.route("/pickup/dashboard", methods=['GET'])
def dashboard():
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    return render_template('dashboard.html', history=pickup_history, now=now_str)

# Large Billboard (For Student Screen)
@app.route("/billboard", methods=['GET'])
@app.route("/pickup/billboard", methods=['GET'])
def billboard():
    return render_template('billboard.html')

# API for clients/billboards to poll status
@app.route("/api/poll", methods=['GET'])
@app.route("/pickup/api/poll", methods=['GET'])
def api_poll():
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    return jsonify({
        "history": pickup_history,
        "now": now_str
    }), 200

# Manual removal of parent (Optional backend action)
@app.route("/api/clear_parent", methods=['POST'])
@app.route("/pickup/api/clear_parent", methods=['POST'])
def clear_parent():
    data = request.json
    target_name = data.get("name")
    if not target_name: return "No name provided", 400
    global pickup_history
    pickup_history = [h for h in pickup_history if h["name"] != target_name]
    logger.info(f"Cleared parent from history: {target_name}")
    return "OK", 200

# Endpoint to fetch the generated audio (MP3)
@app.route("/get_audio/<filename>", methods=['GET'])
@app.route("/pickup/get_audio/<filename>", methods=['GET'])
def get_audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if os.path.exists(path):
        resp = send_file(path, mimetype="audio/mpeg")
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    
    # 這裡如果找不到，我們記錄目前的目錄內容來除錯
    logger.warning(f"🔍 [404 音檔請求] 找不到檔案: {path}")
    logger.info(f"📂 目前音檔目錄內容: {os.listdir(AUDIO_DIR)[:10]}")
    return "No audio found", 404

# --- LINE Webhook Handler ---
@app.route("/", methods=['POST'])
@app.route("/pickup", methods=['POST'], strict_slashes=False)
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    
    # Debug Logging for 400 errors
    logger.info(f"📨 [Webhook] 收到請求 - 長度: {len(body or '')}, 簽章: {signature[:10] if signature else 'None'}...")
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("❌ [Webhook] 簽章驗證失敗 (Invalid Signature)！請檢查 LINE_CHANNEL_SECRET 是否正確。")
        abort(400)
    except Exception as e:
        logger.error(f"❌ [Webhook] 處理過程發生錯誤: {e}")
        abort(500)
    return 'OK', 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg_text = event.message.text.strip()
    user_id = event.source.user_id
    
    # 1. Registration Handling
    if msg_text.startswith("#") or msg_text.startswith("＃"):
        new_name = msg_text[1:].strip()
        if new_name:
            PARENTS_DB[user_id] = new_name
            save_parents_db()
            line_reply(event.reply_token, f"🎉 註冊成功！\n\n您的廣播識別為：【{new_name}】\n\n現在您可以點選下方選單開始呼叫孩子囉！")
        return

    # Help command / Registration Guide (不會觸發廣播)
    if msg_text in ["幫助", "註冊", "？", "?", "選單", "身分註冊", "身份註冊"]:
        line_reply(event.reply_token, HELP_TEXT)
        return

    # 2. Check Registration (CRITICAL: Every broadcast attempt must be blocked with a warning if not registered)
    if user_id not in PARENTS_DB:
        logger.warning(f"🚨 [未註冊存取] 使用者 {user_id} 嘗試發送訊息或點選選單: {msg_text}")
        # Explicit warning to the user so they don't think broadcast succeeded
        line_reply(event.reply_token, "⚠️ 【警告：呼叫失敗】\n\n您尚未完成身份註冊，系統無法為您廣播。請先依照下方指示完成註冊。\n\n" + HELP_TEXT)
        return

    # 3. Process Broadcast Message (Only for registered parents)
    parent_name = PARENTS_DB[user_id]
    s_text, s_label, s_class = msg_text, "通知", "type-soon"
    
    # Handle known button texts (from Rich Menu message actions)
    if "已到達" in msg_text:
        s_text, s_label, s_class = "已到達校門口，請儘快前往大門。", "已到達校門", "type-arrived"
    elif "即將到達" in msg_text:
        s_text, s_label, s_class = "預計 5 分鐘內即將到達。", "即將到達", "type-soon"
    elif "接走" in msg_text or "接到孩子" in msg_text:
         s_text, s_label, s_class = "已接到孩子，謝謝老師。", "已接到孩子", "type-thanks"

    global pickup_history
    # Remove old record for same parent
    pickup_history = [h for h in pickup_history if h["name"] != parent_name]
    
    now_time = datetime.datetime.now().strftime("%H:%M:%S")
    
    # Filename for audio (cloud-accessible)
    audio_filename = f"audio_{int(time.time())}_{user_id[-5:]}.mp3"
    audio_full_path = os.path.join(AUDIO_DIR, audio_filename)
    
    entry = {
        "name": parent_name, 
        "status": s_label, 
        "time": now_time, 
        "class": s_class,
        "speech_text": f"{parent_name} {s_text}",
        "audio_url": f"/get_audio/{audio_filename}"
    }
    
    # Store in history
    pickup_history.insert(0, entry)
    if len(pickup_history) > 30: pickup_history.pop()
    save_history() # Persist to disk
    
    # Queue audio generation
    speech_queue.put((f"{parent_name} {s_text}", audio_full_path))
    
    line_reply(event.reply_token, f"📢 已廣播：{parent_name} {s_text}")
    
    # Background: Clean old audio files (1 hr old)
    def clean_old_audio():
        now = time.time()
        for f in os.listdir(AUDIO_DIR):
            fpath = os.path.join(AUDIO_DIR, f)
            if os.path.isfile(fpath) and os.stat(fpath).st_mtime < now - 3600:
                try: os.remove(fpath)
                except: pass
    threading.Thread(target=clean_old_audio, daemon=True).start()

@handler.add(FollowEvent)
def handle_follow(event):
    """當使用者加入好友時，主動發送註冊說明"""
    user_id = event.source.user_id
    logger.info(f"✨ [新好友] 使用者加入: {user_id}")
    
    welcome_text = (
        "👋 您好！歡迎使用【學生接送廣播系統】。\n\n"
        "為了能正確辨識您的身份並在校門口廣播，請先完成簡單的註冊。"
    )
    line_reply(event.reply_token, f"{welcome_text}\n\n{HELP_TEXT}")

@handler.add(PostbackEvent)
def handle_postback(event):
    """處理 Rich Menu 或按鈕點擊事件 (postback data)"""
    user_id = event.source.user_id
    data = event.postback.data
    logger.info(f"🔘 [選單點擊] 使用者: {user_id}, 動作: {data}")

    # Standardize: Convert postback into message-like flow for handle_message to digest
    # But we perform registration check here too for immediate feedback
    if user_id not in PARENTS_DB:
        logger.warning(f"🚨 [未註冊點擊] 使用者 {user_id} 點擊選單: {data}")
        line_reply(event.reply_token, "⚠️ 【警告：功能受限】\n\n偵測到您尚未註冊，請先輸入【#學生姓名】完成註冊後再使用選單按鈕。\n\n" + HELP_TEXT)
        return

    # If registered, simulate a message event so we don't duplicate broadcast logic
    # Creating a mock message object
    class MockMessage:
        def __init__(self, text): self.text = text
    
    event.message = MockMessage(data)
    handle_message(event)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
