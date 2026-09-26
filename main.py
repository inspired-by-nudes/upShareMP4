import os, secrets, json, hashlib, subprocess, threading, logging, time, asyncio, shutil, re, html
from typing import List
from urllib.parse import urlparse, urljoin, quote, unquote
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
logger = logging.getLogger("upsharemedia")
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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
INFERENCE_TEXT_MODEL = os.getenv("INFERENCE_TEXT_MODEL", "qwen2.5:14b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
DB_FILE = os.path.join(CONFIG_DIR, "upsharemedia.json")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")

db_lock = threading.Lock()
view_lock = threading.Lock()
active_downloads = {}
recent_views = {}

if os.getenv("YTDLP_COOKIES"):
    with open(COOKIE_FILE, "w") as f: f.write(os.getenv("YTDLP_COOKIES").replace("\\n", "\n"))

def get_cookie_file_for_url(url: str):
    if os.path.exists(COOKIE_FILE): return COOKIE_FILE
    return None

def get_requests_cookies(cookie_path: str):
    if not cookie_path or not os.path.exists(cookie_path): return None
    import http.cookiejar
    cj = http.cookiejar.MozillaCookieJar(cookie_path)
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
        return cj
    except:
        return None

def is_social_media_url(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    social_domains = ["instagram.com", "tiktok.com", "youtube.com", "youtu.be", "vimeo.com", "twitter.com", "x.com", "bsky.app", "reddit.com", "redd.it"]
    return any(d in domain for d in social_domains)

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f: return json.load(f)

    initial_username = os.getenv("APP_USERNAME", "admin")
    default_db = {
        "users": {
            initial_username: {
                "password": hashlib.sha256(os.getenv("APP_PASSWORD", "adminpassword").encode()).hexdigest(),
                "token": secrets.token_urlsafe(32),
                "role": "admin",
                "max_space_mb": 0,
                "warning_mb": int(os.getenv("MAX_DOWNLOAD_MB", "150")),
                "bandwidth": 0
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
            user_data["username"] = username
            return {"username": username, "role": user_data["role"], "config": user_data}
    raise StarletteHTTPException(status_code=401, detail="Unauthorized")

def verify_admin(user: dict = Depends(verify_auth)):
    if user["role"] != "admin": raise StarletteHTTPException(status_code=403, detail="Admin access required")
    return user

def increment_view_counter(video_id: str, file_path: str, filename: str):
    try:
        now = time.time()
        with view_lock:
            if video_id in recent_views and (now - recent_views[video_id]) < 10:
                return
            recent_views[video_id] = now
            
        with db_lock:
            db = load_db()
            vid_info = db.get("videos", {}).get(video_id)
            if vid_info:
                ext = vid_info.get('ext')
                if ext and filename != f"{video_id}{ext}": return

                db["videos"][video_id]["views"] = db["videos"][video_id].get("views", 0) + 1
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)
                    db["server_bandwidth"] = db.get("server_bandwidth", 0) + file_size

                    owner = db["videos"][video_id].get("owner")
                    if owner and owner in db["users"]:
                        db["users"][owner]["bandwidth"] = db["users"][owner].get("bandwidth", 0) + file_size
                save_db(db)
    except Exception:
        pass

@app.middleware("http")
async def track_video_views(request: Request, call_next):
    response = await call_next(request)
    if request.method == "GET" and response.status_code in (200, 206):
        path = request.url.path
        range_header = request.headers.get("range", "")
        if path.startswith("/videos/") and (path.endswith(('.mp4', '.html', '.jpg', '.png', '.webp'))) and (not range_header or "bytes=0-" in range_header):
            filename = os.path.basename(path)
            video_id = filename.rsplit(".", 1)[0]
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            asyncio.create_task(asyncio.to_thread(increment_view_counter, video_id, file_path, filename))
    return response

app.mount("/videos", StaticFiles(directory=DOWNLOAD_DIR), name="videos")

def generate_secure_id(): return f"vid_{secrets.token_urlsafe(8)}"

def ensure_ios_compatible_video(file_path: str):
    if not file_path.endswith(('.mp4', '.mov', '.mkv', '.webm')):
        return file_path
    try:
        res = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ], capture_output=True, text=True)
        codec = res.stdout.strip().lower()
    except Exception:
        codec = ""

    temp_out = file_path.rsplit('.', 1)[0] + "_transcoded.mp4"
    if codec == "h264":
        cmd = ["ffmpeg", "-y", "-i", file_path, "-c", "copy", "-movflags", "+faststart", temp_out]
    else:
        cmd = ["ffmpeg", "-y", "-i", file_path, "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-movflags", "+faststart", temp_out]
    
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0 and os.path.exists(temp_out):
        target_path = file_path.rsplit('.', 1)[0] + ".mp4"
        if file_path != target_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass
        shutil.move(temp_out, target_path)
        return target_path
    else:
        if os.path.exists(temp_out):
            try: os.remove(temp_out)
            except: pass
        return file_path

def ensure_jpg_image(file_path: str) -> str:
    if file_path.lower().endswith(('.jpg', '.jpeg')): return file_path
    target_jpg = file_path.rsplit('.', 1)[0] + ".jpg"
    try:
        res = subprocess.run(["ffmpeg", "-y", "-i", file_path, "-q:v", "2", target_jpg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and os.path.exists(target_jpg) and os.path.getsize(target_jpg) > 0:
            if file_path != target_jpg and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            return target_jpg
    except Exception: pass
    return file_path

def extract_true_duration(video_id: str, user_id: str, url: str = "#", custom_title: str = None, ext: str = ".mp4", expire_days: int = 0, engine: str = None, carousel_type: str = None, total_duration: float = 0.0):
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}")
    duration = total_duration
    if ext == ".mp4" and duration == 0.0:
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
            "carousel_type": carousel_type,
            "expires_at": expires_at
        }
        save_db(db)

def generate_carousel_html(carousel_tags: str, extracted_title: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover'>
    <title>{html.escape(extracted_title)}</title>
    <style>
        * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
        html, body {{ margin: 0; padding: 0; background: #000; width: 100vw; height: 100dvh; min-height: -webkit-fill-available; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; user-select: none; }}
        
        .carousel-container {{ position: relative; width: 100vw; height: 100dvh; min-height: -webkit-fill-available; overflow: hidden; display: flex; align-items: center; justify-content: center; }}
        .carousel-track {{ display: flex; transition: transform 0.3s cubic-bezier(0.25, 1, 0.5, 1); height: 100%; width: 100%; }}
        .carousel-item {{ min-width: 100vw; width: 100vw; height: 100dvh; display: flex; align-items: center; justify-content: center; flex-shrink: 0; background: #000; position: relative; padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left); }}
        .carousel-item img, .carousel-item video {{ max-width: 100%; max-height: 100%; width: auto; height: auto; object-fit: contain !important; display: block; margin: auto; }}

        .btn {{ position: absolute; top: 50%; transform: translateY(-50%); background: rgba(0,0,0,0.6); color: white; border: 1px solid rgba(255,255,255,0.2); width: 44px !important; height: 44px !important; min-width: 44px !important; min-height: 44px !important; padding: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; border-radius: 50% !important; font-size: 20px; z-index: 20; transition: all 0.2s; aspect-ratio: 1/1 !important; box-sizing: border-box; flex-shrink: 0 !important; line-height: 1; }}
        .btn:hover {{ background: rgba(0,0,0,0.9); scale: 1.1; }}
        .btn-prev {{ left: 15px; }}
        .btn-next {{ right: 15px; }}
        
        .dots {{ position: absolute; bottom: calc(20px + env(safe-area-inset-bottom, 0px)); width: 100%; display: flex; justify-content: center; align-items: center; gap: 8px; z-index: 20; pointer-events: auto; }}
        .dot {{ width: 10px; height: 10px; background: rgba(255,255,255,0.35); border-radius: 5px; overflow: hidden; position: relative; cursor: pointer; transition: all 0.3s ease; }}
        .dot.active {{ width: 28px; background: rgba(255,255,255,0.35); }}
        .dot-fill {{ height: 100%; width: 0%; background: #ffffff; border-radius: 5px; }}
        
        .tap-zone {{ position: absolute; top: 0; bottom: 0; width: 35%; z-index: 15; }}
        .tap-left {{ left: 0; }}
        .tap-right {{ right: 0; }}
    </style>
    </head><body>
        <div class="carousel-container" id="carousel">
            <div class="tap-zone tap-left" id="tapLeft"></div>
            <div class="tap-zone tap-right" id="tapRight"></div>
            <div class="carousel-track" id="track">{carousel_tags}</div>
            <button class="btn btn-prev" id="btnPrev" onclick="window.move(-1)">❮</button>
            <button class="btn btn-next" id="btnNext" onclick="window.move(1)">❯</button>
            <div class="dots" id="dots"></div>
        </div>
        <script>
            const track = document.getElementById('track');
            const items = track.children.length;
            const dotsContainer = document.getElementById('dots');
            let index = 0;
            let imgTimer = null;

            for (let i = 0; i < items; i++) {{
                let d = document.createElement('div');
                d.className = 'dot' + (i === 0 ? ' active' : '');
                d.onclick = (e) => {{ e.stopPropagation(); window.goTo(i); }};
                d.innerHTML = `<div class="dot-fill" id="fill-${{i}}"></div>`;
                dotsContainer.appendChild(d);
            }}

            const dots = dotsContainer.children;

            function updateSlide() {{
                if (imgTimer) clearInterval(imgTimer);
                document.querySelectorAll('video').forEach(v => {{ v.pause(); v.currentTime = 0; }});

                track.style.transform = `translateX(-${{index * 100}}vw)`;

                for (let i = 0; i < items; i++) {{
                    dots[i].className = 'dot';
                    const fill = document.getElementById(`fill-${{i}}`);
                    if (fill) {{ fill.style.transition = 'none'; fill.style.width = i < index ? '100%' : '0%'; }}
                }}
                
                dots[index].className = 'dot active';
                const currentFill = document.getElementById(`fill-${{index}}`);
                
                const currentSlide = track.children[index];
                const video = currentSlide.querySelector('video');
                
                if (video) {{
                    video.muted = true;
                    video.play().catch(() => {{}});
                    video.ontimeupdate = () => {{
                        if (video.duration && currentFill) {{
                            const pct = (video.currentTime / video.duration) * 100;
                            currentFill.style.transition = 'width 0.1s linear';
                            currentFill.style.width = pct + '%';
                        }}
                    }};
                    video.onended = () => {{
                        if (currentFill) currentFill.style.width = '100%';
                        if (index < items - 1) window.move(1);
                    }};
                }} else {{
                    let start = Date.now();
                    const duration = 5000;
                    imgTimer = setInterval(() => {{
                        let elapsed = Date.now() - start;
                        let pct = Math.min(100, (elapsed / duration) * 100);
                        if (currentFill) {{
                            currentFill.style.transition = 'width 0.1s linear';
                            currentFill.style.width = pct + '%';
                        }}
                        if (elapsed >= duration) {{
                            clearInterval(imgTimer);
                            if (index < items - 1) window.move(1);
                        }}
                    }}, 100);
                }}
            }}

            window.move = function(dir) {{
                index += dir;
                if (index < 0) index = items - 1;
                if (index >= items) index = 0;
                updateSlide();
            }};

            window.goTo = function(i) {{
                index = i;
                updateSlide();
            }};

            let touchStartX = 0;
            let touchEndX = 0;
            const container = document.getElementById('carousel');

            container.addEventListener('touchstart', e => {{ touchStartX = e.changedTouches[0].screenX; }}, {{ passive: true }});
            container.addEventListener('touchend', e => {{ touchEndX = e.changedTouches[0].screenX; handleSwipe(); }}, {{ passive: true }});

            function handleSwipe() {{
                const diff = touchStartX - touchEndX;
                if (Math.abs(diff) > 40) {{
                    if (diff > 0) window.move(1);
                    else window.move(-1);
                }}
            }}

            document.getElementById('tapLeft').onclick = (e) => {{ e.stopPropagation(); window.move(-1); }};
            document.getElementById('tapRight').onclick = (e) => {{ e.stopPropagation(); window.move(1); }};

            document.addEventListener('keydown', e => {{
                if (e.key === 'ArrowLeft') window.move(-1);
                if (e.key === 'ArrowRight' || e.key === ' ') window.move(1);
            }});

            if (items <= 1) {{
                document.querySelectorAll('.btn').forEach(b => b.style.display = 'none');
                document.querySelectorAll('.tap-zone').forEach(tz => tz.style.display = 'none');
                document.getElementById('dots').style.display = 'none';
            }}

            updateSlide();
        </script>
    </body></html>
    """

def my_hook(d, task_id, user_id):
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').strip()
        s = d.get('_speed_str', '0KiB/s').strip()
        active_downloads[task_id] = f"Downloading: {p} ({s})"
    elif d['status'] == 'finished':
        active_downloads[task_id] = "Processing..."

def format_tokens(count):
    if count >= 1000: return f"{count/1000:.1f}k".replace('.0k', 'k')
    return str(count)

def clean_html_with_ai(raw_html: str) -> tuple:
    prompt = f"You are an expert HTML typographer. Enhance typography (headings, blockquotes, bolding, italics). \nCRITICAL RULES:\n1. Output the ENTIRE article text. However, you MUST REMOVE any 'Add to Google', 'preferred source', newsletter signups, subscription prompts, or advertisement text.\n2. Do NOT split blockquotes into multiple adjacent blocks for the same speaker. Keep quotes combined in a single <blockquote> element.\n3. When a blockquote includes an attribution line (e.g., '— Name'), place it on a NEW LINE at the bottom of the blockquote using a <br> tag.\n4. CRITICAL: You will see text markers like ___UPSHARE_IMAGE___SRC:url___CAPTION:text___, ___UPSHARE_VIDEO___SRC:url___ and ___UPSHARE_EMBED___RAW:code___. You MUST preserve these markers exactly word-for-word. Do not alter, translate, or remove them.\n5. Return ONLY valid HTML.\n\nHere is the raw HTML:\n\n{raw_html[:35000]}"
    bt = "`" * 3

    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={GEMINI_API_KEY}"
            headers = {'Content-Type': 'application/json'}
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            res = requests.post(url, headers=headers, json=payload, timeout=90)
            if res.status_code == 200:
                json_res = res.json()
                result = json_res['candidates'][0]['content']['parts'][0]['text']
                usage = json_res.get('usageMetadata') or {}
                token_count = usage.get('totalTokenCount', 0)
                engine_str = f"📄 Gemini ({format_tokens(token_count)})" if token_count else "📄 Gemini"
                clean_result = result.replace(f'{bt}html', '').replace(bt, '').strip()
                if len(clean_result) > 100: return clean_result, engine_str
        except Exception: pass

    if INFERENCE_TEXT_MODEL:
        try:
            url = f"{OLLAMA_HOST}/api/generate"
            payload = {"model": INFERENCE_TEXT_MODEL, "prompt": prompt, "stream": False}
            res = requests.post(url, json=payload, timeout=90)
            if res.status_code == 200:
                json_res = res.json()
                result = json_res.get('response', raw_html)
                tokens = json_res.get('prompt_eval_count', 0) + json_res.get('eval_count', 0)
                engine_str = f"📄 Ollama ({format_tokens(tokens)})" if tokens else "📄 Ollama"
                clean_result = result.replace(f'{bt}html', '').replace(bt, '').strip()
                if len(clean_result) > 100: return clean_result, engine_str
        except Exception: pass

    return raw_html, "📄 Readability"

def process_embed_post(url: str, user_id: str, task_id: str, expire_days: int) -> bool:
    # Unroll shortened reddit links so oEmbed endpoints match cleanly
    if "redd.it" in url or "/s/" in url:
        try:
            r = requests.head(url, allow_redirects=True, timeout=5, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            url = r.url
        except: pass

    domain = urlparse(url).netloc.lower()
    new_id = generate_secure_id()
    html_path = os.path.join(DOWNLOAD_DIR, f"{new_id}.html")
    
    if "twitter.com" in domain or "x.com" in domain:
        oembed_endpoint = f"https://publish.twitter.com/oembed?url={quote(url)}&theme=dark"
        platform = "Twitter"
        script_tag = '<script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>'
    elif "bsky.app" in domain:
        oembed_endpoint = f"https://embed.bsky.app/oembed?url={quote(url)}"
        platform = "Bluesky"
        script_tag = '<script async src="https://embed.bsky.app/static/embed.js" charset="utf-8"></script>'
    elif "reddit.com" in domain:
        oembed_endpoint = f"https://www.reddit.com/oembed?url={quote(url)}"
        platform = "Reddit"
        script_tag = '<script async src="https://embed.redditmedia.com/widgets.js" charset="utf-8"></script>'
    else:
        return False

    try:
        active_downloads[task_id] = f"Fetching {platform} Post..."
        # Reddit aggressively blocks Python default headers. We MUST spoof a browser.
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}
        res = requests.get(oembed_endpoint, headers=headers, timeout=10)
        
        if res.status_code == 200:
            data = res.json()
            embed_html = data.get("html", "")
            
            author = data.get("author_name")
            title = f"Post by {author}" if author else f"{platform} Post"
            
            thumb_url = data.get("thumbnail_url")
            
            # Cascade 1: Let yt-dlp find attached high-res media thumbnails
            if not thumb_url:
                try:
                    with yt_dlp.YoutubeDL({'quiet': True, 'ignoreerrors': True}) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if info and info.get('thumbnail'):
                            thumb_url = info.get('thumbnail')
                except Exception: pass

            # Cascade 2: Let the free Microlink API hunt for the exact OpenGraph image
            if not thumb_url:
                try:
                    res_ml = requests.get(f"https://api.microlink.io?url={quote(url)}", timeout=8)
                    if res_ml.status_code == 200:
                        thumb_url = res_ml.json().get('data', {}).get('image', {}).get('url')
                except Exception: pass
            
            # Cascade 3: Command Thum.io to generate a live headless-browser screenshot
            if not thumb_url:
                thumb_url = f"https://image.thum.io/get/width/1200/crop/800/noanimate/{url}"

            if thumb_url:
                try:
                    t_res = requests.get(thumb_url, headers=headers, timeout=15)
                    # Validate the image isn't an empty payload
                    if t_res.status_code == 200 and len(t_res.content) > 1000:
                        with open(os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg"), "wb") as tf:
                            tf.write(t_res.content)
                    else:
                        raise ValueError("Empty image")
                except Exception:
                    # Absolute Cascade 4: Write a high-res platform logo
                    try:
                        f_res = requests.get(f"https://www.google.com/s2/favicons?domain={domain}&sz=256", headers=headers)
                        with open(os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg"), "wb") as tf: tf.write(f_res.content)
                    except: pass

            og_image_meta = f'<meta property="og:image" content="/videos/{new_id}.jpg"><meta name="twitter:image" content="/videos/{new_id}.jpg">' if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg")) else ''

            full_page = f"""<!DOCTYPE html>
            <html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'>
            <title>{html.escape(title)}</title>
            <meta property="og:title" content="{html.escape(title)}">
            <meta property="og:type" content="article">
            <meta property="og:description" content="View post on upShareMedia">
            {og_image_meta}
            <meta name="twitter:card" content="summary_large_image">
            <style>
                body {{ font-family: system-ui, sans-serif; background: #121212; color: #fff; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }}
                .embed-wrapper {{ max-width: 550px; width: 100%; margin: 0 auto; display: flex; justify-content: center; }}
            </style>
            </head><body>
                <div class="embed-wrapper">{embed_html}</div>
                {script_tag}
            </body></html>"""
            
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(full_page)
            
            extract_true_duration(new_id, user_id, url, title, ".html", expire_days, engine="📰 Post")
            return True
    except Exception as e:
        logger.error(f"Embed processing failed for {url}: {e}")
    return False

def extract_article(url: str, user_id: str, task_id: str, expire_days: int):
    new_id = generate_secure_id()
    try:
        active_downloads[task_id] = "Parsing Article..."
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8'
        }
        r = requests.get(url, headers=headers, timeout=10)

        if 'image' in r.headers.get('Content-Type', '').lower():
            ext = '.' + urlparse(url).path.split('/')[-1].split('.')[-1]
            if not ext or len(ext) > 5 or not ext[1:].isalpha(): ext = '.jpg'
            with open(os.path.join(DOWNLOAD_DIR, f"{new_id}{ext}"), "wb") as f: f.write(r.content)
            extract_true_duration(new_id, user_id, url, f"Image - {ext[1:].upper()}", ext, expire_days, engine="🖼️ Image")
            return

        orig_soup = BeautifulSoup(r.content, 'html.parser')

        for bq in list(orig_soup.find_all('blockquote', class_=re.compile(r'(twitter-tweet|bsky-embed|reddit-embed-bq)', re.I))):
            marker = orig_soup.new_tag('p')
            marker.string = f"___UPSHARE_EMBED___RAW:{quote(str(bq))}___"
            bq.replace_with(marker)

        junk_selectors = [
            'script', 'style', 'nav', 'footer', 'header', 'form', 'aside', 'iframe', 'noscript',
            '[class*="social-share"]', '[class*="share-bar"]', '[class*="newsletter"]',
            '.ad-container', '.advertisement', '.mrf-article-body-ad'
        ]
        for sel in junk_selectors:
            for el in orig_soup.select(sel):
                try: el.decompose()
                except: pass

        google_artifacts = ['Add Delish as a preferred source', 'Use your Google account to add this', 'Google search results', 'preferred source in your Google', 'Add to Google Settings']
        for el in orig_soup.find_all(string=True):
            text = el.text.strip()
            if any(art in text for art in google_artifacts):
                parent_block = el.find_parent(['div', 'aside', 'section'])
                if parent_block: parent_block.decompose()
                else: el.extract()

        og_img = orig_soup.find('meta', property='og:image')
        article_img_url = None
        if og_img and isinstance(og_img, type(orig_soup.new_tag('meta'))):
            article_img_url = og_img.get('content')

        for iframe in list(orig_soup.find_all(['iframe', 'embed', 'video'])):
            src = iframe.get('src') or iframe.get('data-src') or ''
            if not src.startswith('http') and src.startswith('//'): src = f"https:{src}"
            if 'youtube' in src or 'youtu.be' in src or 'vimeo' in src:
                marker = orig_soup.new_tag('p')
                marker.string = f"___UPSHARE_VIDEO___SRC:{src}___"
                iframe.replace_with(marker)
            else:
                iframe.decompose()

        seen_srcs = set()
        first_img_src = None
        junk_img_keywords = ['logo', 'icon', 'badge', 'clock', 'avatar', 'button', 'google', 'facebook', 'twitter', 'instagram', 'pinterest', 'share', 'pixel', 'sprite', 'svg']
        
        for img in list(orig_soup.find_all('img')):
            src = img.get('data-src') or img.get('data-lazy-src') or img.get('src') or ''
            if not src or src.startswith('data:'):
                img.decompose()
                continue

            alt = (img.get('alt') or '').lower()
            cls = ' '.join(img.get('class', [])).lower()
            
            w, h = img.get('width'), img.get('height')
            is_small = False
            try:
                if w and int(w) < 100: is_small = True
                if h and int(h) < 100: is_small = True
            except: pass

            if is_small or any(k in src.lower() for k in junk_img_keywords) or any(k in cls for k in junk_img_keywords) or any(k in alt for k in ['clock', 'logo', 'icon', 'google']):
                img.decompose()
                continue

            if src in seen_srcs:
                img.decompose()
                continue
            seen_srcs.add(src)
            
            if not first_img_src:
                first_img_src = src

            container = img.find_parent(['figure', 'picture'])
            if not container:
                potentials = img.find_parents(['div'], class_=re.compile(r'(wp-caption|image-container|photo-wrapper|featured-image)', re.I))
                for p_container in potentials:
                    if len(p_container.get_text(strip=True)) < 250:
                        container = p_container
                        break

            cap_parts = []
            if container and container.name != 'body':
                cap_el = container.find(['figcaption']) or container.find(class_=re.compile(r'(caption)', re.I))
                if cap_el:
                    credit_el = cap_el.find(class_=re.compile(r'(credit|source|byline)', re.I))
                    if credit_el:
                        c_txt = credit_el.get_text(strip=True)
                        credit_el.decompose()
                        if c_txt: cap_parts.append(c_txt)
                    
                    cap_txt = cap_el.get_text(separator=' ', strip=True)
                    if cap_txt: cap_parts.append(cap_txt)
                else:
                    for el in container.find_all(['span', 'p', 'div'], class_=re.compile(r'(caption|credit|byline|source)', re.I)):
                        t = el.get_text(separator=' ', strip=True)
                        if t and t not in cap_parts:
                            cap_parts.append(t)
                            
                cap_text = "|||".join(cap_parts).replace('___', ' - ')
                target_to_replace = container
            else:
                target_to_replace = img
                cap_text = ""

            marker = orig_soup.new_tag('p')
            marker.string = f"___UPSHARE_IMAGE___SRC:{src}___CAPTION:{cap_text}___"
            target_to_replace.replace_with(marker)

        doc = Document(str(orig_soup))
        title = doc.title()
        readable_html = doc.summary()
        
        soup = BeautifulSoup(readable_html, 'html.parser')
        raw_html_str = str(soup)
        
        if len(BeautifulSoup(raw_html_str, 'html.parser').get_text(strip=True)) < 300:
            logger.info(f"[{task_id}] Readability stripped too much. Attempting manual DOM extraction...")
            article_body = orig_soup.find(['article', 'main']) or orig_soup.find(class_=re.compile(r'(article-body|entry-content|post-content|story-body|single__content)', re.I))
            if article_body:
                raw_html_str = str(article_body)
            else:
                raw_html_str = str(orig_soup.find('body') or orig_soup)
                
            soup = BeautifulSoup(raw_html_str, 'html.parser')

        for h1 in soup.find_all('h1'):
            if title.lower() in h1.get_text().lower() or h1.get_text().lower() in title.lower():
                h1.decompose()
        for a in soup.find_all('a'):
            txt = a.get_text(strip=True)
            if txt.isdigit() and len(txt) <= 3:
                a.decompose()

        cleaned_html, engine = clean_html_with_ai(str(soup))
        final_html = cleaned_html

        def vid_repl(match):
            src = match.group(1).strip()
            return f'<div style="position:relative; padding-bottom:56.25%; height:0; overflow:hidden; margin:30px 0; border-radius:8px; box-shadow:0 4px 12px rgba(0,0,0,0.3);"><iframe src="{src}" style="position:absolute; top:0; left:0; width:100%; height:100%; border:0;" allowfullscreen="true"></iframe></div>'

        final_html = re.sub(r'<p[^>]*>\s*___UPSHARE_VIDEO___SRC:([\s\S]*?)___\s*</p>', vid_repl, final_html)
        final_html = re.sub(r'___UPSHARE_VIDEO___SRC:([\s\S]*?)___', vid_repl, final_html)

        def embed_repl(match):
            raw_code = unquote(match.group(1).strip())
            return f'<div style="display:flex; justify-content:center; margin:25px 0;">{raw_code}</div>'

        final_html = re.sub(r'<p[^>]*>\s*___UPSHARE_EMBED___RAW:([\s\S]*?)___\s*</p>', embed_repl, final_html)
        final_html = re.sub(r'___UPSHARE_EMBED___RAW:([\s\S]*?)___', embed_repl, final_html)

        img_counter = [0]
        def img_repl(match):
            src = match.group(1).strip()
            src = urljoin(url, src)
            cap = match.group(2).strip()
            
            img_counter[0] += 1
            local_filename = f"{new_id}_art_{img_counter[0]}.jpg"
            local_filepath = os.path.join(DOWNLOAD_DIR, local_filename)
            try:
                r_img = requests.get(src, headers=headers, timeout=10)
                if r_img.status_code == 200:
                    with open(local_filepath, "wb") as local_f: local_f.write(r_img.content)
                    ensure_jpg_image(local_filepath)
                    src = f"/videos/{local_filename}"
            except Exception as ex:
                logger.error(f"Failed offline archiving for {src}: {ex}")

            cap_lines = [c.strip() for c in cap.split('|||') if c.strip()] if cap else []
            caption_text = []
            credit_text = []

            for line in cap_lines:
                if '//' in line:
                    parts = [p.strip() for p in line.split('//') if p.strip()]
                    line = " / ".join(parts)
                clean_line = line.strip()
                is_credit = bool(re.search(r'(getty|images|photo|courtesy|reuters|ap|afp|nurphoto|splash|shutterstock|instagram|twitter|facebook|credit|via)', clean_line, re.I)) or clean_line.startswith('—') or clean_line.startswith('-') or ('/' in clean_line and len(clean_line) < 100)
                
                if is_credit:
                    clean_credit = re.sub(r'^[—\-\s]+', '', clean_line)
                    if clean_credit and clean_credit not in credit_text:
                        credit_text.append(f"— {clean_credit}")
                else:
                    caption_text.append(clean_line)

            final_cap_parts = []
            if caption_text: final_cap_parts.append(" ".join(caption_text))
            if credit_text: final_cap_parts.extend(credit_text)
            formatted_cap = "<br>".join(final_cap_parts) if final_cap_parts else ""

            fig = f'<figure style="margin: 30px 0; display: flex; flex-direction: column; align-items: center; text-align: center;"><img src="{src}" style="max-width:100%; height:auto; border-radius:8px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); display: block; margin: 0 auto;">'
            if formatted_cap:
                fig += f'<figcaption style="font-size: 0.85rem; color: #aaa; text-align: center; margin-top: 8px; font-style: italic; max-width: 90%; display: block; margin-left: auto; margin-right: auto;">{formatted_cap}</figcaption>'
            fig += '</figure>'
            return fig

        final_html = re.sub(r'<p[^>]*>\s*___UPSHARE_IMAGE___SRC:([\s\S]*?)___CAPTION:([\s\S]*?)___\s*</p>', img_repl, final_html)
        final_html = re.sub(r'___UPSHARE_IMAGE___SRC:([\s\S]*?)___CAPTION:([\s\S]*?)___', img_repl, final_html)

        ai_soup = BeautifulSoup(final_html, 'html.parser')
        
        for p in list(ai_soup.find_all(['p', 'span', 'small'])):
            if p.find_all(['p', 'div', 'article', 'section']):
                continue
            txt = p.get_text(strip=True)
            if txt and len(txt) < 150 and ('//' in txt or (any(k in txt.lower() for k in ['getty', 'nurphoto', 'shutterstock', 'credit:']) and len(txt) < 120)):
                if not p.find_parent('figcaption'):
                    clean_credit = re.sub(r'\s*//\s*', ' / ', txt)
                    clean_credit = re.sub(r'^[—\-\s]+', '', clean_credit)
                    
                    prev_fig = p.find_previous('figure')
                    if prev_fig:
                        cap_el = prev_fig.find('figcaption')
                        if cap_el and clean_credit not in cap_el.get_text():
                            cap_el.append(ai_soup.new_tag('br'))
                            cap_el.append(f"— {clean_credit}")
                        elif not cap_el:
                            new_cap = ai_soup.new_tag('figcaption', attrs={'style': 'font-size: 0.85rem; color: #aaa; text-align: center; margin-top: 8px; font-style: italic; max-width: 90%; display: block; margin-left: auto; margin-right: auto;'})
                            new_cap.string = f"— {clean_credit}"
                            prev_fig.append(new_cap)
                        p.decompose()

        bq_list = ai_soup.find_all('blockquote')
        for i in range(len(bq_list) - 1, 0, -1):
            curr_bq = bq_list[i]
            prev_bq = bq_list[i - 1]
            if prev_bq.find_next_sibling() == curr_bq and 'twitter-tweet' not in curr_bq.get('class', []) and 'bsky-embed' not in curr_bq.get('class', []) and 'reddit-embed-bq' not in curr_bq.get('class', []):
                prev_bq.append(ai_soup.new_tag('br'))
                for child in list(curr_bq.contents): prev_bq.append(child)
                curr_bq.decompose()

        final_html = str(ai_soup)
        html_path = os.path.join(DOWNLOAD_DIR, f"{new_id}.html")
        domain = urlparse(url).netloc.replace('www.', '')

        logo_html = f"""
        <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; margin-bottom: 30px; padding: 20px; background: #1e1e1e; border-radius: 8px;">
            <a href="{url}" target="_blank" style="display: flex; align-items: center; justify-content: center;">
                <img src="https://www.google.com/s2/favicons?domain={domain}&sz=64" alt="{domain} Logo" style="width: 48px; height: 48px; border-radius: 50%; margin: 0;">
            </a>
            <a href="{url}" target="_blank" style="color: #ff8c00; text-decoration: none; font-size: 0.9rem; font-weight: bold; text-align: center;">View Original Article on {domain}</a>
        </div>
        """

        safe_title = html.escape(title)
        if not article_img_url: article_img_url = first_img_src
            
        if article_img_url:
            article_img_url = urljoin(url, article_img_url)
            try:
                img_data = requests.get(article_img_url, headers=headers, timeout=10).content
                with open(os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg"), "wb") as img_f:
                    img_f.write(img_data)
            except Exception as e:
                logger.error(f"Thumbnail download failed: {e}")

        og_image_meta = f'<meta property="og:image" content="/videos/{new_id}.jpg"><meta name="twitter:image" content="/videos/{new_id}.jpg">' if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg")) else ''

        clean_page = f"""
        <html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
        <title>{safe_title}</title>
        <meta property="og:title" content="{safe_title}">
        <meta property="og:type" content="article">
        <meta property="og:description" content="View article on upShareMedia">
        {og_image_meta}
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{safe_title}">
        <style>
            body{{font-family: system-ui, sans-serif; line-height: 1.7; max-width: 800px; margin: 0 auto; padding: 20px; background:#121212; color:#e0e0e0;}} 
            h2, h3 {{color:#ff8c00; margin-top: 40px;}}
            h1 {{color:#ff8c00; border-bottom:2px solid #333; padding-bottom:12px; margin-bottom:30px; font-size: 2rem;}}
            a {{color: #00E676; text-decoration: none;}}
            a:hover {{text-decoration: underline;}}
            blockquote {{border-left: 4px solid #ff8c00; margin: 25px 0; padding-left: 20px; color: #fff; font-style: italic; font-size: 1.1rem; background: #1a1a1a; padding-top: 10px; padding-bottom: 10px; border-radius: 0 6px 6px 0;}}
            p {{margin-bottom: 18px;}}
            figure {{ margin: 30px 0; display: flex; flex-direction: column; align-items: center; text-align: center; }}
            figcaption {{ font-size: 0.85rem; color: #aaa; text-align: center; margin-top: 8px; font-style: italic; max-width: 90%; display: block; margin-left: auto; margin-right: auto; }}
        </style>
        </head><body>
            {logo_html}
            <h1>{safe_title}</h1>
            <div>{final_html}</div>
            <script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
            <script async src="https://embed.bsky.app/static/embed.js" charset="utf-8"></script>
            <script async src="https://embed.redditmedia.com/widgets.js" charset="utf-8"></script>
        </body></html>
        """
        with open(html_path, "w", encoding="utf-8") as f: f.write(clean_page)
        extract_true_duration(new_id, user_id, url, title, ".html", expire_days, engine=engine)
    except Exception as e:
        logger.error(f"Article parse failed: {e}")
    finally:
        if task_id in active_downloads: del active_downloads[task_id]

def scrape_instaloader(url: str, task_id: str) -> list:
    try:
        import instaloader
    except ImportError:
        logger.warning(f"[Instagram Task {task_id}] Instaloader not installed.")
        return []

    extracted = []
    try:
        shortcode_match = re.search(r'/(?:p|reel|tv)/([^/?#]+)', url)
        if not shortcode_match: return []
        shortcode = shortcode_match.group(1)

        logger.info(f"[Instagram Task {task_id}] Querying Instaloader...")
        L = instaloader.Instaloader(quiet=True, download_pictures=False, download_video_thumbnails=False, download_videos=False)
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        caption = post.caption or "Instagram Media"
        
        if post.typename == 'GraphSidecar':
            for node in post.get_sidecar_nodes():
                extracted.append({
                    'is_video': node.is_video,
                    'vid_url': node.video_url if node.is_video else None,
                    'img_url': node.display_url,
                    'title': caption
                })
        else:
            extracted.append({
                'is_video': post.is_video,
                'vid_url': post.video_url if post.is_video else None,
                'img_url': post.url,
                'title': caption
            })
            
        if extracted:
            return extracted
    except Exception as e:
        logger.error(f"[Instagram Task {task_id}] Instaloader Error: {e}")
    return extracted

def scrape_instagram_embed(url: str, task_id: str) -> list:
    extracted = []
    try:
        shortcode_match = re.search(r'/(?:p|reel|tv)/([^/?#]+)', url)
        if not shortcode_match: return []
        shortcode = shortcode_match.group(1)
        
        embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36', 'Accept': '*/*'}
        
        r = requests.get(embed_url, headers=headers, timeout=15)
        if r.status_code == 200:
            json_data = None
            match = re.search(r'window\.__additionalDataLoaded\([^,]+,\s*({.+?})\);', r.text)
            if match:
                json_data = json.loads(match.group(1))
            if not json_data:
                soup = BeautifulSoup(r.text, 'html.parser')
                for script in soup.find_all('script'):
                    if script.string and 'shortcode_media' in script.string:
                        try:
                            m = re.search(r'({.*"shortcode_media".*})', script.string)
                            if m: json_data = json.loads(m.group(1))
                        except: pass

            if json_data:
                media = json_data.get('shortcode_media', {})
                if not media: media = json_data.get('graphql', {}).get('shortcode_media', {})
                    
                if media:
                    caption_text = "Instagram Media"
                    try:
                        edges = media.get('edge_media_to_caption', {}).get('edges', [])
                        if edges: caption_text = edges[0]['node']['text']
                    except: pass
                    
                    children = media.get('edge_sidecar_to_children', {}).get('edges', [])
                    items = [child['node'] for child in children] if children else [media]
                    
                    for item in items:
                        is_vid = item.get('is_video', False)
                        vid_url = item.get('video_url')
                        img_url = item.get('display_url')
                        extracted.append({
                            'is_video': is_vid,
                            'vid_url': html.unescape(vid_url).replace('\\/', '/') if vid_url else None,
                            'img_url': html.unescape(img_url).replace('\\/', '/') if img_url else None,
                            'title': caption_text
                        })
                    return extracted
    except Exception as e:
        logger.error(f"[Instagram Task {task_id}] Embed Error: {e}")
    return extracted

def scrape_cobalt_fleet(url: str, task_id: str) -> list:
    extracted = []
    cobalt_instances = [
        "https://api.cobalt.tools/", "https://api.cobalt.buss.lol/",
        "https://api.vytal.io/", "https://cobalt.qewertyy.dev/",
        "https://cobalt.seasi.dev/", "https://co.wuk.sh/"
    ]
    headers_cobalt = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    
    for ep in cobalt_instances:
        try:
            r = requests.post(ep, json={"url": url}, headers=headers_cobalt, timeout=12)
            if r.status_code in [200, 201, 202]:
                data = r.json()
                status = data.get("status")
                
                if status in ["success", "stream", "redirect"]:
                    u = data.get("url")
                    is_vid = data.get("type") == "video" or (u and ".mp4" in u.lower())
                    extracted.append({'is_video': is_vid, 'vid_url': u if is_vid else None, 'img_url': u if not is_vid else None, 'title': 'Instagram Media'})
                    return extracted
                elif status == "picker":
                    for item in data.get("picker", []):
                        u = item.get("url")
                        if u:
                            is_vid = item.get("type") == "video" or ".mp4" in u.lower()
                            extracted.append({'is_video': is_vid, 'vid_url': u if is_vid else None, 'img_url': u if not is_vid else None, 'title': 'Instagram Media'})
                    if extracted: return extracted
        except: pass
    return extracted

def process_yt_dlp(url: str, user_id: str, task_id: str, expire_days: int):
    # Map all variants of embed targets
    domain = urlparse(url).netloc.lower()
    is_embed_target = False
    
    if "twitter.com" in domain or "x.com" in domain:
        if "/status/" in url: is_embed_target = True
    elif "bsky.app" in domain:
        if "/post/" in url: is_embed_target = True
    elif "reddit.com" in domain or "redd.it" in domain:
        if "/comments/" in url or "/s/" in url or "redd.it" in domain: is_embed_target = True

    if is_embed_target:
        if process_embed_post(url, user_id, task_id, expire_days):
            if task_id in active_downloads: del active_downloads[task_id]
            return

    if not is_social_media_url(url):
        extract_article(url, user_id, task_id, expire_days)
        return

    cookie_path = get_cookie_file_for_url(url)
    valid_media_exts = ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.mp4', '.mkv', '.webm', '.mov')
    cj = get_requests_cookies(cookie_path)
    download_success = False

    if "instagram.com" in url:
        url = url.split('?')[0]
        headers_cdn = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Accept': '*/*'}

        native_items = scrape_instaloader(url, task_id)
        if not native_items: native_items = scrape_instagram_embed(url, task_id)
        if not native_items: native_items = scrape_cobalt_fleet(url, task_id)

        if native_items:
            download_success = True
            for idx, slide in enumerate(native_items):
                base_name = f"temp_yt_{task_id}_{idx:03d}"
                item_saved = False
                try:
                    if slide['is_video'] and slide['vid_url']:
                        r_v = requests.get(slide['vid_url'], headers=headers_cdn, cookies=cj, stream=True, timeout=25)
                        if r_v.status_code == 200:
                            with open(f"{DOWNLOAD_DIR}/{base_name}.mp4", 'wb') as f:
                                for chunk in r_v.iter_content(chunk_size=8192): f.write(chunk)
                            ensure_ios_compatible_video(f"{DOWNLOAD_DIR}/{base_name}.mp4")
                            item_saved = True

                    if slide['img_url']:
                        r_i = requests.get(slide['img_url'], headers=headers_cdn, cookies=cj, timeout=15)
                        if r_i.status_code == 200 and 'image' in r_i.headers.get('Content-Type', '').lower():
                            with open(f"{DOWNLOAD_DIR}/{base_name}.jpg", 'wb') as f: f.write(r_i.content)
                            if not slide['is_video'] or not item_saved:
                                ensure_jpg_image(f"{DOWNLOAD_DIR}/{base_name}.jpg")
                                item_saved = True

                    if item_saved:
                        with open(f"{DOWNLOAD_DIR}/{base_name}.info.json", 'w', encoding='utf-8') as f:
                            json.dump({'title': slide['title']}, f)
                    else:
                        download_success = False
                except Exception as ex:
                    logger.error(f"[Instagram Task {task_id}] Failed to save slide {idx}: {ex}")
                    download_success = False

            if not download_success:
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(f"temp_yt_{task_id}_"):
                        try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                        except: pass

        if not download_success:
            logger.info(f"[Task {task_id}] Proxies blocked. Routing directly via yt-dlp...")
            ydl_opts_ig = {'extract_flat': 'in_playlist', 'ignoreerrors': True, 'quiet': True}
            if cookie_path: ydl_opts_ig['cookiefile'] = cookie_path
            
            info = None
            try:
                with yt_dlp.YoutubeDL(ydl_opts_ig) as ydl: info = ydl.extract_info(url, download=False)
            except: pass
                
            entries = []
            if info and 'entries' in info: entries = [e for e in info['entries'] if e]
            elif info: entries = [info]
                
            if entries:
                download_success = True
                for idx, e in enumerate(entries):
                    slide_url = e.get('url') or e.get('webpage_url') or url
                    base_name = f"temp_yt_{task_id}_{idx:03d}"
                    dl_opts = {'outtmpl': os.path.join(DOWNLOAD_DIR, f"{base_name}.%(ext)s"), 'format': 'best', 'quiet': True, 'postprocessor_args': {'ffmpeg': ['-movflags', '+faststart']}}
                    if cookie_path: dl_opts['cookiefile'] = cookie_path
                    
                    video_success = False
                    try:
                        with yt_dlp.YoutubeDL(dl_opts) as ydl_vid:
                            if ydl_vid.download([slide_url]) == 0:
                                video_success = True
                                vid_p = os.path.join(DOWNLOAD_DIR, f"{base_name}.mp4")
                                if os.path.exists(vid_p): ensure_ios_compatible_video(vid_p)
                                with open(os.path.join(DOWNLOAD_DIR, f"{base_name}.info.json"), 'w', encoding='utf-8') as f: 
                                    json.dump({'title': "Media"}, f)
                    except: pass
                    
                    if not video_success:
                        raw_opts = {'quiet': True, 'ignoreerrors': True}
                        if cookie_path: raw_opts['cookiefile'] = cookie_path
                        try:
                            with yt_dlp.YoutubeDL(raw_opts) as ydl_raw:
                                slide_info = ydl_raw.extract_info(slide_url, download=False, process=False)
                                img_url = None
                                if slide_info:
                                    if slide_info.get('thumbnails'): img_url = slide_info['thumbnails'][-1]['url']
                                    elif slide_info.get('url'): img_url = slide_info['url']
                                if not img_url: img_url = e.get('thumbnail')
                                if img_url:
                                    r_img = requests.get(img_url, headers=headers_cdn, timeout=15)
                                    if r_img.status_code == 200 and 'image' in r_img.headers.get('Content-Type', '').lower():
                                        with open(os.path.join(DOWNLOAD_DIR, f"{base_name}.jpg"), 'wb') as f: f.write(r_img.content)
                                        ensure_jpg_image(os.path.join(DOWNLOAD_DIR, f"{base_name}.jpg"))
                                        with open(os.path.join(DOWNLOAD_DIR, f"{base_name}.info.json"), 'w', encoding='utf-8') as f: 
                                            json.dump({'title': "Photo"}, f)
                                    else: download_success = False
                                else: download_success = False
                        except:
                            download_success = False
    else:
        ydl_opts = {
            'outtmpl': f'{DOWNLOAD_DIR}/temp_yt_{task_id}_%(autonumber)03d_%(id)s.%(ext)s',
            'format': 'bestvideo[vcodec^=avc]+bestaudio[ext=m4a]/best[ext=mp4]/best', 
            'merge_output_format': 'mp4',
            'writeinfojson': True,
            'writethumbnail': True,
            'noplaylist': False,
            'ignoreerrors': True,
            'postprocessor_args': {'ffmpeg': ['-movflags', '+faststart']}, 
            'progress_hooks': [lambda d: my_hook(d, task_id, user_id)]
        }
        if cookie_path: ydl_opts['cookiefile'] = cookie_path
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.extract_info(url, download=True)
        except Exception as e:
            logger.error(f"yt-dlp extract error: {e}")

    try:
        active_downloads[task_id] = "Processing Data..."
        media_files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"temp_yt_{task_id}_")]

        if media_files:
            media_files.sort()
            
            bases = {}
            for f in media_files:
                base = f.split('.info.json')[0] if f.endswith('.info.json') else f.rsplit('.', 1)[0]
                if base not in bases: bases[base] = []
                bases[base].append(f)

            valid_bases = {}
            for b, files in bases.items():
                if any(f.endswith(valid_media_exts) for f in files):
                    valid_bases[b] = files
            bases = valid_bases

            if len(bases) == 1:
                base = list(bases.keys())[0]
                files = bases[base]
                
                primary = next((f for f in files if f.endswith(('.mp4', '.webm', '.mkv', '.mov'))), None)
                if not primary:
                    primary = next((f for f in files if f.endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic'))), files[0])
                if not primary.endswith(valid_media_exts): return

                ext_found = primary.rsplit('.', 1)[1].lower()
                if ext_found == 'jpeg': ext_found = 'jpg'
                info_file = next((os.path.join(DOWNLOAD_DIR, jf) for jf in files if jf.endswith(".info.json")), None)
                
                new_id = generate_secure_id()
                new_media = os.path.join(DOWNLOAD_DIR, f"{new_id}.{ext_found}")
                os.rename(os.path.join(DOWNLOAD_DIR, primary), new_media)
                
                if ext_found in ['mp4', 'mov', 'mkv', 'webm']:
                    active_downloads[task_id] = "Optimizing for Mobile..."
                    new_media = ensure_ios_compatible_video(new_media)
                    ext_found = new_media.rsplit('.', 1)[1].lower()
                else:
                    new_media = ensure_jpg_image(new_media)
                    ext_found = new_media.rsplit('.', 1)[1].lower()
                
                extracted_title = None
                thumb_downloaded = False
                
                for f in files:
                    if f != primary and f.endswith(('.jpg', '.jpeg', '.webp', '.png')):
                        thumb_ext = f.rsplit('.', 1)[1].lower()
                        if ext_found in ['jpg', 'png', 'webp']:
                            try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                            except: pass
                        else:
                            if thumb_ext == 'jpeg': thumb_ext = 'jpg'
                            os.rename(os.path.join(DOWNLOAD_DIR, f), os.path.join(DOWNLOAD_DIR, f"{new_id}.{thumb_ext}"))
                            ensure_jpg_image(os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg"))
                            thumb_downloaded = True
                            break 
                            
                if not thumb_downloaded and ext_found in ['mp4', 'webm', 'mkv', 'mov']:
                    subprocess.run(["ffmpeg", "-y", "-i", new_media, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{new_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if info_file and os.path.exists(info_file):
                    try:
                        with open(info_file, 'r', encoding='utf-8') as inf_f: 
                            info_data = json.load(inf_f)
                            t = info_data.get('title')
                            d = info_data.get('description')
                            if t and t.strip() and "Instagram" not in t: extracted_title = t
                            elif d and d.strip(): extracted_title = d
                            elif t: extracted_title = t
                    except: pass
                
                extract_true_duration(new_id, user_id, url, extracted_title, f".{ext_found}", expire_days, engine="🖼️ Image" if ext_found in ['jpg', 'png', 'webp', 'jpeg'] else None)

            elif len(bases) > 1:
                new_id = generate_secure_id()
                html_path = os.path.join(DOWNLOAD_DIR, f"{new_id}.html")
                
                carousel_tags = ""
                sorted_bases = sorted(bases.keys())
                
                idx_counter = 0
                has_video = False
                total_duration = 0.0

                for base in sorted_bases:
                    files = bases[base]
                    primary = next((f for f in files if f.endswith(('.mp4', '.webm', '.mkv', '.mov'))), None)
                    if not primary:
                        primary = next((f for f in files if f.endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic'))), files[0])

                    if not primary.endswith(valid_media_exts): continue
                        
                    ext = primary.rsplit('.', 1)[1].lower()
                    if ext == 'jpeg': ext = 'jpg'
                    new_media_name = f"{new_id}_{idx_counter}.{ext}"
                    new_media_path = os.path.join(DOWNLOAD_DIR, new_media_name)
                    os.rename(os.path.join(DOWNLOAD_DIR, primary), new_media_path)
                    
                    if ext in ['mp4', 'mov', 'mkv', 'webm']:
                        has_video = True
                        active_downloads[task_id] = "Optimizing for Mobile..."
                        new_media_path = ensure_ios_compatible_video(new_media_path)
                        actual_ext = new_media_path.rsplit('.', 1)[1].lower()
                        new_media_name = f"{new_id}_{idx_counter}.{actual_ext}"
                        
                        try:
                            res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", new_media_path], capture_output=True, text=True)
                            total_duration += float(res.stdout.strip())
                        except Exception: pass

                        carousel_tags += f"<div class='carousel-item' data-type='video'><video src='/videos/{new_media_name}' autoplay muted playsinline loop></video></div>"
                    else:
                        new_media_path = ensure_jpg_image(new_media_path)
                        actual_ext = new_media_path.rsplit('.', 1)[1].lower()
                        new_media_name = f"{new_id}_{idx_counter}.{actual_ext}"
                        carousel_tags += f"<div class='carousel-item' data-type='image'><img src='/videos/{new_media_name}'></div>"
                    idx_counter += 1

                extracted_title = "Media Carousel"
                first_info = next((os.path.join(DOWNLOAD_DIR, f) for b in sorted_bases for f in bases[b] if f.endswith(".info.json")), None)
                if first_info and os.path.exists(first_info):
                    try:
                        with open(first_info, 'r', encoding='utf-8') as inf_f: 
                            info_data = json.load(inf_f)
                            t = info_data.get('title')
                            d = info_data.get('description')
                            if t and t.strip() and "Instagram" not in t: extracted_title = t
                            elif d and d.strip(): extracted_title = d
                            elif t: extracted_title = t
                    except: pass

                gallery_html = generate_carousel_html(carousel_tags, extracted_title)
                with open(html_path, "w", encoding="utf-8") as f: f.write(gallery_html)

                first_base = sorted_bases[0]
                first_files = bases[first_base]
                first_primary = next((f for f in first_files if f.endswith(('.mp4', '.webm', '.mkv', '.mov'))), None)
                if not first_primary:
                    first_primary = next((f for f in first_files if f.endswith(('.jpg', '.jpeg', '.png', '.webp'))), first_files[0])
                first_ext = first_primary.rsplit('.', 1)[1].lower()
                if first_ext == 'jpeg': first_ext = 'jpg'

                if first_ext in ['mp4', 'webm', 'mkv', 'mov']:
                    subprocess.run(["ffmpeg", "-y", "-i", os.path.join(DOWNLOAD_DIR, f"{new_id}_0.{first_ext}"), "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{new_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    try: shutil.copy(os.path.join(DOWNLOAD_DIR, f"{new_id}_0.{first_ext}"), os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg"))
                    except: pass

                c_type = "carousel_media" if has_video else "carousel_image"
                extract_true_duration(new_id, user_id, url, extracted_title, ".html", expire_days, engine="🎡 Carousel", carousel_type=c_type, total_duration=total_duration)

        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(f"temp_yt_{task_id}_"):
                try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                except: pass

    finally:
        if task_id in active_downloads: del active_downloads[task_id]

def process_local_carousel(files_paths: list, filenames: list, user_id: str, task_id: str, expire_days: int):
    new_id = generate_secure_id()
    html_path = os.path.join(DOWNLOAD_DIR, f"{new_id}.html")
    carousel_tags = ""
    has_video = False
    total_duration = 0.0

    try:
        active_downloads[task_id] = "Building Carousel..."
        
        for idx, (temp_path, original_name) in enumerate(zip(files_paths, filenames)):
            ext = os.path.splitext(original_name)[1].lower()
            if not ext: ext = ".mp4"
            if ext == '.jpeg': ext = '.jpg'

            new_media_name = f"{new_id}_{idx}{ext}"
            new_media_path = os.path.join(DOWNLOAD_DIR, new_media_name)
            
            if ext in [".mp4", ".mov", ".avi", ".mkv", ".webm"]:
                has_video = True
                active_downloads[task_id] = f"Converting File {idx+1}..."
                subprocess.run(["ffmpeg", "-y", "-i", temp_path, "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-movflags", "+faststart", new_media_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                actual_ext = ".mp4"
                new_media_name = f"{new_id}_{idx}{actual_ext}"
                
                try:
                    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", new_media_path], capture_output=True, text=True)
                    total_duration += float(res.stdout.strip())
                except Exception: pass

                carousel_tags += f"<div class='carousel-item' data-type='video'><video src='/videos/{new_media_name}' autoplay muted playsinline loop></video></div>"
            else:
                shutil.move(temp_path, new_media_path)
                new_media_path = ensure_jpg_image(new_media_path)
                actual_ext = os.path.splitext(new_media_path)[1].lower()
                new_media_name = f"{new_id}_{idx}{actual_ext}"
                carousel_tags += f"<div class='carousel-item' data-type='image'><img src='/videos/{new_media_name}'></div>"
                
            try: os.remove(temp_path)
            except: pass

        extracted_title = "Uploaded Carousel"
        gallery_html = generate_carousel_html(carousel_tags, extracted_title)
        with open(html_path, "w", encoding="utf-8") as f: f.write(gallery_html)

        first_ext = os.path.splitext(filenames[0])[1].lower()
        if first_ext in [".mp4", ".mov", ".avi", ".mkv", ".webm"]:
            subprocess.run(["ffmpeg", "-y", "-i", os.path.join(DOWNLOAD_DIR, f"{new_id}_0.mp4"), "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{new_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try: shutil.copy(os.path.join(DOWNLOAD_DIR, f"{new_id}_0.jpg"), os.path.join(DOWNLOAD_DIR, f"{new_id}.jpg"))
            except: pass

        c_type = "carousel_media" if has_video else "carousel_image"
        extract_true_duration(new_id, user_id, custom_title=extracted_title, ext=".html", expire_days=expire_days, engine="🎡 Carousel", carousel_type=c_type, total_duration=total_duration)
        
    except Exception as e:
        logger.error(f"Local carousel error: {e}")
    finally:
        if task_id in active_downloads: del active_downloads[task_id]

def convert_local_file(input_path: str, final_path: str, video_id: str, user_id: str, task_id: str, original_filename: str, expire_days: int):
    active_downloads[task_id] = "Converting..."
    subprocess.run(["ffmpeg", "-i", input_path, "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-movflags", "+faststart", final_path, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-i", final_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{video_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(input_path)
    extract_true_duration(video_id, user_id, custom_title=original_filename, expire_days=expire_days)
    if task_id in active_downloads: del active_downloads[task_id]

@app.get("/view/{video_id}")
def view_media(video_id: str):
    safe_id = os.path.basename(video_id)
    with db_lock:
        db = load_db()
        vid = db["videos"].get(safe_id)
    if not vid: return RedirectResponse("/")
    
    ext = vid.get("ext", ".mp4")
    if ext == ".html": return RedirectResponse(f"/videos/{safe_id}.html")
        
    media_url = f"/videos/{safe_id}{ext}"
    title = html.escape(vid.get("title", safe_id))
    
    if ext in [".mp4", ".webm", ".mkv", ".mov"]:
        content = f'<video src="{media_url}" controls autoplay muted playsinline loop style="max-width:100%; max-height:100%; width:auto; height:auto; object-fit:contain; outline:none; display:block; margin:auto;"></video>'
    else:
        content = f'<img src="{media_url}" style="max-width:100%; max-height:100%; width:auto; height:auto; object-fit:contain; display:block; margin:auto;">'
        
    html_content = f"""
    <!DOCTYPE html>
    <html><head><meta charset='utf-8'><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover"><title>{title}</title>
    <style>
        * {{ box-sizing: border-box; }}
        html, body {{ margin:0; padding:0; background:#000; width:100vw; height:100vh; height:100dvh; min-height:-webkit-fill-available; display:flex; align-items:center; justify-content:center; overflow:hidden; }}
        .media-container {{ width:100vw; height:100vh; height:100dvh; display:flex; align-items:center; justify-content:center; padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left); }}
    </style>
    </head><body><div class="media-container">{content}</div></body></html>
    """
    return HTMLResponse(html_content)

@app.post("/api/login")
def login(username: str = Form(...), password: str = Form(...)):
    hashed = hashlib.sha256(password.encode()).hexdigest()
    with db_lock:
        db = load_db()
        users = db.get("users", {})
        if username in users and secrets.compare_digest(users[username].get("password", ""), hashed):
            token = users[username].get("token")
            if not token:
                token = secrets.token_urlsafe(32)
                users[username]["token"] = token
                save_db(db)
            response = Response(content=json.dumps({"status": "success"}), media_type="application/json")
            response.set_cookie(key="upshare_session", value=token, max_age=SESSION_DAYS*86400, httponly=True, samesite="lax")
            return response
    raise StarletteHTTPException(status_code=401, detail="Invalid credentials")

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("upshare_session")
    return {"status": "success"}

@app.post("/api/download_form")
async def download_form(background_tasks: BackgroundTasks, url: str = Form(...), expire_days: int = Form(0), confirm_override: bool = Form(False), user: dict = Depends(verify_auth)):
    task_id = generate_secure_id()
    active_downloads[task_id] = "Queued..."
    background_tasks.add_task(process_yt_dlp, url, user["username"], task_id, expire_days)
    return {"status": "processing", "task_id": task_id}

@app.post("/api/upload")
async def upload_file_endpoint(background_tasks: BackgroundTasks, file: list[UploadFile] = File(...), expire_days: int = Form(0), user: dict = Depends(verify_auth)):
    task_id = generate_secure_id()
    
    if len(file) > 1:
        temp_paths = []
        filenames = []
        for f in file:
            t_path = os.path.join(DOWNLOAD_DIR, f"temp_{generate_secure_id()}{os.path.splitext(f.filename)[1].lower()}")
            with open(t_path, "wb") as buffer: shutil.copyfileobj(f.file, buffer)
            temp_paths.append(t_path)
            filenames.append(f.filename)
        background_tasks.add_task(process_local_carousel, temp_paths, filenames, user["username"], task_id, expire_days)
        return {"status": "processing", "task_id": task_id}
    else:
        video_id = generate_secure_id()
        ext = os.path.splitext(file[0].filename)[1].lower()
        if not ext: ext = ".mp4"
        
        temp_path = os.path.join(DOWNLOAD_DIR, f"temp_{video_id}{ext}")
        final_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
        
        with open(temp_path, "wb") as buffer: shutil.copyfileobj(file[0].file, buffer)
            
        if ext in [".mp4", ".mov", ".avi", ".mkv", ".webm"]:
            background_tasks.add_task(convert_local_file, temp_path, final_path, video_id, user["username"], task_id, file[0].filename, expire_days)
        else:
            target_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}")
            shutil.move(temp_path, target_path)
            extract_true_duration(video_id, user["username"], custom_title=file[0].filename, ext=ext, expire_days=expire_days)
            
        return {"status": "processing", "video_id": video_id}

@app.post("/api/edit/{video_id}")
async def edit_video(video_id: str, background_tasks: BackgroundTasks, start: str = Form(...), end: str = Form(...), mode: str = Form(...), user: dict = Depends(verify_auth)):
    if not re.match(r'^[\d\.:]+$', start) or not re.match(r'^[\d\.:]+$', end): raise StarletteHTTPException(status_code=400, detail="Invalid timestamps")
    if mode not in ["copy", "overwrite"]: raise StarletteHTTPException(status_code=400, detail="Invalid mode")

    safe_id = os.path.basename(video_id)
    with db_lock:
        db = load_db()
        vid = db["videos"].get(safe_id)
        if not vid or (user["role"] != "admin" and vid["owner"] != user["username"]): raise StarletteHTTPException(status_code=403, detail="Forbidden")

    ext = vid.get("ext", ".mp4")
    input_path = os.path.join(DOWNLOAD_DIR, f"{safe_id}{ext}")
    if not os.path.exists(input_path): raise StarletteHTTPException(status_code=404, detail="Not found")

    def run_edit():
        task_id = generate_secure_id()
        active_downloads[task_id] = "Clipping Media..."
        dur = max(0, float(end) - float(start))
        
        if mode == "copy":
            new_id = generate_secure_id()
            out_path = os.path.join(DOWNLOAD_DIR, f"{new_id}{ext}")
            subprocess.run(["ffmpeg", "-ss", start, "-i", input_path, "-t", str(dur), "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-movflags", "+faststart", out_path, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["ffmpeg", "-y", "-i", out_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{new_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            extract_true_duration(new_id, user["username"], custom_title=f"Clip - {vid.get('title', new_id)}", ext=ext)
        else:
            temp_out = os.path.join(DOWNLOAD_DIR, f"temp_edit_{safe_id}{ext}")
            subprocess.run(["ffmpeg", "-ss", start, "-i", input_path, "-t", str(dur), "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-movflags", "+faststart", temp_out, "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.move(temp_out, input_path)
            subprocess.run(["ffmpeg", "-y", "-i", input_path, "-ss", "00:00:00.100", "-vframes", "1", "-q:v", "2", f"{DOWNLOAD_DIR}/{safe_id}.jpg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            try:
                res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path], capture_output=True, text=True)
                with db_lock:
                    db = load_db()
                    if safe_id in db["videos"]:
                        db["videos"][safe_id]["duration"] = float(res.stdout.strip())
                        save_db(db)
            except: pass
            
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
    return FileResponse(target_file, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

@app.get("/api/videos")
def list_videos(user: dict = Depends(verify_auth)):
    with db_lock: db = load_db()
    videos_data = []

    dir_files = os.listdir(DOWNLOAD_DIR)
    file_sizes = {}
    for f in dir_files:
        if not f.startswith('temp_'):
            try: file_sizes[f] = os.path.getsize(os.path.join(DOWNLOAD_DIR, f))
            except: pass

    for f in dir_files:
        if f.endswith(('.mp4', '.webm', '.mkv', '.html', '.jpg', '.png', '.webp')) and not f.startswith('temp_'):
            base_name = f.rsplit('.', 1)[0]
            vid_info = db["videos"].get(base_name)
            
            if not vid_info: continue
            
            expected_ext = vid_info.get("ext")
            if expected_ext:
                if f != f"{base_name}{expected_ext}": continue
            else:
                if f.endswith(('.jpg', '.png', '.webp')) and any(e in dir_files for e in [f"{base_name}.mp4", f"{base_name}.webm", f"{base_name}.mkv", f"{base_name}.html"]):
                    continue

            if user["role"] != "admin" and vid_info.get("owner") != user["username"]: continue

            added_timestamp = vid_info.get("added", 0)
            engine = vid_info.get("engine", "")
            
            type_str = "video"
            if f.endswith('.html'):
                if "Carousel" in engine:
                    type_str = vid_info.get("carousel_type", "carousel_media")
                else:
                    type_str = "article"
            elif f.endswith(('.jpg', '.png', '.webp')):
                type_str = "image"

            total_size_bytes = 0
            for pf in file_sizes:
                if pf == base_name or pf.startswith(f"{base_name}.") or pf.startswith(f"{base_name}_"):
                    total_size_bytes += file_sizes[pf]

            thumb = next((f"{base_name}{e}" for e in ['.jpg', '.webp', '.png'] if f"{base_name}{e}" in file_sizes), None)
            mins, secs = divmod(int(vid_info.get("duration", 0)), 60)
            date_str = time.strftime("%b %d", time.localtime(added_timestamp)) if added_timestamp else "Unknown"

            videos_data.append({
                "id": base_name,
                "filename": f,
                "type": type_str,
                "title": vid_info.get("title", f),
                "original_url": vid_info.get("url", "#"),
                "domain": vid_info.get("domain", "unknown"),
                "thumbnail": thumb,
                "duration": f"{mins}:{secs:02d}",
                "engine": engine,
                "size_bytes": total_size_bytes,
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
    for f in os.listdir(DOWNLOAD_DIR):
        if f == safe_id or f.startswith(f"{safe_id}.") or f.startswith(f"{safe_id}_"):
            fp = os.path.join(DOWNLOAD_DIR, f)
            try:
                os.remove(fp)
                deleted = True
            except: pass
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

@app.get("/api/stats")
def get_stats(user: dict = Depends(verify_auth)):
    with db_lock:
        db = load_db()
        users = db.get("users", {})
        videos = db.get("videos", {})
        
    user_vid_count = sum(1 for v in videos.values() if user["role"] == "admin" or v.get("owner") == user["username"])
    
    used_disk = 0
    for f in os.listdir(DOWNLOAD_DIR):
        if not f.startswith('temp_'):
            base = f.split('.')[0] if '.' in f else f
            if '_' in base and base.count('_') > 1: base = base.rsplit('_', 1)[0]
            
            vid = videos.get(base)
            if vid and (user["role"] == "admin" or vid.get("owner") == user["username"]):
                try: used_disk += os.path.getsize(os.path.join(DOWNLOAD_DIR, f))
                except: pass
                
    user_data = users.get(user["username"], {})
    return {
        "role": user["role"],
        "video_count": user_vid_count,
        "used_disk": used_disk,
        "user_bandwidth": user_data.get("bandwidth", 0),
        "bandwidth": db.get("server_bandwidth", 0)
    }

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
            "warning_mb": 150,
            "bandwidth": 0
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

@app.get("/api/env")
def get_env():
    with db_lock:
        db = load_db()
        return {"login_msg": db.get("settings", {}).get("login_msg", os.getenv("LOGIN_CONTACT_MSG", ""))}
        
@app.get("/icon.svg")
def get_favicon(): return FileResponse("icon.svg")

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open("index.html", "r", encoding='utf-8') as f: return f.read()

async def cleanup_expired_media():
    while True:
        now = time.time()
        with db_lock:
            db = load_db()
            to_delete = []
            for vid, data in db.get("videos", {}).items():
                exp = data.get("expires_at", 0)
                if exp > 0 and now > exp: to_delete.append(vid)
            for vid in to_delete: _delete_video_internal(vid, db)
            if to_delete: save_db(db)
        await asyncio.sleep(3600)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_expired_media())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)