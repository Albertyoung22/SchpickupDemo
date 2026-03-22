import os
import sys
import json
import asyncio
import subprocess
import threading
import edge_tts
import logging
import queue
import time
import datetime
import webbrowser
import tkinter as tk
from tkinter import ttk
import ctypes
from flask import Flask, request, abort, jsonify, render_template, send_file
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from dotenv import load_dotenv
from flask_cors import CORS

# Load variables from .env
load_dotenv()

# --- Configuration & Globals ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PickupMasterUnified")

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app) # 允許網頁跨網域存取資料庫與音檔
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '9825dc29feb8522d4fc1e273411d8f37')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')
handler = WebhookHandler(CHANNEL_SECRET)

# --- Windows 任務欄圖示強制註冊 ---
if sys.platform == 'win32':
    try:
        myappid = 'school.pickup.master.v1'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except:
        pass

# 建立絕對路徑的音效資料夾
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
if not os.path.exists(AUDIO_DIR): os.makedirs(AUDIO_DIR)

VOICE_OPTIONS = {
    "曉曉 (最溫柔)": "zh-CN-XiaoxiaoNeural",
    "雲希 (最親切)": "zh-CN-YunxiNeural",
    "曉臻 (台灣腔)": "zh-TW-HsiaoChenNeural",
    "雲哲 (台灣腔男)": "zh-TW-YunJheNeural",
    "雲希 (台灣腔男)": "zh-TW-YunxiNeural"
}

# 全域變數，由 GUI 初始化
VOICE_NAME = None 
VOICE_RATE = None  
VOICE_VOLUME = None 
TEMP_AUDIO = "output.mp3"
PARENTS_FILE = "parents.json"
PARENTS_DB = {}
pickup_history = []
speech_queue = queue.Queue()

def load_parents_db():
    global PARENTS_DB
    if os.path.exists(PARENTS_FILE):
        try:
            with open(PARENTS_FILE, "r", encoding="utf-8") as f:
                PARENTS_DB = json.load(f)
        except: PARENTS_DB = {}
    else: PARENTS_DB = {}

def save_parents_db():
    try:
        with open(PARENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(PARENTS_DB, f, ensure_ascii=False, indent=4)
    except: pass

load_parents_db()

# --- Speech worker thread ---
async def generate_and_play_speech(text, v, r, vol, audio_path):
    try:
        communicate = edge_tts.Communicate(text, v, rate=r, volume=vol)
        await communicate.save(audio_path)
        subprocess.run(["mpv", "--no-video", audio_path], check=True)
    except Exception as e:
        logger.error(f"TTS/MPV Error: {e}")

def speech_worker_thread():
    while True:
        task = speech_queue.get()
        if task is None: break
        text, audio_path = task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            v_display = VOICE_NAME.get() if VOICE_NAME else "曉臻 (台灣腔)"
            voice_code = VOICE_OPTIONS.get(v_display, "zh-TW-HsiaoChenNeural")
            rate = VOICE_RATE.get() if VOICE_RATE else "+0%"
            vol = VOICE_VOLUME.get() if VOICE_VOLUME else "+0%"
            loop.run_until_complete(generate_and_play_speech(text, voice_code, rate, vol, audio_path))
        finally:
            loop.close()
        speech_queue.task_done()

threading.Thread(target=speech_worker_thread, daemon=True).start()

# --- Web Routes (雙重支援，確保 RenderBridge 穩定) ---
@app.route("/dashboard", methods=['GET'])
@app.route("/pickup/dashboard", methods=['GET'])
def dashboard():
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    return render_template('dashboard.html', history=pickup_history, now=now_str)

@app.route("/billboard", methods=['GET'])
@app.route("/pickup/billboard", methods=['GET'])
def billboard():
    return render_template('billboard.html')

@app.route("/get_audio/<filename>", methods=['GET'])
@app.route("/pickup/get_audio/<filename>", methods=['GET'])
def get_audio(filename):
    """ 讓網頁看板抓取指定的廣播音檔 """
    path = os.path.join(AUDIO_DIR, filename)
    if os.path.exists(path):
        logger.info(f"Serving audio: {filename}")
        resp = send_file(path, mimetype="audio/mpeg")
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    logger.error(f"Audio NOT Found: {filename}")
    return "No audio", 404

@app.route("/api/poll", methods=['GET'])
@app.route("/pickup/api/poll", methods=['GET'])
def api_poll():
    """ 讓網頁看板輪詢最新狀態 """
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    return jsonify({
        "history": pickup_history,
        "now": now_str
    }), 200

@app.route("/pickup/api/clear_parent", methods=['POST'])
def clear_parent():
    """ 老師手動清除已接走的家長 """
    data = request.json
    target_name = data.get("name")
    if not target_name: return "No name", 400
    
    global pickup_history
    pickup_history = [h for h in pickup_history if h["name"] != target_name]
    if gui_monitor: gui_monitor.remove_from_list(target_name)
    
    logger.info(f"Teacher cleared: {target_name}")
    return "OK", 200

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
    
    def reply(txt):
        if CHANNEL_ACCESS_TOKEN:
            try:
                configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=txt)]))
            except: pass

    # --- 智慧註冊導引與注意事項 ---
    HELP_TEXT = (
        "🛑 【重要通知：您尚未完成註冊】\n\n"
        "在使用接送廣播功能前，請務必先完成「身份註冊」：\n"
        "--------------------------\n"
        "✍️ 註冊方式：直接回覆 #名字\n"
        "範例：#三年二班王小明爸爸\n"
        "--------------------------\n\n"
        "⚠️ 【使用注意事項】：\n"
        "1. 您的廣播將由校門口語音大聲讀出，請勿輸入非必要或過於頻繁的訊息。\n"
        "2. 一個 LINE 帳號目前僅能註冊一名領回者身份。\n"
        "3. 系統主要用於到校門口時的提醒，請避免在遠處過早點擊「已到達」。\n"
        "4. 如有操作困難，請諮詢校門口導護老師或聯繫總務處。"
    )

    # 1. Registration
    if msg_text.startswith("#") or msg_text.startswith("＃"):
        new_name = msg_text[1:].strip()
        if new_name:
            PARENTS_DB[user_id] = new_name
            save_parents_db()
            reply(f"🎉 註冊成功！\n\n您的廣播名稱為：【{new_name}】\n\n現在您可以點選選單，告訴孩子您快到校門口囉！")
            if gui_monitor: gui_monitor.update_parents_count()
        return

    # 3. Help/Intro (註冊教學/身分註冊按鈕 - 不廣播)
    if msg_text in ["幫助", "註冊", "？", "?", "選單", "身分註冊", "身份註冊"] or "請輸入格式" in msg_text:
        reply(HELP_TEXT)
        return

    # 4. Contact Center (學校電話按鈕 - 不廣播)
    if "學校的電話號碼" in msg_text or "聯絡" in msg_text or "電話" in msg_text:
        school_phone = "02-1234-5678"
        reply(f"📞 學校官方聯絡電話：\n{school_phone}\n\n(點擊號碼後即可直接撥號聯繫學校櫃台。)")
        return

    # 5. Check Valid (尚未註冊的阻擋)
    if user_id not in PARENTS_DB:
        reply(f"⚠️ 偵測到您尚未完成註冊喔！\n\n{HELP_TEXT}")
        return

    # 6. Process (對齊選單文字的廣播邏輯)
    parent_name = PARENTS_DB[user_id]
    s_text, s_label, s_class = msg_text, "通知", "type-soon"
    
    if "已到達" in msg_text:
        s_text, s_label, s_class = "已到達校門口，請儘快前往大門。", "已到達校門", "type-arrived"
    elif "即將到達" in msg_text:
        s_text, s_label, s_class = "預計 5 分鐘內即將到達。", "即將到達", "type-soon"
    elif "會晚點到" in msg_text:
        s_text, s_label, s_class = "會晚一點點到達，請耐心等候。", "會晚點到", "type-soon"
    elif "接到孩子" in msg_text or "謝謝老師" in msg_text:
        s_text, s_label, s_class = "已接到孩子，謝謝老師。", "已接到孩子", "type-thanks"

    global pickup_history
    # 先除舊
    pickup_history = [h for h in pickup_history if h["name"] != parent_name]
    if gui_monitor: gui_monitor.remove_from_list(parent_name)
    
    now_time = datetime.datetime.now().strftime("%H:%M:%S")
    
    # 建立該則訊息專屬的音檔檔案
    audio_filename = f"audio_{int(time.time())}.mp3"
    audio_full_path = os.path.join(AUDIO_DIR, audio_filename)
    
    entry = {
        "name": parent_name, 
        "status": s_label, 
        "time": now_time, 
        "class": s_class,
        "speech_text": f"{parent_name} {s_text}",
        "audio_url": f"/get_audio/{audio_filename}?t={int(time.time())}"
    }
    
    # 紀錄 (非「已接到」才加入看板，已接到的維持留在歷史)
    pickup_history.insert(0, entry)
    if len(pickup_history) > 30: pickup_history.pop()
    
    if gui_monitor: gui_monitor.add_to_list(entry)
    
    # 廣播 (將音檔路徑傳入)
    speech_queue.put((f"{parent_name} {s_text}", audio_full_path))
    reply(f"📢 已廣播：{parent_name} {s_text}")
    
    # 清理一小時前過期的音效檔案 (背景工作)
    def clean_old_audio():
        now = time.time()
        for f in os.listdir(AUDIO_DIR):
            fpath = os.path.join(AUDIO_DIR, f)
            if os.stat(fpath).st_mtime < now - 3600:
                try: os.remove(fpath)
                except: pass
    threading.Thread(target=clean_old_audio, daemon=True).start()

# --- Desktop GUI Master ---
class PickupMasterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("👋 學生接送智慧監控中心 (Unified V4)")
        self.root.geometry("750x650")
        self.root.configure(bg="#f8f9fa")
        
        global VOICE_NAME, VOICE_RATE, VOICE_VOLUME
        VOICE_NAME = tk.StringVar(value="曉臻 (台灣腔)")
        VOICE_RATE = tk.StringVar(value="+0%")
        VOICE_VOLUME = tk.StringVar(value="+0%")
        
        self.setup_ui()
        self.update_parents_count()
        self.start_server()

    def setup_ui(self):
        # --- 設置程式圖示 ---
        try:
            icon_path = os.path.join(BASE_DIR, "static", "images", "sch_pickup.ico")
            if os.path.exists(icon_path):
                # 方案 A: 窗口圖標
                self.icon_img = tk.PhotoImage(file=os.path.join(BASE_DIR, "static", "images", "sch_pickup_logo.png"))
                self.root.iconphoto(True, self.icon_img)
                # 方案 B: Windows 任務欄高品質圖標 (必需是 .ico)
                self.root.iconbitmap(icon_path)
        except Exception as e:
            logger.error(f"Failed to load professional icon: {e}")

        # Header
        header = tk.Label(self.root, text="🏫 學生接送智慧中心", font=("Microsoft JhengHei", 20, "bold"), bg="#1a73e8", fg="white", pady=15)
        header.pack(fill=tk.X)

        main_frame = tk.Frame(self.root, bg="#f8f9fa", padx=25, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Status Indicators
        top_row = tk.Frame(main_frame, bg="#f8f9fa")
        top_row.pack(fill=tk.X, pady=5)
        self.parent_count_lbl = tk.Label(top_row, text="已註冊家長: 0", font=("Microsoft JhengHei", 11), bg="#e8f0fe", fg="#1967d2", padx=15, pady=8)
        self.parent_count_lbl.pack(side=tk.LEFT)
        self.server_status_lbl = tk.Label(top_row, text="● 伺服器啟動中...", font=("Microsoft JhengHei", 10), bg="#f8f9fa", fg="#f29900")
        self.server_status_lbl.pack(side=tk.RIGHT)

        # Quick Launch Buttons
        btn_frame = tk.Frame(main_frame, bg="#f8f9fa")
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        # 公開網址 (RenderBridge)
        tk.Button(btn_frame, text="🚀 開啟大螢幕看板 (Billboard)", font=("Microsoft JhengHei", 10, "bold"), bg="#ff9500", fg="white", padx=15, pady=5, command=lambda: webbrowser.open("https://renderbridge.onrender.com/pickup/billboard")).pack(side=tk.LEFT, padx=(0, 10))
        tk.Button(btn_frame, text="📋 開啟導護儀表板 (Dashboard)", font=("Microsoft JhengHei", 10, "bold"), bg="#34a853", fg="white", padx=15, pady=5, command=lambda: webbrowser.open("https://renderbridge.onrender.com/pickup/dashboard")).pack(side=tk.LEFT)

        # Settings
        ctrl_frame = tk.LabelFrame(main_frame, text="播報參數調整", font=("Microsoft JhengHei", 10, "bold"), bg="white", padx=20, pady=15)
        ctrl_frame.pack(fill=tk.X, pady=15)

        # Voice
        tk.Label(ctrl_frame, text="播報角色 (Voice):", bg="white").grid(row=0, column=0, pady=5)
        self.voice_combo = ttk.Combobox(ctrl_frame, textvariable=VOICE_NAME, values=list(VOICE_OPTIONS.keys()), state="readonly")
        self.voice_combo.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        # Labels PRE-CREATION to avoid callback error
        self.rate_val_lbl = tk.Label(ctrl_frame, text="+0%", bg="white", font=("Consolas", 10, "bold"), width=6)
        self.rate_val_lbl.grid(row=1, column=2, padx=5)
        self.vol_val_lbl = tk.Label(ctrl_frame, text="+0%", bg="white", font=("Consolas", 10, "bold"), width=6)
        self.vol_val_lbl.grid(row=2, column=2, padx=5)

        # Rate Slider
        tk.Label(ctrl_frame, text="速度 (Rate):", bg="white").grid(row=1, column=0, pady=5)
        self.rate_slider = ttk.Scale(ctrl_frame, from_=-50, to=100, orient=tk.HORIZONTAL, command=self.on_rate_change)
        self.rate_slider.set(0)
        self.rate_slider.grid(row=1, column=1, sticky="ew", padx=(10, 0))

        # Volume Slider
        tk.Label(ctrl_frame, text="音量 (Volume):", bg="white").grid(row=2, column=0, pady=5)
        self.vol_slider = ttk.Scale(ctrl_frame, from_=-50, to=100, orient=tk.HORIZONTAL, command=self.on_vol_change)
        self.vol_slider.set(0)
        self.vol_slider.grid(row=2, column=1, sticky="ew", padx=(10, 0))
        ctrl_frame.columnconfigure(1, weight=1)

        # Main List
        tk.Label(main_frame, text="⏳ 即時接送動態:", font=("Microsoft JhengHei", 11, "bold"), bg="#f8f9fa").pack(anchor=tk.W, pady=(5,5))
        columns = ("time", "name", "status")
        self.tree = ttk.Treeview(main_frame, columns=columns, show="headings", height=10)
        self.tree.heading("time", text="時間")
        self.tree.heading("name", text="家長姓名")
        self.tree.heading("status", text="當前狀態")
        self.tree.column("time", width=80, anchor=tk.CENTER)
        self.tree.column("name", width=350)
        self.tree.column("status", width=120, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)

        self.tree.tag_configure('type-arrived', background='#ffebee', foreground='#b71c1c')
        self.tree.tag_configure('type-soon', background='#fff3e0', foreground='#e65100')
        self.tree.tag_configure('type-thanks', background='#e8f5e9', foreground='#1b5e20')

    def on_rate_change(self, val):
        i = int(float(val))
        txt = f"+{i}%" if i >= 0 else f"{i}%"
        VOICE_RATE.set(txt)
        if hasattr(self, 'rate_val_lbl'): self.rate_val_lbl.config(text=txt)

    def on_vol_change(self, val):
        i = int(float(val))
        txt = f"+{i}%" if i >= 0 else f"{i}%"
        VOICE_VOLUME.set(txt)
        if hasattr(self, 'vol_val_lbl'): self.vol_val_lbl.config(text=txt)

    def update_parents_count(self):
        self.root.after(0, lambda: self.parent_count_lbl.config(text=f"已註冊家長: {len(PARENTS_DB)} 位"))

    def add_to_list(self, entry):
        self.root.after(0, lambda: self._add_to_tree(entry))

    def _add_to_tree(self, entry):
        self.tree.insert("", 0, values=(entry["time"], entry["name"], entry["status"]), tags=(entry["class"],))
        if len(self.tree.get_children()) > 30: self.tree.delete(self.tree.get_children()[-1])

    def remove_from_list(self, name):
        self.root.after(0, lambda: self._remove_by_name(name))

    def _remove_by_name(self, name):
        for item in self.tree.get_children():
            if self.tree.item(item)["values"][1] == name:
                self.tree.delete(item)

    def start_server(self):
        def run_flask():
            try:
                self.root.after(2000, lambda: self.server_status_lbl.config(text="● 伺服器：運行中 (Port 5000)", fg="#28a745"))
                # Use threaded=True to ensure responsiveness
                app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
            except:
                self.root.after(0, lambda: self.server_status_lbl.config(text="● 伺服器：啟動失敗", fg="#dc3545"))
        threading.Thread(target=run_flask, daemon=True).start()

gui_monitor = None

if __name__ == "__main__":
    if os.path.exists(TEMP_AUDIO):
        try: os.remove(TEMP_AUDIO)
        except: pass
    root = tk.Tk()
    gui_monitor = PickupMasterGUI(root)
    root.mainloop()
