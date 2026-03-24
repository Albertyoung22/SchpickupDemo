import os
import json
import asyncio
import threading
import edge_tts
import logging
import queue
import time
import datetime
try:
    import webview
except ImportError:
    webview = None
from flask import Flask, request, abort, jsonify, render_template, send_file
from flask_cors import CORS
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, PostbackEvent
from dotenv import load_dotenv

# Load variables from .env if present
load_dotenv()

# --- Configuration & Globals ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PickupDesktopApp")

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app)

# --- Windows Taskbar Icon Registration ---
if os.name == 'nt':
    import ctypes
    try:
        myappid = 'school.pickup.unified.v5'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except: pass

# Config from Env Vars
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '9825dc29feb8522d4fc1e273411d8f37')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')
handler = WebhookHandler(CHANNEL_SECRET)

# Audio directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
if not os.path.exists(AUDIO_DIR): os.makedirs(AUDIO_DIR)

VOICE_OPTIONS = {
    "曉臻 (台灣腔)": "zh-TW-HsiaoChenNeural",
    "雲哲 (台灣腔男)": "zh-TW-YunJheNeural",
    "曉曉 (最溫柔)": "zh-CN-XiaoxiaoNeural",
    "雲希 (最親切)": "zh-CN-YunxiNeural"
}

# Current Voice Settings
current_voice = "zh-TW-HsiaoChenNeural"
current_rate = "+0%"
current_volume = "+0%"
enable_local_play = True  # Enable local MPV playback
school_phone = "02-1234-5678" # Default School Phone Number

speech_queue = queue.Queue()
PARENTS_FILE = "parents.json"
PARENTS_DB = {}
pickup_history = []

def get_help_text():
    return (
        "🛑 【重要通知：您尚未完成註冊】\n\n"
        "在使用接送廣播功能前，請務必先完成註冊：\n"
        "--------------------------\n"
        "✍️ 請輸入格式：#三年二班王小明的爸爸\n"
        "--------------------------\n\n"
        "⚠️ 【使用注意事項】：\n"
        "1. 廣播內容將直接顯示於校門口大螢幕並由語音讀出，請勿輸入非必要資訊。\n"
        "2. 一個 LINE 帳號僅能綁定一位學生姓名，若有異動請重新輸入註冊指令。\n"
        "3. 請確保網路收訊良好，避免訊息延遲造成接送困擾。\n"
        f"4. 如有任何註冊問題，請聯繫學校教務處 ({school_phone})。"
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
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)]))
    except Exception as e:
        logger.error(f"Failed to reply via LINE: {e}")

def load_parents_db():
    global PARENTS_DB
    # Free, 0-config JSON storage for permanence
    blob_url = "https://jsonblob.com/api/jsonBlob/019d2072-b6a5-759c-94b4-dda409d5c48f"
    import urllib.request, urllib.error

    req = urllib.request.Request(blob_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            PARENTS_DB = json.loads(response.read().decode('utf-8'))
            logger.info("Successfully loaded DB from jsonblob!")
            return
    except Exception as e:
        logger.error(f"Jsonblob load error: {e}")

    # Fallback to local file
    if os.path.exists(PARENTS_FILE):
        try:
            with open(PARENTS_FILE, "r", encoding="utf-8") as f:
                PARENTS_DB = json.load(f)
        except: PARENTS_DB = {}
    else: PARENTS_DB = {}

def save_parents_db():
    blob_url = "https://jsonblob.com/api/jsonBlob/019d2072-b6a5-759c-94b4-dda409d5c48f"
    import urllib.request, urllib.error
    
    data = json.dumps(PARENTS_DB).encode('utf-8')
    req = urllib.request.Request(
        blob_url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            logger.info("Successfully saved DB to jsonblob!")
    except Exception as e:
        logger.error(f"Jsonblob save error: {e}")

    # Fallback to local file
    try:
        with open(PARENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(PARENTS_DB, f, ensure_ascii=False, indent=4)
    except: pass

load_parents_db()

# --- Speech worker ---
async def generate_speech(text, v, r, vol, audio_path):
    try:
        communicate = edge_tts.Communicate(text, v, rate=r, volume=vol)
        await communicate.save(audio_path)
        if enable_local_play:
            import subprocess
            import shutil
            if shutil.which("mpv"):
                logger.info(f"🔊 [本地播放] {text}")
                subprocess.run(["mpv", "--no-video", audio_path], check=False)
            else:
                logger.warning("⚠️ [本地播放失敗] 找不到 mpv 執行檔，請安裝 mpv 以啟用此功能。")
    except Exception as e:
        logger.error(f"TTS Generation/Playback Error: {e}")

def speech_worker_thread():
    while True:
        task = speech_queue.get()
        if task is None: break
        text, audio_path = task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(generate_speech(text, current_voice, current_rate, current_volume, audio_path))
        finally:
            loop.close()
        speech_queue.task_done()

threading.Thread(target=speech_worker_thread, daemon=True).start()

# --- WebView API ---
class DesktopAPI:
    def get_settings(self):
        return {
            "voice": current_voice,
            "rate": current_rate,
            "volume": current_volume,
            "local_play": enable_local_play,
            "school_phone": school_phone,
            "parent_count": len(PARENTS_DB),
            "voice_options": VOICE_OPTIONS
        }

    def update_settings(self, settings):
        global current_voice, current_rate, current_volume, enable_local_play, school_phone
        if "voice" in settings: current_voice = settings["voice"]
        if "rate" in settings: current_rate = settings["rate"]
        if "volume" in settings: current_volume = settings["volume"]
        if "local_play" in settings: enable_local_play = settings["local_play"]
        if "school_phone" in settings: school_phone = settings["school_phone"]
        logger.info(f"⚙️ [設定更新] {settings}")
        return True

desktop_api = DesktopAPI()

# --- Web Routes ---
@app.route('/api/tts_preview', methods=['POST'])
def api_tts_preview():
    import io, asyncio
    try:
        d = request.json or {}
        text = d.get('text')
        if not text:
            return jsonify(ok=False, error="No text"), 400

        async def _gen():
            # Use module's current_voice and current_rate
            tts = edge_tts.Communicate(text, current_voice, rate=current_rate)
            out = io.BytesIO()
            async for chunk in tts.stream():
                if chunk["type"] == "audio":
                    out.write(chunk["data"])
            out.seek(0)
            return out

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            audio_io = loop.run_until_complete(_gen())
        finally:
            loop.close()
            
        return send_file(audio_io, mimetype="audio/mpeg", as_attachment=False, download_name="preview.mp3")
    except Exception as e:
        logger.error(f"[TTS_PREVIEW] Error: {e}")
        return jsonify(ok=False, error=str(e)), 500

@app.route("/", methods=['GET'])
def index():
    return jsonify({"status": "running", "uptime": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))))}), 200

@app.route("/dashboard", methods=['GET'])
@app.route("/pickup/dashboard", methods=['GET'])
def dashboard():
    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M:%S")
    return render_template('dashboard.html', history=pickup_history, now=now_str)

@app.route("/billboard", methods=['GET'])
@app.route("/pickup/billboard", methods=['GET'])
def billboard():
    return render_template('billboard.html')

@app.route("/api/poll", methods=['GET'])
@app.route("/pickup/api/poll", methods=['GET'])
def api_poll():
    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M:%S")
    return jsonify({"history": pickup_history, "now": now_str}), 200

@app.route("/api/clear_parent", methods=['POST'])
@app.route("/pickup/api/clear_parent", methods=['POST'])
def clear_parent():
    data = request.json
    target_name = data.get("name")
    if not target_name: return "No name provided", 400
    global pickup_history
    pickup_history = [h for h in pickup_history if h["name"] != target_name]
    return "OK", 200

@app.route("/get_audio/<filename>", methods=['GET'])
@app.route("/pickup/get_audio/<filename>", methods=['GET'])
def get_audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if os.path.exists(path):
        resp = send_file(path, mimetype="audio/mpeg")
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    return "No audio found", 404

@app.route("/", methods=['POST'])
@app.route("/pickup", methods=['POST'], strict_slashes=False)
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except: abort(400)
    return 'OK', 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg_text = event.message.text.strip()
    user_id = event.source.user_id
    # 1. Registration Handling (SILENT - NO BROADCAST)
    if msg_text.startswith("#") or msg_text.startswith("＃") or "請輸入格式" in msg_text:
        new_name = msg_text.replace("請輸入格式", "").replace("：", "").replace(":", "").strip()
        if new_name.startswith("#") or new_name.startswith("＃"):
            new_name = new_name[1:].strip()
            
        if not new_name: return # Ignore empty commands
        
        if new_name == "取消註冊":
            if user_id in PARENTS_DB:
                del PARENTS_DB[user_id]
                save_parents_db()
                line_reply(event.reply_token, "🗑️ 已成功取消您的家長註冊。")
            return
        else:
            PARENTS_DB[user_id] = new_name
            save_parents_db()
            line_reply(event.reply_token, f"🎉 註冊成功！\n\n您的廣播識別為：【{new_name}】\n\n現在您可以點選下方選單開始呼叫孩子囉！")
        return
        
    if msg_text.startswith("@刪除") or msg_text.startswith("＠刪除"):
        target_name = msg_text[3:].strip()
        deleted = False
        for uid, name in list(PARENTS_DB.items()):
            if name == target_name or name == f"[BANNED]{target_name}":
                del PARENTS_DB[uid]
                deleted = True
        if deleted:
            save_parents_db()
            line_reply(event.reply_token, f"✅ 已將「{target_name}」從資料庫移除。")
        else:
            line_reply(event.reply_token, f"⚠️ 找不到名為「{target_name}」的家長。")
        return

    if msg_text.startswith("@黑名單") or msg_text.startswith("＠黑名單"):
        target_name = msg_text[4:].strip()
        banned = False
        for uid, name in list(PARENTS_DB.items()):
            if name == target_name:
                PARENTS_DB[uid] = f"[BANNED]{target_name}"
                banned = True
        if banned:
            save_parents_db()
            line_reply(event.reply_token, f"⛔ 已將「{target_name}」列入黑名單，該帳號將無法觸發廣播。")
        else:
            line_reply(event.reply_token, f"⚠️ 找不到名為「{target_name}」的家長。")
        return

    if msg_text in ["幫助", "註冊", "？", "?", "選單", "身分註冊", "身份註冊"]:
        line_reply(event.reply_token, get_help_text())
        return
    if "學校的電話號碼" in msg_text or "學校電話" in msg_text:
        line_reply(event.reply_token, f"🏫 學校的電話號碼：{school_phone}")
        return
    if user_id not in PARENTS_DB:
        line_reply(event.reply_token, get_help_text())
        return
        
    parent_name = PARENTS_DB[user_id]
    if parent_name.startswith("[BANNED]"):
        line_reply(event.reply_token, "⚠️ 您目前已被管理員限制廣播功能。")
        return
    s_text, s_label, s_class = msg_text, "通知", "type-soon"
    if "已到達" in msg_text: s_text, s_label, s_class = "已到達校門口，請儘快前往大門。", "已到達校門", "type-arrived"
    elif "即將到達" in msg_text: s_text, s_label, s_class = "預計 5 分鐘內即將到達。", "即將到達", "type-soon"
    elif "接走" in msg_text or "接到孩子" in msg_text: s_text, s_label, s_class = "已接到孩子，謝謝老師。", "已接到孩子", "type-thanks"
    global pickup_history
    pickup_history = [h for h in pickup_history if h["name"] != parent_name]
    now_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M:%S")
    audio_filename = f"audio_{int(time.time())}.mp3"
    audio_full_path = os.path.join(AUDIO_DIR, audio_filename)
    entry = {"name": parent_name, "status": s_label, "time": now_time, "class": s_class, "speech_text": f"{parent_name} {s_text}", "audio_url": f"/get_audio/{audio_filename}"}
    pickup_history.insert(0, entry)
    if len(pickup_history) > 30: pickup_history.pop()
    speech_queue.put((f"{parent_name} {s_text}", audio_full_path))
    line_reply(event.reply_token, f"📢 已廣播：{parent_name} {s_text}")

@handler.add(FollowEvent)
def handle_follow(event):
    line_reply(event.reply_token, f"👋 您好！歡迎使用【學生接送廣播系統】。\n\n{get_help_text()}")

# --- Desktop UI Implementation ---
def run_app():
    # Detect if running on Render (Cloud Server)
    is_render = os.environ.get("RENDER") is not None
    port = int(os.environ.get("PORT", 5000))
    
    if is_render:
        logger.info("☁️ [環境檢測] 正在 Render 雲端環境執行。")
        global enable_local_play
        enable_local_play = False # Disable local play on cloud
        # On Render, the server is managed by Gunicorn or starts here
        app.run(host='0.0.0.0', port=port, debug=False)
    else:
        # Local Desktop Mode
        logger.info(f"🏠 [環境檢測] 正在本地桌面模式執行 (Port {port})。")
        threading.Thread(target=lambda: app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False), daemon=True).start()
        time.sleep(1.5)
        webview.create_window(
            '🏫 學生接送智慧監控中心', 
            f'http://127.0.0.1:{port}/dashboard', 
            js_api=desktop_api,
            width=1024, height=768, confirm_close=True
        )
        webview.start()

if __name__ == "__main__":
    run_app()
