# upShareMedia

<div align="center">
  <img src="./screenshot.png" alt="upShareMedia UI" width="800"/>
</div>

A self-hosted, lightweight media archiving and conversion console built for seamless mobile-first usage, rapid link sharing, and distraction-free content viewing.

## Features
- **Multi-User:** Full access control, per-user file isolation, force password resets.
- **Multi-Format Support:** Powered by `yt-dlp` to fetch remote media into high-compatibility `.mp4` video files, or fallback to an ad-free **Reader Mode** for news articles and web pages.
- **In-App Media Clipper & Cropper:** Lossless start/end timestamp clipping directly within the console—save as a new copy or overwrite the original file on disk without quality loss.
- **Auto-Expiration:** Set optional self-destruct countdowns (24 Hours, 7 Days, 30 Days) on fetch or upload to automatically manage Unraid array storage.
- **Local File Transcoding:** Drag-and-drop local media anywhere on the screen for automated FFmpeg conversion to standardized MP4s.
- **Link Sharing & Preview Optimization:** Asynchronous background view-tracking engine ensures generated media links load instantly when shared via iMessage, Discord, etc.
- **Stat Tracking:** Native real-time view counts, bandwidth monitoring, & disk space tracking.

## Environment Variables
| Variable | Description | Default |
| :--- | :--- | :--- |
| `APP_USERNAME` | Master administrator username initialized on first boot | `admin` |
| `APP_PASSWORD` | Master administrator password initialized on first boot | `adminpassword` |
| `PORT` | Internal port the application listens on | `29738` |
| `DOWNLOAD_DIR` | Internal container path for media storage | `/downloads` |
| `CONFIG_DIR` | Internal container path for persistent database storage (`v3_db.json`) | `/config` |
| `SESSION_DAYS` | Number of days before browser session token expires | `30` |
| `MAX_DOWNLOAD_MB` | File size warning threshold (in MB) before prompting for user confirmation | `150` |
| `LOGIN_CONTACT_MSG` | Optional welcome/contact message displayed on the login screen | `""` |
| `YTDLP_COOKIES` | Raw Netscape cookie string for authenticating YouTube/Instagram requests | `""` |
| `TIKTOK_COOKIES` | Raw Netscape cookie string specifically reserved for TikTok requests | `""` |