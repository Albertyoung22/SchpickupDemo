import os
import json
import asyncio
import threading
import edge_tts
import logging
import queue
import time
import datetime
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
logger = logging.getLogger("PickupRenderServer")

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app) # Enable CORS for cross-domain access

# Config from Env Vars
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '9825dc29feb8522d4fc1e273411d8f37')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')
handler = WebhookHandler(CHANNEL_SECRET)

# Audio directory (absolute path)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
if not os.path.exists(AUDIO_DIR): os.makedirs(AUDIO_DIR)

VOICE_CODE = "zh-TW-HsiaoChenNeural" # Default: HsiaoChen
VOICE_RATE = "+0%"
VOICE_VOLUME = "+0%"

speech_queue = queue.Queue()

# --- Database & History ---
PARENTS_FILE = "parents.json"
PARENTS_DB = {}
pickup_history = []

HELP_TEXT = (
    "🛑 【重要通知：您尚未完成註冊】\n\n"
    "在使用接送廣播功能前，請務必先完成註冊：\n"
    "--------------------------\n"
    "✍️ 請輸入格式：#三年二班王小明的爸爸\n"
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

    if os.path.exists(PARENTS_FILE):
        try:
            with open(PARENTS_FILE, "r", encoding="utf-8") as f:
                PARENTS_DB = json.load(f)
        except Exception as e:
            logger.error(f"Error loading {PARENTS_FILE}: {e}")
            PARENTS_DB = {}
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

    try:
        with open(PARENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(PARENTS_DB, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Error saving {PARENTS_FILE}: {e}")
        logger.error(f"Error saving {PARENTS_FILE}: {e}")

load_parents_db()

# --- Speech worker thread (Generates MP3 for clients) ---
async def generate_speech(text, v, r, vol, audio_path):
    try:
        communicate = edge_tts.Communicate(text, v, rate=r, volume=vol)
        await communicate.save(audio_path)
    except Exception as e:
        logger.error(f"TTS Generation Error: {e}")

def speech_worker_thread():
    while True:
        task = speech_queue.get()
        if task is None: break
        text, audio_path = task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            logger.info(f"🎤 [語音生成] 正在產製音檔: {text} -> {audio_path}")
            loop.run_until_complete(generate_speech(text, VOICE_CODE, VOICE_RATE, VOICE_VOLUME, audio_path))
            if os.path.exists(audio_path):
                logger.info(f"✅ [生成成功] 檔案已存在: {audio_path} (大小: {os.path.getsize(audio_path)} bytes)")
            else:
                logger.error(f"❌ [生成失敗] 檔案未能在預期位置找到: {audio_path}")
        except Exception as e:
            logger.error(f"❌ [語音異常] 發生非預期錯誤: {e}")
        finally:
            loop.close()
        speech_queue.task_done()

# Start background thread
threading.Thread(target=speech_worker_thread, daemon=True).start()

# --- Web Routes ---

# Home route (Redir to Dashboard)
@app.route("/", methods=['GET'])
def index():
    return jsonify({"status": "running", "uptime": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))))}), 200

# Web Dashboard (For Teachers)
@app.route('/api/tts_preview', methods=['POST'])
def api_tts_preview():
    import io, asyncio
    try:
        d = request.json or {}
        text = d.get('text')
        if not text:
            return jsonify(ok=False, error="No text"), 400

        async def _gen():
            tts = edge_tts.Communicate(text, VOICE_CODE, rate="+0%")
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

@app.route("/dashboard", methods=['GET'])
@app.route("/pickup/dashboard", methods=['GET'])
def dashboard():
    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M:%S")
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
    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M:%S")
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
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        abort(500)
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
        elif new_name:
            PARENTS_DB[user_id] = new_name
            save_parents_db()
            line_reply(event.reply_token, f"🎉 註冊成功！\n\n您的廣播識別為：【{new_name}】\n\n現在您可以點選下方選單開始呼叫孩子囉！")
        return

    # Admin DB Management Commands
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
    # Help command / Registration Guide (不會觸發廣播)
    if msg_text in ["幫助", "註冊", "？", "?", "選單", "身分註冊", "身份註冊"]:
        line_reply(event.reply_token, HELP_TEXT)
        return

    # 2. Check Registration
    if user_id not in PARENTS_DB:
        logger.warning(f"🚨 [未註冊存取] 使用者 {user_id} 嘗試發送訊息: {msg_text}")
        line_reply(event.reply_token, HELP_TEXT)
        return

    parent_name = PARENTS_DB[user_id]
    if parent_name.startswith("[BANNED]"):
        logger.warning(f"🚫 [黑名單攔截] {parent_name} 嘗試發言")
        line_reply(event.reply_token, "⚠️ 您目前已被管理員限制廣播功能。")
        return

    # Process Broadcast Message
    parent_name = PARENTS_DB[user_id]
    s_text, s_label, s_class = msg_text, "通知", "type-soon"
    
    if "已到達" in msg_text:
        s_text, s_label, s_class = "已到達校門口，請儘快前往大門。", "已到達校門", "type-arrived"
    elif "即將到達" in msg_text:
        s_text, s_label, s_class = "預計 5 分鐘內即將到達。", "即將到達", "type-soon"
    elif "接走" in msg_text or "接到孩子" in msg_text:
         s_text, s_label, s_class = "已接到孩子，謝謝老師。", "已接到孩子", "type-thanks"

    global pickup_history
    # Remove old record for same parent
    pickup_history = [h for h in pickup_history if h["name"] != parent_name]
    
    now_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M:%S")
    
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
    """處理 Rich Menu 或按鈕點擊事件，若未註冊則提示"""
    user_id = event.source.user_id
    data = event.postback.data
    logger.info(f"🔘 [選單點擊] 使用者: {user_id}, 動作: {data}")

    if user_id not in PARENTS_DB:
        logger.warning(f"🚨 [未註冊點擊] 使用者 {user_id} 嘗試點擊選單: {data}")
        line_reply(event.reply_token, HELP_TEXT)
        return

    # 若已註冊，則將 postback data 當作文字訊息處理 (模擬家長輸入文字)
    event.message = type('obj', (object,), {'text': data})
    handle_message(event)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
