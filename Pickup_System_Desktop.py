import os
import json
import asyncio
import threading
import edge_tts
import logging
import queue
import time
import datetime
import serial
import requests
from gtts import gTTS
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

# Default options (will be overwritten by cloud if exists)
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
VOICE_CONFIG_BLOB_URL = "https://jsonblob.com/api/jsonBlob/019d2b5b-7936-740c-862f-fe91e9739930" # Cloud settings blob

# --- 4-Relay Configuration ---
RELAY4_PORT = os.getenv("RELAY4_PORT", "COM5") 
relay_states = {1: False, 2: False, 3: False, 4: False} # 追蹤繼電器狀態
RELAY_NAMES = {1: "📣 1號 (廣播)", 2: "💡 2號 (門燈)", 3: "🌬️ 3號 (風扇)", 4: "🚪 4號 (電門)"}

def control_usb_relay4(ch: int, on: bool):
    """控制 4-Relay 第 ch 路繼電器（1~4） - 採用 LCUS-4 協定"""
    if ch not in (1, 2, 3, 4): return False
    on_flag = 1 if on else 0
    payload = bytes([0xA0, ch, on_flag, (0xA0 + ch + on_flag) & 0xFF])
    
    try:
        # Use short timeout to avoid blocking UI too long
        with serial.Serial(RELAY4_PORT, 9600, timeout=0.5) as ser:
            ser.write(payload)
            ser.flush()
            relay_states[ch] = on # 更新狀態
            logger.info(f"Relay {ch} set to {'ON' if on else 'OFF'} via {RELAY4_PORT}")
            return True
    except Exception as e:
        logger.error(f"❌ [硬體錯誤] 無法連接至 {RELAY4_PORT}: {e}")
        return False

speech_queue = queue.Queue()
PARENTS_FILE = "parents.json"
PARENTS_DB = {}
pickup_history = []
activity_log = []
LOG_BLOB_URL = "https://jsonblob.com/api/jsonBlob/019d2b5b-74fa-7e9a-9840-3b14b0890057"
CLOUD_URL = os.getenv("CLOUD_URL", "https://schpickupdemo.onrender.com")

# --- Business Config ---
BUSINESS_CONFIG_FILE = "business_config.json"
business_config = {}

def load_business_config():
    global business_config
    default_config = {
        "account_name": "接送系統Demo",
        "status_message": "",
        "show_followers": False,
        "address": "尚未登錄",
        "business_hours": "",
        "website": "",
        "phone": "",
        "bottom_buttons": ["加入好友"]
    }
    if os.path.exists(BUSINESS_CONFIG_FILE):
        try:
            with open(BUSINESS_CONFIG_FILE, "r", encoding="utf-8") as f:
                business_config = json.load(f)
        except: business_config = default_config
    else: business_config = default_config

def save_business_config():
    try:
        with open(BUSINESS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(business_config, f, ensure_ascii=False, indent=4)
    except: pass

load_business_config()

# --- Cloud Relay Command Buffer ---
pending_relay_commands = []

def get_help_text():
    return (
        "🛑 【重要通知：您如未完成註冊】\n\n"
        "在使用接送廣播功能前，請務必先完成註冊：\n"
        "--------------------------\n"
        "✍️ 註冊方式：直接回覆 #名字+車號\n"
        "範例：#三年二班王小明爸爸+ABC-1234\n"
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
        logger.error(f"❌ [LINE 回覆失敗] Token: {reply_token[:10]}... Error: {e}")

def load_parents_db():
    global PARENTS_DB
    # Free, 0-config JSON storage for permanence
    blob_url = "https://jsonblob.com/api/jsonBlob/019d2b5b-7294-7b61-bd70-99b56a819197"
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
    blob_url = "https://jsonblob.com/api/jsonBlob/019d2b5b-7294-7b61-bd70-99b56a819197"
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

def load_activity_log():
    global activity_log
    import urllib.request, urllib.error
    req = urllib.request.Request(LOG_BLOB_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            activity_log = json.loads(response.read().decode('utf-8'))
            logger.info(f"Successfully loaded {len(activity_log)} log entries from cloud.")
    except Exception as e:
        logger.error(f"Activity log load error: {e}")

def save_activity_log():
    global activity_log
    import urllib.request, urllib.error
    # Keep only last 7 days (approx 1000 entries max to keep it fast)
    activity_log = activity_log[-1000:] 
    
    data = json.dumps(activity_log).encode('utf-8')
    req = urllib.request.Request(
        LOG_BLOB_URL,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            logger.info("Successfully saved activity log to cloud.")
    except Exception as e:
        logger.error(f"Activity log save error: {e}")

def load_voice_config():
    global current_voice, current_rate, current_volume, VOICE_OPTIONS
    import urllib.request, urllib.error
    req = urllib.request.Request(VOICE_CONFIG_BLOB_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            config = json.loads(response.read().decode('utf-8'))
            current_voice = config.get("voice", "zh-TW-HsiaoChenNeural")
            current_rate = config.get("rate", "+0%")
            current_volume = config.get("volume", "+0%")
            # Load dynamic voice options if present and not empty
            v_opts = config.get("voice_options")
            if v_opts and isinstance(v_opts, dict) and len(v_opts) > 0:
                VOICE_OPTIONS = v_opts
                logger.info(f"✅ [語音選單] 已從雲端載入 {len(VOICE_OPTIONS)} 個項目")
            else:
                logger.info("ℹ️ [語音選單] 雲端無選單資料，使用系統預設值。")
            logger.info(f"✅ [語音設定] 已從雲端載入: {current_voice}, {current_rate}")
    except Exception as e:
        logger.warning(f"⚠️ [語音設定載入失敗] 使用預設值: {e}")

def save_voice_config():
    import urllib.request, urllib.error
    config = {
        "voice": current_voice, 
        "rate": current_rate, 
        "volume": current_volume,
        "voice_options": VOICE_OPTIONS # Save the list as well
    }
    data = json.dumps(config).encode('utf-8')
    req = urllib.request.Request(
        VOICE_CONFIG_BLOB_URL,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            logger.info("✅ [語音設定] 已同步至雲端（包含選單變更）。")
    except Exception as e:
        logger.error(f"❌ [語音同步失敗]: {e}")

load_parents_db()
load_activity_log()
# Sync current visible history with the last few items of activity log
pickup_history = activity_log[-30:]
pickup_history.reverse() # Dashboard/Billboard expects newest first

# --- Speech worker ---
async def generate_speech(text, v, r, vol, audio_path):
    try:
        communicate = edge_tts.Communicate(text, v, rate=r, volume=vol)
        await communicate.save(audio_path)
    except Exception as e:
        logger.warning(f"⚠️ [Edge-TTS 失敗] 嘗試使用 gTTS 備援: {e}")
        try:
            # Fallback to gTTS
            tts = gTTS(text=text, lang='zh-tw')
            tts.save(audio_path)
        except Exception as ge:
            logger.error(f"❌ [TTS 全部失敗] {ge}")
            return

    try:
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

# --- Weather & Location Helpers ---
def _get_server_location():
    """ 自動偵測伺服器位置 """
    res = {"lat": 25.0330, "lon": 121.5654, "city": "台北市"}
    try:
        r = requests.get("http://ip-api.com/json/?fields=status,lat,lon,city&lang=zh-CN", timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                res["lat"] = data.get("lat", res["lat"])
                res["lon"] = data.get("lon", res["lon"])
                res["city"] = data.get("city") or res["city"]
    except: pass
    return res

def _get_weather_report():
    """ 取得目前天氣概況文字 """
    loc = _get_server_location()
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={loc['lat']}&longitude={loc['lon']}&current=temperature_2m,relative_humidity_2m,weather_code&timezone=auto"
        r = requests.get(url, timeout=5)
        if r.status_code != 200: return "無法取得氣象資料"
        data = r.json().get("current", {})
        temp = data.get("temperature_2m", "?")
        code = data.get("weather_code", 0)
        
        status = "晴朗"
        if code in (1, 2, 3): status = "多雲"
        elif 51 <= code <= 65: status = "有雨"
        elif code >= 95: status = "雷雨"
        
        now_str = datetime.datetime.now().strftime("%H點%M分")
        return f"現在時間 {now_str}，所在位置 {loc['city']}，目前氣溫 {temp} 度，天氣狀況：{status}。"
    except: return "氣象資料讀取失敗"

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
        global current_voice, current_rate, current_volume, enable_local_play, school_phone, VOICE_OPTIONS
        if "voice" in settings: current_voice = settings["voice"]
        if "rate" in settings: current_rate = settings["rate"]
        if "volume" in settings: current_volume = settings["volume"]
        if "local_play" in settings: enable_local_play = settings["local_play"]
        if "school_phone" in settings: school_phone = settings["school_phone"]
        if "voice_options" in settings and isinstance(settings["voice_options"], dict) and len(settings["voice_options"]) > 0:
            VOICE_OPTIONS = settings["voice_options"]
        
        save_voice_config()
        logger.info(f"⚙️ [本地設定更新] {settings}")
        return True

desktop_api = DesktopAPI()

# --- Web Routes ---
@app.route('/api/tts_preview', methods=['POST'])
def api_tts_preview():
    import io, asyncio
    try:
        d = request.json or {}
        text = d.get('text')
        voice = d.get('voice', current_voice)
        rate = d.get('rate', current_rate)

        if not text:
            return jsonify(ok=False, error="No text"), 400

        async def _gen():
            tts = edge_tts.Communicate(text, voice, rate=rate)
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

@app.route('/api/get_settings', methods=['GET'])
def api_get_settings():
    return jsonify({
        "voice": current_voice,
        "rate": current_rate,
        "volume": current_volume,
        "voice_options": VOICE_OPTIONS,
        "school_phone": school_phone
    })

@app.route('/api/update_settings', methods=['POST'])
def api_update_settings():
    global current_voice, current_rate, current_volume, VOICE_OPTIONS
    try:
        data = request.json or {}
        if "voice" in data: current_voice = data["voice"]
        if "rate" in data: current_rate = data["rate"]
        if "volume" in data: current_volume = data["volume"]
        
        # Handle dynamic voice options update (with validation)
        v_opts = data.get("voice_options")
        if v_opts and isinstance(v_opts, dict) and len(v_opts) > 0:
            VOICE_OPTIONS = v_opts
            logger.info("⚙️ [選單更新] 語音長度已更新並驗證。")
        
        save_voice_config()
        logger.info(f"⚙️ [設定更新] 語音內容已變更為: {current_voice}")
        return jsonify(ok=True)
    except Exception as e:
        logger.error(f"❌ [API 設定更新失敗]: {e}")
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/check_registration', methods=['GET'])
def api_check_registration():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify(registered=False, error="Missing user_id"), 400
    
    is_registered = user_id in PARENTS_DB
    return jsonify({
        "registered": is_registered,
        "help_text": get_help_text(),
        "parent_name": PARENTS_DB.get(user_id, "") if is_registered else ""
    })

@app.route("/", methods=['GET'])
def index():
    return render_template('portal.html')

@app.route("/landing", methods=['GET'])
def landing():
    return render_template('landing_page.html')

@app.route("/manual", methods=['GET'])
def manual():
    return render_template('manual.html')

@app.route("/business_profile", methods=['GET'])
def business_profile():
    return render_template('business_profile.html')

@app.route("/api/business_config", methods=['GET', 'POST'])
def api_business_config():
    global business_config
    if request.method == 'POST':
        data = request.json
        business_config.update(data)
        save_business_config()
        return jsonify({"status": "success"})
    return jsonify(business_config)

@app.route("/dashboard", methods=['GET'])
@app.route("/pickup/dashboard", methods=['GET'])
def dashboard():
    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M:%S")
    return render_template('dashboard.html', history=pickup_history, now=now_str)

@app.route("/billboard", methods=['GET'])
@app.route("/pickup/billboard", methods=['GET'])
def billboard():
    return render_template('billboard.html')

@app.route("/liff/gps", methods=['GET'])
def liff_gps():
    return render_template('liff_gps.html')

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

@app.route("/api/history", methods=['GET'])
@app.route("/pickup/api/history", methods=['GET'])
def get_full_history():
    # Return 1 week log
    return jsonify(activity_log), 200

@app.route("/history", methods=['GET'])
@app.route("/pickup/history", methods=['GET'])
def history_page():
    return render_template('history.html')

@app.route("/get_audio/<filename>", methods=['GET'])
@app.route("/pickup/get_audio/<filename>", methods=['GET'])
def get_audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if os.path.exists(path):
        resp = send_file(path, mimetype="audio/mpeg")
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    return "No audio found", 404

# --- Relay Cloud Command APIs (Cloud Side) ---
@app.route("/api/relay/send", methods=['POST'])
def api_relay_send():
    """ 接收來自 Web (Billboard/Dashboard) 的繼電器指令 """
    data = request.json
    if not data: return jsonify(ok=False), 400
    ch = data.get("ch")
    on = data.get("on", True)
    if ch in (1,2,3,4):
        pending_relay_commands.append({"ch": ch, "on": on})
        relay_states[ch] = on # 🌟 Optimistic Update for instant UI sync
        logger.info(f"☁️ [雲端指令暫存] Relay {ch} -> {'ON' if on else 'OFF'}")
        return jsonify(ok=True)
    return jsonify(ok=False), 400

@app.route("/api/relay/get", methods=['GET'])
def api_relay_get():
    """ 讓本地端程式來「領取」待執行的指令 """
    global pending_relay_commands
    cmds = list(pending_relay_commands)
    pending_relay_commands = [] # 領完即焚
    if cmds:
        logger.info(f"📤 [雲端發送指令] 已將 {len(cmds)} 條指令發送至本地 Agent 執行")
    return jsonify(cmds)

@app.route("/api/relay/status", methods=['GET'])
def api_relay_status():
    """ 讓前端網頁獲取目前的繼電器狀態 """
    return jsonify(relay_states)

@app.route("/api/relay/update_status", methods=['POST'])
def api_relay_update_status():
    """ 讓本地端回報目前的實體繼電器狀態至雲端 """
    global relay_states
    data = request.json
    if data and isinstance(data, dict):
        for k, v in data.items():
            try:
                ch = int(k)
                if ch in (1,2,3,4):
                    relay_states[ch] = bool(v)
            except: pass
        logger.info(f"📡 [狀態同步] 雲端狀態已更新: {relay_states}")
        return jsonify(ok=True)
    return jsonify(ok=False), 400

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
    global pending_relay_commands, pickup_history, activity_log
    msg_text = event.message.text.strip()
    user_id = event.source.user_id
    logger.info(f"📩 [收到 LINE 訊息] From: {user_id[-5:]} Text: '{msg_text}'")
    
    # 🌟 Priority 1: Handle Keywords (Never Broadcast, Guide only)
    help_keywords = ["幫助", "註冊", "？", "?", "選單", "身分", "身份", "指南", "Help", "格式", "王小明", "電話", "聯絡中心", "Menu", "menu", "官方選單"]
    if any(k in msg_text for k in help_keywords):
        # Specific Handle for Phone
        if "電話" in msg_text or "聯絡中心" in msg_text:
            line_reply(event.reply_token, f"🏫 學校的電話號碼：{school_phone}")
        else:
            line_reply(event.reply_token, get_help_text())
        return

    # 🌟 Priority 1.5: Relay Control Keywords (#relay1 on/off)
    m_lower = msg_text.lower()
    relay_prefixes = ("#relay", "＃relay", "#繼電器", "＃繼電器", "#開關", "＃開關", "relay", "繼電器", "開關")
    
    match_prefix = next((p for p in relay_prefixes if m_lower.startswith(p)), None)
    if match_prefix:
        logger.info(f"⚡ [命中繼電器指令] Prefix: {match_prefix}")
        # Get everything after the prefix (e.g. "1 on" from "#relay1 on")
        cmd = m_lower[len(match_prefix):].strip() 
        try:
            parts = cmd.split()
            if not parts: return
            
            # 支援 "#relay all on" 或 "#relay all off"
            if parts[0] == "all":
                action = parts[1] if len(parts) > 1 else "on"
                is_on = action in ("on", "open", "啟動", "開", "開啓", "開啟")
                for ch in (1, 2, 3, 4):
                    if os.environ.get("RENDER"):
                        pending_relay_commands.append({"ch": ch, "on": is_on})
                        relay_states[ch] = is_on # 🌟 Optimistic Update
                    else:
                        control_usb_relay4(ch, is_on)
                status_text = "一鍵開啟" if is_on else "一鍵關閉"
                line_reply(event.reply_token, f"⚡ [批量指令] {status_text}所有設備中")
                return

            ch_num = int(parts[0])
            # Action: 如果有第二個詞則用它，否則執行 Toggle
            if len(parts) > 1:
                action = parts[1]
                is_on = action in ("on", "open", "啟動", "開", "啟", "開啓", "開啟")
            else:
                # Toggle logic: 根據目前 relay_states 狀態翻轉
                is_on = not relay_states.get(ch_num, False)
            
            # If on Render, buffer the command for local agent
            if os.environ.get("RENDER"):
                logger.info(f"☁️ [雲端模式] 正在暫存指令: {ch_num} -> {is_on}")
                pending_relay_commands.append({"ch": ch_num, "on": is_on})
                relay_states[ch_num] = is_on # 🌟 Optimistic Update
                r_name = RELAY_NAMES.get(ch_num, f"繼電器 {ch_num}")
                line_reply(event.reply_token, f"☁️ [雲端] {r_name} -> {'開啟' if is_on else '關閉'}")
            else:
                logger.info(f"🏠 [本地模式] 正在執行指令: {ch_num} -> {is_on}")
                if control_usb_relay4(ch_num, is_on):
                    r_name = RELAY_NAMES.get(ch_num, f"繼電器 {ch_num}")
                    status_text = "啟動" if is_on else "關閉"
                    line_reply(event.reply_token, f"✅ {r_name} {status_text}成功")
                else:
                    line_reply(event.reply_token, f"❌ {RELAY_NAMES.get(ch_num, 'Relay')} 失敗")
            return


        except (ValueError, IndexError): pass

    # 🌟 Priority 1.6: Weather Trigger (#天氣)
    if msg_text.startswith("#天氣") or msg_text.startswith("＃天氣") or m_lower == "weather":
        report = _get_weather_report()
        line_reply(event.reply_token, f"🌤️ 目前氣象資訊：\n\n{report}")
        # 同時語音播報
        audio_filename = f"weather_{int(time.time())}.mp3"
        audio_full_path = os.path.join(AUDIO_DIR, audio_filename)
        speech_queue.put((report, audio_full_path))
        return
        
    # 2. Handle Name Registration (#NewName)
    if msg_text.startswith("#") or msg_text.startswith("＃"):
        reg_info = msg_text[1:].strip()
        if reg_info == "取消註冊":
            if user_id in PARENTS_DB:
                del PARENTS_DB[user_id]
                save_parents_db()
                line_reply(event.reply_token, "🗑️ 已成功取消您的家長註冊。")
            return
        elif reg_info:
            # 支援 名字+車號 格式
            new_name, car_plate = reg_info, ""
            if "+" in reg_info:
                parts = reg_info.split("+", 1)
                new_name, car_plate = parts[0].strip(), parts[1].strip()
            elif "＋" in reg_info:
                parts = reg_info.split("＋", 1)
                new_name, car_plate = parts[0].strip(), parts[1].strip()
            
            PARENTS_DB[user_id] = {"name": new_name, "plate": car_plate}
            save_parents_db()
            msg = f"🎉 註冊成功！\n\n您的廣播識別為：【{new_name}】"
            if car_plate:
                msg += f"\n已登錄車號：【{car_plate}】"
            else:
                msg += "\n(目前未曾登錄車號，可輸入 #名字+車號 更新)"
            line_reply(event.reply_token, f"{msg}\n\n現在您可以點選下方選單開始呼叫孩子囉！")
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

    if user_id not in PARENTS_DB:
        line_reply(event.reply_token, get_help_text())
        return
        
    parent_info = PARENTS_DB[user_id]
    if isinstance(parent_info, dict):
        parent_name = parent_info.get("name", "家長")
        car_plate = parent_info.get("plate", "")
    else:
        # 兼容舊的純字串格式
        parent_name = parent_info
        car_plate = ""

    if parent_name.startswith("[BANNED]"):
        line_reply(event.reply_token, "⚠️ 您目前已被管理員限制廣播功能。")
        return
    s_text, s_label, s_class = msg_text, "通知", "type-soon"
    if "已到達" in msg_text: 
        s_text, s_label, s_class = "已到達校門口，請儘快前往大門。", "已到達校門", "type-arrived"
        # 自動啟動擴大機 (Relay 1)
        control_usb_relay4(1, True)
    elif "即將到達" in msg_text: s_text, s_label, s_class = "預計 5 分鐘內即將到達。", "即將到達", "type-soon"
    elif "接走" in msg_text or "接到孩子" in msg_text: s_text, s_label, s_class = "已接到孩子，謝謝老師。", "已接到孩子", "type-thanks"
    elif "晚點到" in msg_text: s_text, s_label, s_class = "會晚點到，請老師知悉。", "會晚點到", "type-soon"
    pickup_history = [h for h in pickup_history if h["name"] != parent_name]
    
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    now_date = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M:%S")

    # Filename for audio
    audio_filename = f"audio_{int(time.time())}_{user_id[-5:]}.mp3"
    audio_full_path = os.path.join(AUDIO_DIR, audio_filename)

    entry = {
        "name": parent_name,
        "plate": car_plate,
        "status": s_label, 
        "date": now_date,
        "time": now_time, 
        "class": s_class, 
        "speech_text": f"{parent_name} {s_text}", 
        "audio_url": f"/get_audio/{audio_filename}"
    }
    
    # 1. Update Billboard (Fast list)
    pickup_history.insert(0, entry)
    if len(pickup_history) > 30: pickup_history.pop()
    
    # 2. Update Persistant Log (Long list)
    activity_log.append(entry)
    # Remove logs older than 7 days
    seven_days_ago = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    activity_log = [l for l in activity_log if l.get("date", "0000-00-00") >= seven_days_ago]
    save_activity_log()

    speech_queue.put((f"{parent_name} {s_text}", audio_full_path))
    line_reply(event.reply_token, f"📢 已廣播：{parent_name} {s_text}")

    # Background audio cleaning
    def clean_old():
        now_ts = time.time()
        for f in os.listdir(AUDIO_DIR):
            p = os.path.join(AUDIO_DIR, f)
            if os.path.isfile(p) and os.stat(p).st_mtime < now_ts - 3600:
                try: os.remove(p)
                except: pass
    threading.Thread(target=clean_old, daemon=True).start()

# --- Local Relay Poller (Local Side) ---
def local_relay_poller():
    """ 只有在本地模式執行：向雲端輪詢是否有繼電器指令，並同步回報狀態 """
    if os.environ.get("RENDER"): return # 雲端環境不執行此輪詢
    
    logger.info(f"🛰️ [遠端控制啟動] 開始向雲端輪詢繼電器指令 ({CLOUD_URL})...")
    import requests
    
    last_sync_time = 0
    last_keep_alive = 0
    
    while True:
        try:
            now = time.time()
            # 每 60 秒印一次測試連線 log，方便使用者確認程式沒當掉
            if now - last_keep_alive > 60:
                logger.info(f"🟢 [控制輪詢中] 測試連線正常, 目標: {CLOUD_URL}")
                last_keep_alive = now

            # 1. 向雲端「領取」待執行的指令
            r = requests.get(f"{CLOUD_URL}/api/relay/get", timeout=5)
            executed_any = False
            if r.status_code == 200:
                cmds = r.json()
                for c in cmds:
                    ch = c.get("ch")
                    on = c.get("on")
                    logger.info(f"⚡ [收到雲端指令] 控制繼電器 {ch} 為 {'ON' if on else 'OFF'}")
                    if control_usb_relay4(ch, on):
                        executed_any = True
            
            # 2. 如果之前執行過指令，或每隔 30 秒，主動同步狀態回雲端
            if executed_any or (now - last_sync_time > 30):
                # 準備狀態資料 (JSON 只支援字串作為 Key)
                sync_data = {str(k): v for k, v in relay_states.items()}
                requests.post(f"{CLOUD_URL}/api/relay/update_status", json=sync_data, timeout=5)
                last_sync_time = now
        except Exception as e:
            logger.error(f"⚠️ [輪詢異常] 雲端連線失敗: {e}")
            time.sleep(5) # 發生錯誤時稍微等待
        time.sleep(1.2) # 輪詢頻率

threading.Thread(target=local_relay_poller, daemon=True).start()

@handler.add(FollowEvent)
def handle_follow(event):
    line_reply(event.reply_token, f"👋 您好！歡迎使用【學生接送廣播系統】。\n\n{get_help_text()}")

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    if user_id not in PARENTS_DB:
        line_reply(event.reply_token, get_help_text())
        return
    
    # Process postback data (if any)
    data = event.postback.data
    # For now, we treat postback data similarly to messages if they trigger a broadcast
    handle_message(event)

# --- Desktop UI Implementation ---
def run_app():
    import os
    # Detect if running on Render (Cloud Server)
    is_render = os.environ.get("RENDER") is not None
    port = int(os.environ.get("PORT", 5000))
    
    # Load voice config from cloud on startup
    load_voice_config()
    
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
        if webview:
            webview.create_window(
                '🏫 學生接送智慧監控中心', 
                f'http://127.0.0.1:{port}/dashboard', 
                js_api=desktop_api,
                width=1024, height=768, confirm_close=True
            )
            webview.start()

if __name__ == "__main__":
    run_app()
