import os
import json
import asyncio
import subprocess
import threading
import edge_tts
import logging
import base64
import queue
import time
import time
import datetime
from flask import Flask, request, abort, jsonify, render_template_string
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, PostbackEvent
from dotenv import load_dotenv

# Load variables from .env if available
load_dotenv()

# Set up logging for easier debugging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration ---
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '9825dc29feb8522d4fc1e273411d8f37')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')

handler = WebhookHandler(CHANNEL_SECRET)

# Voice for Edge TTS
VOICE = "zh-TW-HsiaoChenNeural"
VOICE_RATE = "+0%"   # 速度：例如 "+20%" 為快20%，"-10%" 為慢10%
VOICE_VOLUME = "+0%" # 音量：例如 "+50%" 為大聲50%
TEMP_AUDIO = "output.mp3"

# --- Database Management ---
PARENTS_FILE = "parents.json"

def load_parents_db():
    if os.path.exists(PARENTS_FILE):
        try:
            with open(PARENTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading parents DB: {e}")
    return {}

def save_parents_db():
    try:
        with open(PARENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(PARENTS_DB, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Error saving parents DB: {e}")

# 初始化資料庫
PARENTS_DB = load_parents_db()

HELP_TEXT = (
    "🛑 【重要通知：您尚未完成註冊】\n\n"
    "在使用接送廣播功能前，請務必先完成註冊：\n"
    "--------------------------\n"
    "✍️ 註冊方式：直接回覆 #名字\n"
    "範例：#一年二班王小明爸爸\n"
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

# --- Pickup Monitoring Data ---
pickup_history = [] # 儲存 { "name": str, "status": str, "time": str, "type": str }

# --- Speech Queue System (防止語音重疊) ---
speech_queue = queue.Queue()

def speech_worker():
    """ 背景執行緒：依序從隊列中取出文字並廣播 """
    while True:
        text = speech_queue.get()
        if text is None: break
        
        # 建立一個新的事件循環來執行非同步的 TTS
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(generate_and_play_speech(text))
        finally:
            loop.close()
        
        speech_queue.task_done()
        time.sleep(0.5) # 兩段廣播之間的短暫停頓

# 啟動語音背景執行緒
threading.Thread(target=speech_worker, daemon=True).start()

async def generate_and_play_speech(text):
    """
    Generate speech using edge-tts and play it via mpv.
    """
    try:
        logger.info(f"🔊 Announcement starting: {text} (rate={VOICE_RATE}, vol={VOICE_VOLUME})")
        # 加入速度與音量控制
        communicate = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, volume=VOICE_VOLUME)
        await communicate.save(TEMP_AUDIO)
        
        # Play the audio using mpv
        subprocess.run(["mpv", "--no-video", TEMP_AUDIO], check=True)
        logger.info(f"✅ Announcement finished: {text}")
    except Exception as e:
        logger.error(f"❌ TTS Error: {e}")

def speak_in_background(text):
    """
    將文字加入語音排隊隊列。
    """
    logger.info(f"📝 Adding to speech queue: {text}")
    speech_queue.put(text)

# --- LINE Webhook Handler ---

@app.route("/status", methods=['GET'])
def status_check():
    return jsonify({"status": "running", "engine": "Edge-TTS", "handler": "LINE-SDK-v3", "records": len(pickup_history)}), 200

@app.route("/dashboard", methods=['GET'])
def dashboard():
    """ 導護老師查閱介面 """
    template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>🚗 學生接送監控系統</title>
        <meta http-equiv="refresh" content="10"> <!-- 每10秒自動更新 -->
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: 'Segoe UI', sans-serif; background: #f4f7f6; margin: 0; padding: 20px; color: #333; }
            .container { max-width: 800px; margin: auto; }
            h1 { text-align: center; color: #1a73e8; font-size: 24px; margin-bottom: 20px; }
            .card { background: white; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 15px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; border-left: 8px solid #ccc; transition: all 0.3s; }
            .time { color: #888; font-size: 14px; }
            .name { font-size: 18px; font-weight: bold; }
            .status { padding: 5px 12px; border-radius: 20px; font-size: 14px; font-weight: bold; color: white; }
            
            /* 不同類型的狀態顏色 */
            .type-arrived { border-left-color: #d93025; } /* 已到達: 紅色 */
            .type-arrived .status { background: #d93025; }
            
            .type-soon { border-left-color: #f29900; } /* 即將到達: 橘色 */
            .type-soon .status { background: #f29900; }
            
            .type-thanks { border-left-color: #1e8e3e; } /* 已接到: 綠色 */
            .type-thanks .status { background: #1e8e3e; }
            
            .empty { text-align: center; padding: 50px; color: #aaa; }
            .refresh-hint { text-align: center; font-size: 12px; color: #999; margin-top: 20px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚗 學生接送即時看板</h1>
            {% if history %}
                {% for item in history %}
                <div class="card {{ item.class }}">
                    <div>
                        <div class="name">{{ item.name }}</div>
                        <div class="time">🕒 通知時間：{{ item.time }}</div>
                    </div>
                    <div class="status">{{ item.status }}</div>
                </div>
                {% endfor %}
            {% else %}
                <div class="card empty">目前沒有接送紀錄。</div>
            {% endif %}
            <div class="refresh-hint">系統會自動重新整理... (上次更新：{{ now }})</div>
        </div>
    </body>
    </html>
    """
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    return render_template_string(template, history=pickup_history, now=now_str)

# Using strict_slashes=False to support both /pickup and /pickup/
@app.route("/pickup", methods=['POST'], strict_slashes=False)
def callback():
    logger.debug(f"Received request headers: {request.headers}")
    
    # get X-Line-Signature header value
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        logger.warning("Webhook received without signature (manual test).")
        data = request.json
        if data and 'name' in data:
            speak_in_background(f"{data['name']} 家長已到達。")
        return 'OK', 200

    # get request body as text
    body = request.get_data(as_text=True)
    logger.debug(f"Request body received (length={len(body)})")

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Check your channel secret.")
        abort(400)

    return 'OK', 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg_text = event.message.text.strip()
    user_id = event.source.user_id
    logger.info(f"Received LINE Message from {user_id}: {msg_text}")
    
    # 1. 處理註冊指令 (以 # 開頭)
    if msg_text.startswith("#") or msg_text.startswith("＃"):
        new_name = msg_text[1:].strip()
        if new_name:
            PARENTS_DB[user_id] = new_name
            save_parents_db()
            line_reply(event.reply_token, f"✅ 註冊成功！\n身分已設定為：【{new_name}】\n\n現在您可以直接傳送訊息（如：已到達）來進行廣播了。")
            logger.info(f"New registration: {user_id} -> {new_name}")
        else:
            line_reply(event.reply_token, "請在 # 後方輸入您的身份內容。\n範例：#一年一班王小明爸爸")
        return

    # 2. 檢查使用者是否已註冊
    if user_id not in PARENTS_DB:
        logger.warning(f"Unregistered user {user_id} tried to send message.")
        line_reply(event.reply_token, HELP_TEXT)
        return

    # 3. 已註冊使用者：執行廣播
    parent_name = PARENTS_DB[user_id]
    
    # 組合語音文字與儀表板標籤
    speech_text = msg_text
    status_label = "通知"
    card_class = "type-soon" # 預設橘色
    
    if "已到達" in msg_text or "已到" in msg_text or msg_text == "已到達":
        status_label = "已到達校門"
        card_class = "type-arrived" # 紅色
        speech_text = "已到達。"
    elif "即將" in msg_text:
        status_label = "即將到達"
        card_class = "type-soon" # 橘色
    elif "接到" in msg_text or "謝謝" in msg_text:
        status_label = "已接到孩子"
        card_class = "type-thanks" # 綠色

    # 新增到歷史紀錄 (保留最近 30 筆)
    now_time = datetime.datetime.now().strftime("%H:%M")
    pickup_history.insert(0, {
        "name": parent_name,
        "status": status_label,
        "time": now_time,
        "class": card_class
    })
    if len(pickup_history) > 30:
        pickup_history.pop()

    announcement = f"{parent_name} {speech_text}"
    
    # 加入排隊廣播
    speak_in_background(announcement)
    
    # 確認回覆
    line_reply(event.reply_token, f"📢 廣播中：\n「{announcement}」")

@handler.add(FollowEvent)
def handle_follow(event):
    """當使用者加入好友時，主動發送註冊說明"""
    user_id = event.source.user_id
    logger.info(f"New Follower: {user_id}")
    
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
    logger.info(f"Postback received from {user_id}: {data}")

    if user_id not in PARENTS_DB:
        line_reply(event.reply_token, HELP_TEXT)
        return

    # 若已註冊，則將 postback data 當作文字訊息處理
    event.message = type('obj', (object,), {'text': data})
    handle_message(event)

if __name__ == "__main__":
    if os.path.exists(TEMP_AUDIO):
        try: os.remove(TEMP_AUDIO)
        except: pass
        
    logger.info(f"Student Pickup Server (LINE Webhook v3) started on port 5000")
    logger.info(f"Endpoint: /pickup (strict_slashes=False)")
    app.run(host='0.0.0.0', port=5000, debug=False)
