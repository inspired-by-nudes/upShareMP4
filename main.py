from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, Depends, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from urllib.parse import urlparse
import yt_dlp
import uuid
import os
import subprocess
import secrets
import json
import base64
import hashlib
import threading

app = FastAPI(title="upShareMP4")

# --- ENVIRONMENT VARIABLES ---
APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "adminpassword")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")
CONFIG_DIR = os.getenv("CONFIG_DIR", "/config")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
MAX_DOWNLOAD_MB = float(os.getenv("MAX_DOWNLOAD_MB", "150"))
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_FILE = os.path.join(CONFIG_DIR, "stats_db.json")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")
db_lock = threading.Lock()

if YTDLP_COOKIES:
    with open(COOKIE_FILE, "w") as f:
        f.write(YTDLP_COOKIES.replace("\\n", "\n"))

def get_auth_token():
    return hashlib.sha256(f"{APP_USERNAME}:{APP_PASSWORD}".encode()).hexdigest()

def verify_auth(request: Request):
    if request.cookies.get("upshare_session") == get_auth_token():
        return APP_USERNAME
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ", 1)[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            u, p = decoded.split(":", 1)
            if secrets.compare_digest(u, APP_USERNAME) and secrets.compare_digest(p, APP_PASSWORD):
                return APP_USERNAME
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="Unauthorized")

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"bandwidth_bytes": 0, "views": {}, "deleted_count": 0}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

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
                db["views"][video_id] = db.get("views", {}).get(video_id, 0) + 1
                file_path = os.path.join(DOWNLOAD_DIR, filename)
                if os.path.exists(file_path):
                    db["bandwidth_bytes"] = db.get("bandwidth_bytes", 0) + os.path.getsize(file_path)
                save_db(db)
        except Exception:
            pass
    return response

app.mount("/videos", StaticFiles(directory=DOWNLOAD_DIR), name="videos")

class VideoRequest(BaseModel):
    url: str

class UrlUpdate(BaseModel):
    url: str

class TitleUpdate(BaseModel):
    title: str

def extract_true_duration(video_id: str):
    mp4_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    json_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.info.json")
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", mp4_path], capture_output=True, text=True)
        duration = float(result.stdout.strip())
        if os.path.exists(json_path):
            with open(json_path, 'r') as f:
                info = json.load(f)
            info['duration'] = duration
            with open(json_path, 'w') as f:
                json.dump(info, f)
    except Exception:
        pass

def process_video(url: str, video_id: str):
    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/{video_id}.%(ext)s',
        'format': 'bestvideo[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'writeinfojson': True,
        'writethumbnail': True,
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    if os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    
    extract_true_duration(video_id)

def convert_local_file(input_path: str, output_path: str, video_id: str):
    subprocess.run(["ffmpeg", "-i", input_path, "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", output_path, "-y"])
    # Adjusted to 0.1 seconds to guarantee extraction even on very short uploads
    subprocess.run(["ffmpeg", "-y", "-i", output_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{video_id}.jpg"])
    os.remove(input_path)
    extract_true_duration(video_id)

def create_dummy_info(video_id: str, original_filename: str):
    info = {"title": original_filename, "webpage_url_domain": "localhost", "duration": 0, "webpage_url": "#"}
    with open(f"{DOWNLOAD_DIR}/{video_id}.info.json", "w") as f:
        json.dump(info, f)

@app.post("/api/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    if secrets.compare_digest(username, APP_USERNAME) and secrets.compare_digest(password, APP_PASSWORD):
        max_age = SESSION_DAYS * 86400
        response.set_cookie(key="upshare_session", value=get_auth_token(), max_age=max_age, httponly=True)
        return {"status": "success"}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("upshare_session")
    return {"status": "logged_out"}

@app.get("/api/stats")
def get_stats(user: str = Depends(verify_auth)):
    with db_lock:
        db = load_db()
    videos = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.mp4') and not f.startswith('temp_')]
    used_videos_bytes = sum(os.path.getsize(os.path.join(DOWNLOAD_DIR, f)) for f in videos)
    return {
        "used_disk": used_videos_bytes,
        "bandwidth": db.get("bandwidth_bytes", 0),
        "video_count": len(videos),
        "deleted_count": db.get("deleted_count", 0),
        "limit_mb": MAX_DOWNLOAD_MB
    }

@app.post("/api/download_form")
async def form_download(background_tasks: BackgroundTasks, url: str = Form(...), override_password: str = Form(None), user: str = Depends(verify_auth)):
    if override_password != APP_PASSWORD:
        try:
            ydl_opts = {'noplaylist': True}
            if os.path.exists(COOKIE_FILE):
                ydl_opts['cookiefile'] = COOKIE_FILE
                
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                size_bytes = info.get("filesize") or info.get("filesize_approx") or 0
                size_mb = size_bytes / (1024 * 1024)
                if size_mb > MAX_DOWNLOAD_MB:
                    return {"status": "needs_override", "size_mb": round(size_mb, 1)}
        except Exception:
            pass 
            
    video_id = f"vid_{str(uuid.uuid4())[:8]}"
    background_tasks.add_task(process_video, url, video_id)
    return {"status": "processing"}

@app.post("/api/upload")
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), user: str = Depends(verify_auth)):
    video_id = f"vid_{str(uuid.uuid4())[:8]}"
    temp_path = os.path.join(DOWNLOAD_DIR, f"temp_{video_id}_{file.filename}")
    final_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    
    with open(temp_path, "wb") as buffer:
        buffer.write(await file.read())
        
    create_dummy_info(video_id, file.filename)
    background_tasks.add_task(convert_local_file, temp_path, final_path, video_id)
    return {"status": "processing"}

@app.post("/api/videos/{video_id}/url")
def update_video_url(video_id: str, req: UrlUpdate, user: str = Depends(verify_auth)):
    safe_id = os.path.basename(video_id)
    info_file = os.path.join(DOWNLOAD_DIR, f"{safe_id}.info.json")
    if os.path.exists(info_file):
        with db_lock:
            with open(info_file, 'r') as f:
                info = json.load(f)
            info['webpage_url'] = req.url
            domain = urlparse(req.url).netloc.replace('www.', '')
            info['webpage_url_domain'] = domain if domain else "unknown"
            with open(info_file, 'w') as f:
                json.dump(info, f)
        return {"status": "updated"}
    raise HTTPException(status_code=404, detail="Video not found")

@app.post("/api/videos/{video_id}/title")
def update_video_title(video_id: str, req: TitleUpdate, user: str = Depends(verify_auth)):
    safe_id = os.path.basename(video_id)
    info_file = os.path.join(DOWNLOAD_DIR, f"{safe_id}.info.json")
    if os.path.exists(info_file):
        with db_lock:
            with open(info_file, 'r') as f:
                info = json.load(f)
            info['title'] = req.title
            with open(info_file, 'w') as f:
                json.dump(info, f)
        return {"status": "updated"}
    raise HTTPException(status_code=404, detail="Video not found")

@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, user: str = Depends(verify_auth)):
    safe_id = os.path.basename(video_id)
    deleted = False
    for ext in ['.mp4', '.info.json', '.jpg', '.webp', '.png']:
        file_path = os.path.join(DOWNLOAD_DIR, f"{safe_id}{ext}")
        if os.path.exists(file_path):
            os.remove(file_path)
            deleted = True
    if deleted:
        with db_lock:
            db = load_db()
            db["deleted_count"] = db.get("deleted_count", 0) + 1
            if safe_id in db.get("views", {}):
                del db["views"][safe_id]
            save_db(db)
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Video not found")

@app.get("/api/videos")
def list_videos(user: str = Depends(verify_auth)):
    with db_lock:
        db = load_db()
    videos_data = []
    for file in os.listdir(DOWNLOAD_DIR):
        if file.endswith('.mp4') and not file.startswith('temp_'):
            base_name = file.rsplit('.', 1)[0]
            info_file = os.path.join(DOWNLOAD_DIR, f"{base_name}.info.json")
            mp4_file = os.path.join(DOWNLOAD_DIR, file)
            
            title = file
            domain = "unknown"
            thumbnail = None
            duration_str = "--:--"
            original_url = "#"
            size = os.path.getsize(mp4_file)
            date_ts = os.path.getmtime(mp4_file)
            views = db.get("views", {}).get(base_name, 0)
            
            if os.path.exists(info_file):
                with open(info_file, 'r', encoding='utf-8') as f:
                    info = json.load(f)
                    title = info.get('title', file)
                    domain = info.get('webpage_url_domain', 'localhost')
                    original_url = info.get('webpage_url', '#')
                    duration = info.get('duration', 0)
                    if duration:
                        mins, secs = divmod(int(duration), 60)
                        duration_str = f"{mins}:{secs:02d}"
            
            for ext in ['.jpg', '.webp', '.png']:
                if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{base_name}{ext}")):
                    thumbnail = f"{base_name}{ext}"
                    break
                    
            videos_data.append({
                "id": base_name,
                "filename": file,
                "title": title,
                "domain": domain,
                "thumbnail": thumbnail,
                "duration": duration_str,
                "size_bytes": size,
                "date": date_ts,
                "views": views,
                "original_url": original_url
            })
            
    videos_data.sort(key=lambda x: x['date'], reverse=True)
    return {"videos": videos_data}

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open("index.html", "r", encoding='utf-8') as f:
        return f.read()