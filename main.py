from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import yt_dlp
import uuid
import os
import subprocess
import secrets
import json

app = FastAPI(title="upShareMP4")
security = HTTPBasic()

# --- ENVIRONMENT VARIABLES ---
# It will look for these in Unraid, but default to damal/secretpassword if missing
APP_USERNAME = os.getenv("APP_USERNAME", "damal")
APP_PASSWORD = os.getenv("APP_PASSWORD", "secretpassword")

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, APP_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
app.mount("/videos", StaticFiles(directory=DOWNLOAD_DIR), name="videos")

class VideoRequest(BaseModel):
    url: str

def process_video(url: str, video_id: str):
    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/{video_id}.%(ext)s',
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'writeinfojson': True,
        'writethumbnail': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def convert_local_file(input_path: str, output_path: str):
    subprocess.run(["ffmpeg", "-i", input_path, "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", output_path, "-y"])
    os.remove(input_path) 

def create_dummy_info(video_id: str, original_filename: str):
    info = {"title": original_filename, "webpage_url_domain": "localhost"}
    with open(f"{DOWNLOAD_DIR}/{video_id}.info.json", "w") as f:
        json.dump(info, f)

@app.post("/api/download")
async def trigger_download(req: VideoRequest, background_tasks: BackgroundTasks, user: str = Depends(verify_credentials)):
    video_id = f"vid_{str(uuid.uuid4())[:8]}"
    background_tasks.add_task(process_video, req.url, video_id)
    return {"status": "processing", "url": f"/videos/{video_id}.mp4"}

@app.post("/api/download_form")
async def form_download(background_tasks: BackgroundTasks, url: str = Form(...), user: str = Depends(verify_credentials)):
    video_id = f"vid_{str(uuid.uuid4())[:8]}"
    background_tasks.add_task(process_video, url, video_id)
    return {"status": "processing"}

@app.post("/api/upload")
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), user: str = Depends(verify_credentials)):
    video_id = f"vid_{str(uuid.uuid4())[:8]}"
    temp_path = os.path.join(DOWNLOAD_DIR, f"temp_{video_id}_{file.filename}")
    final_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    
    with open(temp_path, "wb") as buffer:
        buffer.write(await file.read())
        
    create_dummy_info(video_id, file.filename)
    background_tasks.add_task(convert_local_file, temp_path, final_path)
    return {"status": "processing"}

@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, user: str = Depends(verify_credentials)):
    safe_id = os.path.basename(video_id)
    deleted = False
    
    if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{safe_id}.mp4")):
        os.remove(os.path.join(DOWNLOAD_DIR, f"{safe_id}.mp4"))
        deleted = True
        
    if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{safe_id}.info.json")):
        os.remove(os.path.join(DOWNLOAD_DIR, f"{safe_id}.info.json"))
        
    for ext in ['.jpg', '.webp', '.png']:
        if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{safe_id}{ext}")):
            os.remove(os.path.join(DOWNLOAD_DIR, f"{safe_id}{ext}"))
            
    if deleted:
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Video not found")

@app.get("/api/videos")
def list_videos(user: str = Depends(verify_credentials)):
    videos_data = []
    for file in os.listdir(DOWNLOAD_DIR):
        if file.endswith('.mp4') and not file.startswith('temp_'):
            base_name = file.rsplit('.', 1)[0]
            info_file = os.path.join(DOWNLOAD_DIR, f"{base_name}.info.json")
            
            title = file
            domain = "unknown"
            thumbnail = None
            
            if os.path.exists(info_file):
                with open(info_file, 'r', encoding='utf-8') as f:
                    info = json.load(f)
                    title = info.get('title', file)
                    domain = info.get('webpage_url_domain', 'localhost')
            
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
                "time": os.path.getmtime(os.path.join(DOWNLOAD_DIR, file))
            })
            
    videos_data.sort(key=lambda x: x['time'], reverse=True)
    return {"videos": videos_data}

@app.get("/", response_class=HTMLResponse)
def read_root(user: str = Depends(verify_credentials)):
    with open("index.html", "r", encoding='utf-8') as f:
        return f.read()