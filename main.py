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

# AI Article Cleaner Variables
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

def extract_true_duration(video_id: str, user_id: str, url: str = "#", custom_title: str = None, ext: str = ".mp4", expire_days: int = 0):
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

# --- AI ARTICLE CLEANING ENGINE ---
def clean_html_with_ai(raw_text: str) -> str:
    prompt = f"Strip all promotional links, 'Read More' callouts, ad captions, and social widgets from this text. Return only clean paragraphs:\n\n{raw_text[:4000]}"
    
    # Tier A: Gemini Flash API
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            res = requests.post(url, json=payload, timeout=8)
            if res.status_code == 200:
                return res.json()['candidates'][0]['content']['parts'][0]['text']
        except Exception: pass

    # Tier B: Local Ollama Endpoint
    if INFERENCE_TEXT_MODEL:
        try:
            url = f"{OLLAMA_HOST}/api/generate"
            payload = {"model": INFERENCE_TEXT_MODEL, "prompt": prompt, "stream": False}
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                return res.json().get('response', raw_text)
        except Exception: pass

    return raw_text

def extract_article(url: str, user_id: str, task_id: str, expire_days: int):
    try:
        active_downloads[task_id] = "Parsing Article..."
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        r = requests.get(url, headers=headers, timeout=10)
        
        # Tier C: Mozilla Readability Engine
        doc = Document(r.content)
        title = doc.title()
        readable_html = doc.summary()
        
        soup = BeautifulSoup(readable_html, 'html.parser')
        
        # Prune common artifact phrases
        for p in soup.find_all(['p', 'h1', 'h2', 'h3']):
            txt = p.get_text()
            if re.search(r'(Read More|SEE ALSO|Follow us|Photo:|Subscribe|Newsletter)', txt, re.IGNORECASE):
                p.decompose()

        # Resolve media links
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src: img['src'] = urljoin(url, src)
            img['style'] = "max-width:100%; height:auto; border-radius:8px; margin:15px 0; display:block;"

        # Run AI pass over raw paragraphs if available
        raw_text = soup.get_text(separator="\n\n")
        cleaned_text = clean_html_with_ai(raw_text)
        
        content_body = "".join([f"<p>{p.strip()}</p>" for p in cleaned_text.split("\n\n") if len(p.strip()) > 20]) if GEMINI_API_KEY or INFERENCE_TEXT_MODEL else str(soup)

        new_id = generate_secure_id()
        html_path = os.path.join(DOWNLOAD_DIR, f"{new_id}.html")
        
        clean_html = f"""
        <html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
        <title>{title}</title>
        <style>body{{font-family: system-ui, sans-serif; line-height: 1.6; max-width: 800px; margin: 0 auto; padding: 20px; background:#121212; color:#fff;}} h1{{color:#ff8c00; border-bottom:2px solid #333; padding-bottom:10px;}}</style>
        </head><body><h1>{title}</h1><div>{content_body}</div></body></html>
        """
        with open(html_path, "w", encoding="utf-8") as f: f.write(clean_html)
        extract_true_duration(new_id, user_id, url, title, ".html", expire_days)
    except Exception as e:
        logger.error(f"Article parse failed: {e}")
    finally:
        if task_id in active_downloads: del active_downloads[task_id]

def process_yt_dlp(url: str, user_id: str, task_id: str, expire_days: int, force_article: bool = False):
    # Enforce Social Media Exemptions
    if is_social_media_url(url):
        force_article = False

    if force_article:
        extract_article(url, user_id, task_id, expire_days)
        return

    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/temp_yt_{task_id}_%(id)s.%(ext)s',
        'format': 'bestvideo[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'writeinfojson': True,
        'writethumbnail': True,
        'noplaylist': False,
        'progress_hooks': [lambda d: my_hook(d, task_id, user_id)],
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    cookie_path = get_cookie_file_for_url(url)
    if cookie_path: ydl_opts['cookiefile'] = cookie_path

    success = False
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: 
            info = ydl.extract_info(url, download=True)
            if info: success = True
    except Exception as e: 
        logger.error(f"Download failed: {e}")
        
    if not success and not is_social_media_url(url):
        extract_article(url, user_id, task_id, expire_days)
        return

    try:
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(f"temp_yt_{task_id}_") and f.endswith(".mp4"):
                base = f[:-4]
                info_file = os.path.join(DOWNLOAD_DIR, f"{base}.info.json")
                
                new_id = generate_secure_id()
                new_mp4 = os.path.join(DOWNLOAD_DIR, f"{new_id}.mp4")
                os.rename(os.path.join(DOWNLOAD_DIR, f), new_mp4)
                
                extracted_title = None
                if os.path.exists(info_file):
                    try:
                        with open(info_file, 'r', encoding='utf-8') as inf_f:
                            info_data = json.load(inf_f)
                            extracted_title = info_data.get('title') or info_data.get('fulltitle')
                    except: pass
                    os.remove(info_file)
                    
                for ext in ['.jpg', '.webp', '.png']:
                    old_thumb = os.path.join(DOWNLOAD_DIR, f"{base}{ext}")
                    if os.path.exists(old_thumb):
                        os.rename(old_thumb, os.path.join(DOWNLOAD_DIR, f"{new_id}{ext}"))
                        
                extract_true_duration(new_id, user_id, url, extracted_title, ".mp4", expire_days)
                
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(f"temp_yt_{task_id}_"):
                try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                except: pass
    finally:
        if task_id in active_downloads: del active_downloads[task_id]

def convert_local_file(input_path: str, final_path: str, video_id: str, user_id: str, task_id: str, original_filename: str, expire_days: int):
    active_downloads[task_id] = "Converting..."
    subprocess.run(["ffmpeg", "-i", input_path, "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", final_path, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-i", final_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{video_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(input_path)
    extract_true_duration(video_id, user_id, custom_title=original_filename, expire_days=expire_days)
    if task_id in active_downloads: del active_downloads[task_id]

@app.get("/api/env")
def get_env():
    with db_lock:
        db = load_db()
        return {"login_msg": db.get("settings", {}).get("login_msg", os.getenv("LOGIN_CONTACT_MSG", ""))}

@app.post("/api/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    with db_lock:
        db = load_db()
        user_data = db.get("users", {}).get(username)
        
    if user_data and user_data["password"] == hashlib.sha256(password.encode()).hexdigest():
        response.set_cookie(key="upshare_session", value=user_data["token"], max_age=SESSION_DAYS * 86400, httponly=True)
        return {"status": "success", "token": user_data["token"]}
    raise StarletteHTTPException(status_code=401, detail="Invalid credentials")

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("upshare_session")
    return {"status": "logged_out"}

@app.get("/api/stats")
def get_stats(user: dict = Depends(verify_auth)):
    with db_lock: db = load_db()
    total_videos, total_disk = 0, 0
    
    for f in os.listdir(DOWNLOAD_DIR):
        if f.endswith('.mp4') or f.endswith('.html'):
            if f.startswith('temp_'): continue
            vid_id = f.split('.')[0]
            owner = db["videos"].get(vid_id, {}).get("owner", "")
            if user["role"] == "admin" or owner == user["username"]:
                total_videos += 1
                total_disk += os.path.getsize(os.path.join(DOWNLOAD_DIR, f))
                
    return {
        "role": user["role"],
        "used_disk": total_disk,
        "bandwidth": db.get("server_bandwidth", 0) if user["role"] == "admin" else 0,
        "video_count": total_videos,
        "deleted_count": db.get("deleted_count", 0) if user["role"] == "admin" else 0
    }

@app.post("/api/download_form")
async def form_download(background_tasks: BackgroundTasks, url: str = Form(...), fetch_mode: str = Form("media"), expire_days: int = Form(0), confirm_override: str = Form(None), user: dict = Depends(verify_auth)):
    warning_mb = user["config"].get("warning_mb", 150)
    force_article = (fetch_mode == "article") and not is_social_media_url(url)

    if not force_article and confirm_override != "true":
        try:
            ydl_opts = {'noplaylist': True}
            if cp := get_cookie_file_for_url(url): ydl_opts['cookiefile'] = cp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                size_mb = (info.get("filesize") or info.get("filesize_approx") or 0) / (1024 * 1024)
                if warning_mb > 0 and size_mb > warning_mb: return {"status": "needs_confirmation", "size_mb": round(size_mb, 1)}
        except Exception: pass 
            
    task_id = generate_secure_id()
    active_downloads[task_id] = "Starting up..."
    background_tasks.add_task(process_yt_dlp, url, user["username"], task_id, expire_days, force_article)
    return {"status": "processing"}

@app.post("/api/upload")
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), expire_days: int = Form(0), user: dict = Depends(verify_auth)):
    video_id, task_id = generate_secure_id(), generate_secure_id()
    temp_path = os.path.join(DOWNLOAD_DIR, f"temp_{video_id}_{file.filename}")
    final_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    
    active_downloads[task_id] = "Uploading..."
    with open(temp_path, "wb") as buffer: buffer.write(await file.read())
    background_tasks.add_task(convert_local_file, temp_path, final_path, video_id, user["username"], task_id, file.filename, expire_days)
    return {"status": "processing"}

@app.post("/api/edit/{video_id}")
async def edit_video(video_id: str, background_tasks: BackgroundTasks, start: str = Form(...), end: str = Form(...), mode: str = Form(...), user: dict = Depends(verify_auth)):
    safe_id = os.path.basename(video_id)
    with db_lock:
        db = load_db()
        vid = db["videos"].get(safe_id)
        if not vid or (user["role"] != "admin" and vid["owner"] != user["username"]):
            raise StarletteHTTPException(status_code=403, detail="Forbidden")
    
    input_path = os.path.join(DOWNLOAD_DIR, f"{safe_id}.mp4")
    if not os.path.exists(input_path): raise StarletteHTTPException(status_code=404, detail="Not found")

    def run_edit():
        task_id = generate_secure_id()
        active_downloads[task_id] = "Clipping Media..."
        if mode == "copy":
            new_id = generate_secure_id()
            out_path = os.path.join(DOWNLOAD_DIR, f"{new_id}.mp4")
            subprocess.run(["ffmpeg", "-i", input_path, "-ss", start, "-to", end, "-c", "copy", out_path, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["ffmpeg", "-y", "-i", out_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{new_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            extract_true_duration(new_id, user["username"], custom_title=f"Clip - {vid.get('title', new_id)}")
        else:
            temp_out = os.path.join(DOWNLOAD_DIR, f"temp_edit_{safe_id}.mp4")
            subprocess.run(["ffmpeg", "-i", input_path, "-ss", start, "-to", end, "-c", "copy", temp_out, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.move(temp_out, input_path)
            subprocess.run(["ffmpeg", "-y", "-i", input_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{safe_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            extract_true_duration(safe_id, user["username"], custom_title=vid.get('title'))
        if task_id in active_downloads: del active_downloads[task_id]

    background_tasks.add_task(run_edit)
    return {"status": "processing"}

@app.get("/api/download/{video_id}")
def force_download(video_id: str):
    safe_id = os.path.basename(video_id)
    with db_lock:
        db = load_db()
        vid_info = db["videos"].get(safe_id, {})
    
    ext = vid_info.get("ext", ".mp4")
    target_file = os.path.join(DOWNLOAD_DIR, f"{safe_id}{ext}")
    if not os.path.exists(target_file): raise StarletteHTTPException(status_code=404, detail="File not found")
    
    filename = f"{vid_info.get('title', safe_id)}{ext}"
    filename = filename.replace('"', '').replace(',', '')
    return FileResponse(target_file, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/api/videos")
def list_videos(user: dict = Depends(verify_auth)):
    with db_lock: db = load_db()
    videos_data = []
    
    for f in os.listdir(DOWNLOAD_DIR):
        if (f.endswith('.mp4') or f.endswith('.html')) and not f.startswith('temp_'):
            base_name = f.rsplit('.', 1)[0]
            vid_info = db["videos"].get(base_name, {})
            if user["role"] != "admin" and vid_info.get("owner") != user["username"]: continue
                
            media_file = os.path.join(DOWNLOAD_DIR, f)
            thumb = next((f"{base_name}{e}" for e in ['.jpg', '.webp', '.png'] if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{base_name}{e}"))), None)
            mins, secs = divmod(int(vid_info.get("duration", 0)), 60)
            
            added_timestamp = vid_info.get("added", os.path.getmtime(media_file))
            date_str = time.strftime("%b %d", time.localtime(added_timestamp))
            
            videos_data.append({
                "id": base_name,
                "filename": f,
                "type": "article" if f.endswith('.html') else "video",
                "title": vid_info.get("title", f),
                "original_url": vid_info.get("url", "#"),
                "domain": vid_info.get("domain", "unknown"),
                "thumbnail": thumb,
                "duration": f"{mins}:{secs:02d}" if f.endswith('.mp4') else "Reader",
                "size_bytes": os.path.getsize(media_file),
                "date": added_timestamp,
                "date_badge": date_str,
                "views": vid_info.get("views", 0),
                "expires_at": vid_info.get("expires_at", 0)
            })
    return {"videos": sorted(videos_data, key=lambda x: x['date'], reverse=True)}

@app.put("/api/videos/{video_id}/title")
def rename_video(video_id: str, new_title: str = Form(...), user: dict = Depends(verify_auth)):
    safe_id = os.path.basename(video_id)
    new_title = new_title.strip()[:100] 
    
    with db_lock:
        db = load_db()
        owner = db["videos"].get(safe_id, {}).get("owner", "")
        if user["role"] != "admin" and owner != user["username"]: raise StarletteHTTPException(status_code=403, detail="Forbidden")
        if safe_id in db["videos"]:
            db["videos"][safe_id]["title"] = new_title
            save_db(db)
            return {"status": "success"}
    raise StarletteHTTPException(status_code=404, detail="Video not found")

def _delete_video_internal(safe_id: str, db: dict):
    deleted = False
    for ext in ['.mp4', '.html', '.info.json', '.jpg', '.webp', '.png']:
        fp = os.path.join(DOWNLOAD_DIR, f"{safe_id}{ext}")
        if os.path.exists(fp):
            os.remove(fp)
            deleted = True
    if deleted:
        db["deleted_count"] = db.get("deleted_count", 0) + 1
        if safe_id in db["videos"]: del db["videos"][safe_id]
    return deleted

@app.post("/api/videos/bulk_delete")
def bulk_delete(video_ids: str = Form(...), user: dict = Depends(verify_auth)):
    ids = json.loads(video_ids)
    with db_lock:
        db = load_db()
        for vid in ids:
            safe_id = os.path.basename(vid)
            owner = db["videos"].get(safe_id, {}).get("owner", "")
            if user["role"] == "admin" or owner == user["username"]:
                _delete_video_internal(safe_id, db)
        save_db(db)
    return {"status": "deleted"}

@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, user: dict = Depends(verify_auth)):
    safe_id = os.path.basename(video_id)
    with db_lock:
        db = load_db()
        owner = db["videos"].get(safe_id, {}).get("owner", "")
        if user["role"] != "admin" and owner != user["username"]: raise StarletteHTTPException(status_code=403, detail="Forbidden")
        _delete_video_internal(safe_id, db)
        save_db(db)
    return {"status": "deleted"}

async def event_generator():
    while True:
        if await asyncio.get_event_loop().run_in_executor(None, lambda: bool(active_downloads)):
            yield {"data": json.dumps(active_downloads)}
        else:
            yield {"data": "{}"}
        await asyncio.sleep(1)

@app.get('/api/sse')
async def sse(request: Request, user: dict = Depends(verify_auth)): return EventSourceResponse(event_generator())

# --- ADMIN ENDPOINTS ---
@app.post("/api/users")
def create_user(new_username: str = Form(...), new_password: str = Form(...), user: dict = Depends(verify_admin)):
    with db_lock:
        db = load_db()
        if new_username in db["users"]: raise StarletteHTTPException(status_code=400, detail="User exists")
        db["users"][new_username] = {
            "password": hashlib.sha256(new_password.encode()).hexdigest(),
            "token": secrets.token_urlsafe(32),
            "role": "user",
            "max_space_mb": 0,
            "warning_mb": 150
        }
        save_db(db)
    return {"status": "success"}

@app.post("/api/settings/login_msg")
def update_login_msg(login_msg: str = Form(""), user: dict = Depends(verify_admin)):
    with db_lock:
        db = load_db()
        db["settings"]["login_msg"] = login_msg
        save_db(db)
    return {"status": "success"}

@app.post("/api/users/{target_username}/password")
def change_password(target_username: str, req: Request, password: str = Form(...), user: dict = Depends(verify_auth)):
    if user["role"] != "admin" and user["username"] != target_username: raise StarletteHTTPException(status_code=403, detail="Forbidden")
    with db_lock:
        db = load_db()
        if target_username in db["users"]:
            db["users"][target_username]["password"] = hashlib.sha256(password.encode()).hexdigest()
            db["users"][target_username]["token"] = secrets.token_urlsafe(32)
            save_db(db)
            return {"status": "success"}
    raise StarletteHTTPException(status_code=404, detail="User not found")

@app.get("/icon.svg")
def get_favicon(): return FileResponse("icon.svg")

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open("index.html", "r", encoding='utf-8') as f: return f.read()

# Auto Expiration Background Task
async def cleanup_expired_media():
    while True:
        now = time.time()
        with db_lock:
            db = load_db()
            to_delete = []
            for vid, data in db.get("videos", {}).items():
                exp = data.get("expires_at", 0)
                if exp > 0 and now > exp:
                    to_delete.append(vid)
            for vid in to_delete:
                _delete_video_internal(vid, db)
            if to_delete: save_db(db)
        await asyncio.sleep(3600)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_expired_media())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)