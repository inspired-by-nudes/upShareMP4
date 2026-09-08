import os, secrets, json, hashlib, subprocess, threading, logging, time, asyncio, shutil, re
from urllib.parse import urlparse, urljoin
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
import yt_dlp
import requests
from bs4 import BeautifulSoup
from readability import Document

logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logger = logging.getLogger("upshare")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('%(asctime)s - %(message)s', "%Y-%m-%d %H:%M:%S"))
logger.addHandler(ch)

app = FastAPI(title="upShareMedia")

@app.exception_handler(404)
async def custom_404_handler(request: Request, exc):
    return RedirectResponse(url="/")

PORT = int(os.getenv("PORT", "29738"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")
CONFIG_DIR = os.getenv("CONFIG_DIR", "/config")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "")
TIKTOK_COOKIES = os.getenv("TIKTOK_COOKIES", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
INFERENCE_TEXT_MODEL = os.getenv("INFERENCE_TEXT_MODEL", "qwen2.5:14b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_V2 = os.path.join(CONFIG_DIR, "v2_db.json")
DB_FILE = os.path.join(CONFIG_DIR, "v3_db.json")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")
TIKTOK_COOKIE_FILE = os.path.join(CONFIG_DIR, "tiktok_cookies.txt")

db_lock = threading.Lock()
active_downloads = {}

if YTDLP_COOKIES:
    with open(COOKIE_FILE, "w") as f: f.write(YTDLP_COOKIES.replace("\\n", "\n"))
if TIKTOK_COOKIES:
    with open(TIKTOK_COOKIE_FILE, "w") as f: f.write(TIKTOK_COOKIES.replace("\\n", "\n"))

def get_cookie_file_for_url(url: str):
    if "tiktok.com" in url and os.path.exists(TIKTOK_COOKIE_FILE): return TIKTOK_COOKIE_FILE
    if os.path.exists(COOKIE_FILE): return COOKIE_FILE
    return None

def is_social_media_url(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    social_domains = ["instagram.com", "tiktok.com", "youtube.com", "youtu.be", "twitter.com", "x.com"]
    return any(d in domain for d in social_domains)

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f: return json.load(f)
    elif os.path.exists(DB_V2):
        with open(DB_V2, "r") as f: 
            data = json.load(f)
            save_db(data)
            return data
    
    initial_username = os.getenv("APP_USERNAME", "admin")
    default_db = {
        "users": {
            initial_username: {
                "password": hashlib.sha256(os.getenv("APP_PASSWORD", "adminpassword").encode()).hexdigest(),
                "token": secrets.token_urlsafe(32),
                "role": "admin",
                "max_space_mb": 0,
                "warning_mb": int(os.getenv("MAX_DOWNLOAD_MB", "150"))
            }
        },
        "videos": {},
        "server_bandwidth": 0,
        "deleted_count": 0,
        "settings": {
            "login_msg": os.getenv("LOGIN_CONTACT_MSG", "")
        }
    }
    save_db(default_db)
    return default_db

def save_db(data):
    with open(DB_FILE, "w") as f: json.dump(data, f)

def verify_auth(request: Request):
    token = request.cookies.get("upshare_session")
    auth_header = request.headers.get("Authorization")
    
    with db_lock:
        db = load_db()
        users = db.get("users", {})

    if auth_header and auth_header.startswith("Bearer "): token = auth_header.split(" ", 1)[1]
    for username, user_data in users.items():
        if secrets.compare_digest(user_data.get("token", ""), str(token)):
            return {"username": username, "role": user_data["role"], "config": user_data}
    raise StarletteHTTPException(status_code=401, detail="Unauthorized")

def verify_admin(user: dict = Depends(verify_auth)):
    if user["role"] != "admin": raise StarletteHTTPException(status_code=403, detail="Admin access required")
    return user

def increment_view_counter(video_id: str, file_path: str):
    try:
        with db_lock:
            db = load_db()
            if video_id in db["videos"]:
                db["videos"][video_id]["views"] = db["videos"][video_id].get("views", 0) + 1
                if os.path.exists(file_path):
                    db["server_bandwidth"] = db.get("server_bandwidth", 0) + os.path.getsize(file_path)
                save_db(db)
    except Exception:
        pass

@app.middleware("http")
async def track_video_views(request: Request, call_next):
    response = await call_next(request)
    if request.method == "GET" and response.status_code in (200, 206):
        path = request.url.path
        range_header = request.headers.get("range", "")
        if path.startswith("/videos/") and (path.endswith(".mp4") or path.endswith(".html")) and (not range_header or "bytes=0-" in range_header):
            filename = path.split("/")[-1]
            video_id = filename.split(".")[0]
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            asyncio.create_task(asyncio.to_thread(increment_view_counter, video_id, file_path))
    return response

app.mount("/videos", StaticFiles(directory=DOWNLOAD_DIR), name="videos")

def generate_secure_id(): return f"vid_{secrets.token_urlsafe(8)}"

def extract_true_duration(video_id: str, user_id: str, url: str = "#", custom_title: str = None, ext: str = ".mp4", expire_days: int = 0, engine: str = None):
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}")
    duration = 0.0
    if ext == ".mp4":
        try:
            res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path], capture_output=True, text=True)
            duration = float(res.stdout.strip())
        except: pass
        
    title = custom_title if custom_title else f"{video_id}{ext}"
    title = title[:100]
    expires_at = time.time() + (expire_days * 86400) if expire_days > 0 else 0
    
    with db_lock:
        db = load_db()
        db["videos"][video_id] = {
            "owner": user_id,
            "duration": duration,
            "url": url,
            "domain": urlparse(url).netloc.replace('www.', '') if url != "#" else "localhost",
            "views": 0,
            "added": time.time(),
            "title": title,
            "ext": ext,
            "engine": engine,
            "expires_at": expires_at
        }
        save_db(db)

def my_hook(d, task_id, user_id):
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').strip()
        s = d.get('_speed_str', '0KiB/s').strip()
        active_downloads[task_id] = f"Downloading: {p} ({s})"
    elif d['status'] == 'finished':
        active_downloads[task_id] = "Processing..."

def clean_html_with_ai(raw_html: str) -> tuple:
    prompt = f"You are an HTML cleaner. Strip all promotional links, 'Read More' callouts, ad captions, and social widgets from this HTML. RETURN ONLY CLEAN HTML. YOU MUST KEEP ALL <img src=...> and <video> tags intact. Do not remove media. Here is the HTML:\n\n{raw_html[:30000]}"
    
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            headers = {'Content-Type': 'application/json'}
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            res = requests.post(url, headers=headers, json=payload, timeout=20)
            if res.status_code == 200:
                result = res.json()['candidates'][0]['content']['parts'][0]['text']
                return result.replace("```html", "").replace("```", "").strip(), "🤖 Gemini"
            else:
                logger.error(f"Gemini API returned error code {res.status_code}: {res.text}")
        except Exception as e:
            logger.error(f"Gemini API Exception: {e}")

    if INFERENCE_TEXT_MODEL:
        try:
            url = f"{OLLAMA_HOST}/api/generate"
            payload = {"model": INFERENCE_TEXT_MODEL, "prompt": prompt, "stream": False}
            res = requests.post(url, json=payload, timeout=20)
            if res.status_code == 200:
                result = res.json().get('response', raw_html)
                return result.replace("```html", "").replace("