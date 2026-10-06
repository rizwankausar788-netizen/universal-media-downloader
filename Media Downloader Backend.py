import os
import sys
import shutil
import tempfile
import urllib.parse
from typing import Optional, Dict, Any, List

# Core web & async server
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

try:
    import yt_dlp
except ImportError:
    print("\n[ERROR] yt-dlp is not installed!")
    print("Please install dependencies using: pip install fastapi uvicorn yt-dlp\n")
    sys.exit(1)

app = FastAPI(
    title="FETCH // Universal Media Stream Engine",
    description="Backend API powered by yt-dlp to extract and download media from all supported platforms.",
    version="3.4.0"
)

# Enable permissive Cross-Origin Resource Sharing (Allows GitHub Pages to call this backend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def format_bytes(size: Optional[int]) -> str:
    """Format file size in bytes to human-readable string (MB/GB)."""
    if not size or size <= 0:
        return "Clean Stream"
    power = 2**10
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB'}
    while size > power and n < 3:
        size /= power
        n += 1
    return f"{size:.1f} {power_labels[n]}"

def format_duration(seconds: Optional[int]) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    if not seconds:
        return "Unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

@app.get("/api/health")
async def health_check():
    """Health check endpoint used by the frontend status pill."""
    return {
        "status": "healthy",
        "engine": "yt-dlp",
        "version": yt_dlp.version.__version__,
        "supported_platforms": [
            "youtube", "instagram", "tiktok", "facebook", 
            "threads", "x", "pinterest", "reddit"
        ]
    }

@app.get("/api/info")
async def extract_media_info(url: str = Query(..., description="Target media URL to inspect")):
    """
    Extracts video/audio metadata, resolutions, thumbnails, and available streams
    without downloading the file to disk.
    """
    decoded_url = urllib.parse.unquote(url).strip()
    
    if not decoded_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL format. Must start with http:// or https://")

    # yt-dlp extraction configuration (Fast non-download probe)
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        'socket_timeout': 10,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(decoded_url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="Media could not be retrieved from this link.")

            # Identify platform extractor
            extractor = (info.get('extractor_key') or info.get('extractor') or 'Universal').lower()

            # Process available formats into clean format cards
            raw_formats: List[Dict[str, Any]] = info.get('formats', [])
            formatted_list = []

            # Prioritize standard resolutions
            has_1080p = False
            has_720p = False

            for f in reversed(raw_formats):
                height = f.get('height')
                vcodec = f.get('vcodec', 'none')
                ext = f.get('ext', 'mp4')
                filesize = f.get('filesize') or f.get('filesize_approx')

                # Filter valid video formats with actual streams
                if vcodec != 'none' and height:
                    if height >= 1080 and not has_1080p:
                        formatted_list.append({
                            "format_id": f.get('format_id', 'bestvideo+bestaudio/best'),
                            "resolution": f"{height}p",
                            "ext": ext,
                            "note": f"{f.get('fps', 30)}fps HD",
                            "filesize_str": format_bytes(filesize)
                        })
                        has_1080p = True
                    elif height == 720 and not has_720p:
                        formatted_list.append({
                            "format_id": f.get('format_id', 'bestvideo[height<=720]+bestaudio/best'),
                            "resolution": "720p",
                            "ext": ext,
                            "note": "Standard HD",
                            "filesize_str": format_bytes(filesize)
                        })
                        has_720p = True

            # Always offer MP3 / Audio format
            formatted_list.append({
                "format_id": "bestaudio/best",
                "resolution": "MP3 Audio",
                "ext": "mp3",
                "note": "320kbps High Quality",
                "filesize_str": format_bytes(info.get('filesize_approx', 9000000))
            })

            # If no complex video streams found (e.g. TikTok, IG reel), add best single stream
            if len(formatted_list) <= 1:
                formatted_list.insert(0, {
                    "format_id": "best",
                    "resolution": "Original HD",
                    "ext": info.get('ext', 'mp4'),
                    "note": "Source Quality",
                    "filesize_str": format_bytes(info.get('filesize', 0))
                })

            return {
                "title": info.get('title', 'Extracted Media'),
                "uploader": info.get('uploader') or info.get('channel') or info.get('creator') or 'Creator Stream',
                "duration_formatted": format_duration(info.get('duration')),
                "duration_seconds": info.get('duration', 0),
                "thumbnail": info.get('thumbnail') or (info.get('thumbnails')[-1]['url'] if info.get('thumbnails') else ''),
                "platform": extractor,
                "format_note": f"{info.get('vcodec', 'AVC1')} / {info.get('acodec', 'AAC')}",
                "is_video": not info.get('is_live', False),
                "formats": formatted_list
            }

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch media: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected extraction error: {str(e)}")

@app.get("/api/download")
async def download_media(
    url: str = Query(..., description="Target media URL to download"),
    format_id: Optional[str] = Query("best", description="Specific format_id to download")
):
    """
    Downloads the selected media from YouTube/IG/TikTok/etc. via yt-dlp,
    and streams it directly to the user's browser with attachment headers.
    """
    decoded_url = urllib.parse.unquote(url).strip()
    
    # Temporary working directory for the downloaded file
    temp_dir = tempfile.mkdtemp()
    output_template = os.path.join(temp_dir, "%(title).60s.%(ext)s")

    is_audio = "audio" in (format_id or "").lower() or format_id == "bestaudio/best"

    ydl_opts: Dict[str, Any] = {
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' if not is_audio else 'bestaudio/best',
        'noplaylist': True,
        'restrictfilenames': True,
    }

    # Audio postprocessing if MP3 selected
    if is_audio:
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(decoded_url, download=True)
            filename = ydl.prepare_filename(meta)
            
            if is_audio:
                filename = os.path.splitext(filename)[0] + ".mp3"

            if not os.path.exists(filename):
                # Search temp directory for produced output
                files = os.listdir(temp_dir)
                if files:
                    filename = os.path.join(temp_dir, files[0])
                else:
                    raise HTTPException(status_code=500, detail="Downloaded media file could not be located.")

            # Sanitize download display name
            display_name = os.path.basename(filename)

            # Stream file back to client, then remove temp files
            def cleanup():
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

            return FileResponse(
                path=filename,
                filename=display_name,
                media_type="application/octet-stream",
                background=cleanup
            )

    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Download execution error: {str(e)}")

if os.path.exists("index.html"):
    @app.get("/")
    async def serve_frontend():
        """Serves the premium single-page UI at the root path."""
        return FileResponse("index.html")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n=======================================================")
    print(f" FETCH MEDIA DOWNLOADER ENGINE LIVE")
    print(f" URL: http://localhost:{port}")
    print(f" Supported: YouTube, Instagram, TikTok, X, Reddit, FB")
    print(f"=======================================================\n")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)