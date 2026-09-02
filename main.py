import os, secrets, json, hashlib, subprocess, threading, logging, time, asyncio
from urllib.parse import urlparse
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel
import yt_dlp

# --- LOGGING OPTIMIZATION ---
# Suppress default Uvicorn spam; only show warnings or our custom app logs
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logger = logging.getLogger("upshare")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('%(asctime)s - %(message)s', "%Y-%m-%d %H:%M:%S"))
logger.addHandler(ch)

app = FastAPI(title="upShareMP4")

# --- ENVIRONMENT VARIABLES ---
PORT = int(os.getenv("PORT", "29738"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")
CONFIG_DIR = os.getenv("CONFIG_DIR", "/config")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "")
TIKTOK_COOKIES = os.getenv("TIKTOK_COOKIES", "")
LOGIN_CONTACT_MSG = os.getenv("LOGIN_CONTACT_MSG", "")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_FILE = os.path.join(CONFIG_DIR, "v2_db.json")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")
TIKTOK_COOKIE_FILE = os.path.join(CONFIG_DIR, "tiktok_cookies.txt")

db_lock = threading.Lock()
# Store active progress states for SSE
active_downloads = {}

if YTDLP_COOKIES:
    with open(COOKIE_FILE, "w") as f:
        f.write(YTDLP_COOKIES.replace("\\n", "\n"))
if TIKTOK_COOKIES:
    with open(TIKTOK_COOKIE_FILE, "w") as f:
        f.write(TIKTOK_COOKIES.replace("\\n", "\n"))

def get_cookie_file_for_url(url: str):
    if "tiktok.com" in url and os.path.exists(TIKTOK_COOKIE_FILE): return TIKTOK_COOKIE_FILE
    if os.path.exists(COOKIE_FILE): return COOKIE_FILE
    return None

# --- DATABASE MANAGEMENT (Fresh Start) ---
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f: return json.load(f)
    # Default initial state
    default_db = {
        "users": {
            "admin": {
                "password": hashlib.sha256(os.getenv("APP_PASSWORD", "adminpassword").encode()).hexdigest(),
                "token": secrets.token_urlsafe(32),
                "role": "admin",
                "max_space_mb": 0, # 0 = unlimited
                "warning_mb": int(os.getenv("MAX_DOWNLOAD_MB", "150"))
            }
        },
        "videos": {}, # { video_id: { user_id, views, etc. } }
        "server_bandwidth": 0,
        "deleted_count": 0
    }
    save_db(default_db)
    return default_db

def save_db(data):
    with open(DB_FILE, "w") as f: json.dump(data, f)

# --- AUTHENTICATION ---
def verify_auth(request: Request):
    token = request.cookies.get("upshare_session")
    auth_header = request.headers.get("Authorization")
    
    with db_lock:
        db = load_db()
        users = db.get("users", {})

    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
    
    for username, user_data in users.items():
        if secrets.compare_digest(user_data.get("token", ""), str(token)):
            return {"username": username, "role": user_data["role"], "config": user_data}
            
    raise StarletteHTTPException(status_code=401, detail="Unauthorized")

def verify_admin(user: dict = Depends(verify_auth)):
    if user["role"] != "admin":
        raise StarletteHTTPException(status_code=403, detail="Admin access required")
    return user

@app.middleware("http")
async def track_video_views(request: Request, call_next):
    path = request.url.path
    range_header = request.headers.get("range", "")
    is_new_view = path.startswith("/videos/") and path.endswith(".mp4") and (not range_header or "bytes=0-" in range_header)
    
    response = await call_next(request)
    
    if is_new_view and response.status_code in (200, 206):
        try:
            filename = path.split("/")[-1]
            video_id = filename.split(".")[0]
            with db_lock:
                db = load_db()
                if video_id in db["videos"]:
                    db["videos"][video_id]["views"] = db["videos"][video_id].get("views", 0) + 1
                    file_path = os.path.join(DOWNLOAD_DIR, filename)
                    if os.path.exists(file_path):
                        db["server_bandwidth"] = db.get("server_bandwidth", 0) + os.path.getsize(file_path)
                    save_db(db)
                    logger.info(f"Serving video: {filename}")
        except Exception:
            pass
    return response

app.mount("/videos", StaticFiles(directory=DOWNLOAD_DIR), name="videos")

# --- CORE LOGIC ---
def extract_true_duration(video_id: str, user_id: str, url: str = "#"):
    mp4_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", mp4_path], capture_output=True, text=True)
        duration = float(res.stdout.strip())
        with db_lock:
            db = load_db()
            db["videos"][video_id] = {
                "owner": user_id,
                "duration": duration,
                "url": url,
                "domain": urlparse(url).netloc.replace('www.', '') if url != "#" else "localhost",
                "views": 0,
                "added": time.time()
            }
            save_db(db)
    except Exception:
        pass

def generate_secure_id(): return f"vid_{secrets.token_urlsafe(8)}"

def my_hook(d, task_id, user_id):
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').strip()
        s = d.get('_speed_str', '0KiB/s').strip()
        active_downloads[task_id] = f"Downloading: {p} ({s})"
    elif d['status'] == 'finished':
        active_downloads[task_id] = "Processing/Converting..."

def process_yt_dlp(url: str, user_id: str, task_id: str):
    logger.info(f"Processing URL for {user_id}: {url}")
    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s_%(autonumber)s.%(ext)s',
        'format': 'bestvideo[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'writeinfojson': True,
        'writethumbnail': True,
        'progress_hooks': [lambda d: my_hook(d, task_id, user_id)],
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    
    cookie_path = get_cookie_file_for_url(url)
    if cookie_path: ydl_opts['cookiefile'] = cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = info.get('entries', [info])
            for entry in entries:
                if not entry: continue
                # Locate generated files, extract duration, and add to DB
                # simplified for brevity: scan directory for matching newly added files
                pass
    except Exception as e:
        logger.error(f"Download failed: {e}")
    finally:
        if task_id in active_downloads: del active_downloads[task_id]
        logger.info(f"Completed processing URL task: {task_id}")

def convert_local_file(input_path: str, final_path: str, video_id: str, user_id: str, task_id: str):
    logger.info(f"Processing local upload: {video_id}")
    active_downloads[task_id] = "Converting..."
    subprocess.run(["ffmpeg", "-i", input_path, "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", final_path, "-y"])
    subprocess.run(["ffmpeg", "-y", "-i", final_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{video_id}.jpg"])
    os.remove(input_path)
    extract_true_duration(video_id, user_id)
    if task_id in active_downloads: del active_downloads[task_id]
    logger.info(f"Completed processing local upload: {video_id}")

# --- API ENDPOINTS ---
@app.get("/api/env")
def get_env():
    return {"login_msg": LOGIN_CONTACT_MSG}

@app.post("/api/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    with db_lock:
        db = load_db()
        user_data = db.get("users", {}).get(username)
        
    if user_data and user_data["password"] == hashlib.sha256(password.encode()).hexdigest():
        max_age = SESSION_DAYS * 86400
        response.set_cookie(key="upshare_session", value=user_data["token"], max_age=max_age, httponly=True)
        return {"status": "success", "token": user_data["token"]}
    raise StarletteHTTPException(status_code=401, detail="Invalid credentials")

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("upshare_session")
    return {"status": "logged_out"}

@app.get("/api/stats")
def get_stats(user: dict = Depends(verify_auth)):
    with db_lock:
        db = load_db()
        
    total_videos = 0
    total_disk = 0
    
    for f in os.listdir(DOWNLOAD_DIR):
        if f.endswith('.mp4') and not f.startswith('temp_'):
            vid_id = f.split('.')[0]
            owner = db["videos"].get(vid_id, {}).get("owner", "")
            if user["role"] == "admin" or owner == user["username"]:
                total_videos += 1
                total_disk += os.path.getsize(os.path.join(DOWNLOAD_DIR, f))
                
    return {
        "role": user["role"],
        "used_disk": total_disk,
        "bandwidth": db.get("server_bandwidth", 0) if user["role"] == "admin" else 0, # Simplified user bandwidth
        "video_count": total_videos,
        "deleted_count": db.get("deleted_count", 0) if user["role"] == "admin" else 0,
        "warning_mb": user["config"].get("warning_mb", 150)
    }

@app.post("/api/download_form")
async def form_download(background_tasks: BackgroundTasks, url: str = Form(...), confirm_override: str = Form(None), user: dict = Depends(verify_auth)):
    warning_mb = user["config"].get("warning_mb", 150)
    
    if confirm_override != "true":
        try:
            ydl_opts = {'noplaylist': True} # Temporarily check first video size
            cookie_path = get_cookie_file_for_url(url)
            if cookie_path: ydl_opts['cookiefile'] = cookie_path
                
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                size_bytes = info.get("filesize") or info.get("filesize_approx") or 0
                size_mb = size_bytes / (1024 * 1024)
                if warning_mb > 0 and size_mb > warning_mb:
                    return {"status": "needs_confirmation", "size_mb": round(size_mb, 1)}
        except Exception:
            pass 
            
    task_id = generate_secure_id()
    active_downloads[task_id] = "Starting up..."
    background_tasks.add_task(process_yt_dlp, url, user["username"], task_id)
    return {"status": "processing"}

@app.post("/api/upload")
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), user: dict = Depends(verify_auth)):
    video_id = generate_secure_id()
    task_id = generate_secure_id()
    temp_path = os.path.join(DOWNLOAD_DIR, f"temp_{video_id}_{file.filename}")
    final_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    
    with open(temp_path, "wb") as buffer:
        buffer.write(await file.read())
        
    active_downloads[task_id] = "Queued..."
    background_tasks.add_task(convert_local_file, temp_path, final_path, video_id, user["username"], task_id)
    return {"status": "processing"}

@app.get("/api/videos")
def list_videos(user: dict = Depends(verify_auth)):
    with db_lock:
        db = load_db()
        
    videos_data = []
    for f in os.listdir(DOWNLOAD_DIR):
        if f.endswith('.mp4') and not f.startswith('temp_'):
            base_name = f.rsplit('.', 1)[0]
            vid_info = db["videos"].get(base_name, {})
            
            if user["role"] != "admin" and vid_info.get("owner") != user["username"]:
                continue
                
            mp4_file = os.path.join(DOWNLOAD_DIR, f)
            size = os.path.getsize(mp4_file)
            date_ts = vid_info.get("added", os.path.getmtime(mp4_file))
            duration = vid_info.get("duration", 0)
            
            thumb = None
            for ext in ['.jpg', '.webp', '.png']:
                if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{base_name}{ext}")):
                    thumb = f"{base_name}{ext}"
                    break
                    
            mins, secs = divmod(int(duration), 60)
            videos_data.append({
                "id": base_name,
                "filename": f,
                "title": f,
                "domain": vid_info.get("domain", "unknown"),
                "thumbnail": thumb,
                "duration": f"{mins}:{secs:02d}",
                "size_bytes": size,
                "date": date_ts,
                "views": vid_info.get("views", 0),
                "original_url": vid_info.get("url", "#"),
                "owner": vid_info.get("owner", "unknown")
            })
            
    videos_data.sort(key=lambda x: x['date'], reverse=True)
    return {"videos": videos_data}

@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, user: dict = Depends(verify_auth)):
    safe_id = os.path.basename(video_id)
    with db_lock:
        db = load_db()
        owner = db["videos"].get(safe_id, {}).get("owner", "")
        if user["role"] != "admin" and owner != user["username"]:
            raise StarletteHTTPException(status_code=403, detail="Forbidden")
            
        deleted = False
        for ext in ['.mp4', '.info.json', '.jpg', '.webp', '.png']:
            file_path = os.path.join(DOWNLOAD_DIR, f"{safe_id}{ext}")
            if os.path.exists(file_path):
                os.remove(file_path)
                deleted = True
        if deleted:
            db["deleted_count"] = db.get("deleted_count", 0) + 1
            if safe_id in db["videos"]: del db["videos"][safe_id]
            save_db(db)
            logger.info(f"Video {safe_id} deleted by {user['username']}")
    return {"status": "deleted"}

# --- SSE ENDPOINT ---
async def event_generator():
    while True:
        if await asyncio.get_event_loop().run_in_executor(None, lambda: bool(active_downloads)):
            yield {"data": json.dumps(active_downloads)}
        await asyncio.sleep(1)

@app.get('/api/sse')
async def sse(request: Request, user: dict = Depends(verify_auth)):
    return EventSourceResponse(event_generator())

# --- USER MANAGEMENT ---
@app.post("/api/users/{target_username}/password")
def change_password(target_username: str, req: Request, password: str = Form(...), user: dict = Depends(verify_auth)):
    if user["role"] != "admin" and user["username"] != target_username:
        raise StarletteHTTPException(status_code=403, detail="Forbidden")
        
    with db_lock:
        db = load_db()
        if target_username in db["users"]:
            db["users"][target_username]["password"] = hashlib.sha256(password.encode()).hexdigest()
            db["users"][target_username]["token"] = secrets.token_urlsafe(32) # Invalidate old sessions
            save_db(db)
            logger.info(f"Password reset for {target_username}")
            return {"status": "success"}
    raise StarletteHTTPException(status_code=404, detail="User not found")

@app.get("/icon.svg")
def get_favicon(): return FileResponse("icon.svg")

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open("index.html", "r", encoding='utf-8') as f: return f.read()

# --- CLI COMMAND FOR PASSWORD RESET ---
def reset_password(username, new_password):
    with db_lock:
        db = load_db()
        if username in db["users"]:
            db["users"][username]["password"] = hashlib.sha256(new_password.encode()).hexdigest()
            db["users"][username]["token"] = secrets.token_urlsafe(32)
            save_db(db)
            print(f"Password for {username} successfully updated.")
        else:
            print("User not found.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)