import os
import time
import uuid
import threading
import logging
import yt_dlp

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt_downloader")

# Directory where downloads will be cached temporarily
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
    DOWNLOAD_DIR = "/tmp/downloads"
else:
    DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Global dictionary to track download tasks
# Key: task_id (str), Value: task dict
DOWNLOAD_TASKS = {}
TASKS_LOCK = threading.Lock()


def format_bytes(bytes_num):
    """Format bytes into human readable format."""
    if not bytes_num or bytes_num <= 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.2f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.2f} PB"


def search_videos(query, limit=20, page=1):
    """
    Search YouTube videos by title, keywords, playlist, or channel.
    Supports pagination (limit up to 100, page 1..N) with robust fallback handling.
    """
    # Sanitize inputs
    limit = max(1, min(int(limit), 100))
    page = max(1, int(page))
    fetch_count = limit * page

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb', 'web']
            }
        }
    }

    search_target = query.strip()
    # If the user passed a direct video/playlist URL, use it directly, otherwise use ytsearch
    if not (search_target.startswith('http://') or search_target.startswith('https://')):
        search_target = f"ytsearch{fetch_count}:{search_target}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(search_target, download=False)
        except Exception as e:
            logger.error(f"Search error for query '{query}': {str(e)}")
            raise Exception(f"Failed to perform search: {str(e)}")

    raw_entries = []
    if info:
        if 'entries' in info:
            raw_entries = [e for e in info['entries'] if e]
        elif info.get('id'):
            raw_entries = [info]

    # Slice for target page
    start_idx = (page - 1) * limit
    page_entries = raw_entries[start_idx : start_idx + limit]

    results = []
    for entry in page_entries:
        try:
            video_id = entry.get('id') or entry.get('url', '').split('v=')[-1].split('&')[0]
            video_url = entry.get('webpage_url') or entry.get('url') or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
            
            if not video_url:
                continue

            duration = entry.get('duration')
            if duration and isinstance(duration, (int, float)):
                duration_human = time.strftime('%H:%M:%S', time.gmtime(duration))
            else:
                duration_human = 'Live/Unknown'

            thumbnails = entry.get('thumbnails', [])
            if thumbnails and isinstance(thumbnails, list):
                thumbnail_url = thumbnails[-1].get('url') if isinstance(thumbnails[-1], dict) else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            else:
                thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""

            results.append({
                'id': video_id,
                'title': entry.get('title', 'Untitled Video'),
                'uploader': entry.get('uploader') or entry.get('channel') or entry.get('uploader_id') or 'YouTube',
                'duration': duration or 0,
                'duration_human': duration_human,
                'view_count': entry.get('view_count') or 0,
                'url': video_url,
                'thumbnail': thumbnail_url
            })
        except Exception as item_err:
            logger.warning(f"Skipping malformed search item: {item_err}")
            continue

    return {
        'total_fetched': len(raw_entries),
        'page': page,
        'limit': limit,
        'results': results
    }


def extract_video_info(url):
    """
    Extract metadata and format breakdown for a YouTube video (including Kids & Restricted videos).
    Returns a structured dictionary of information.
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb', 'web']
            }
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.error(f"Error extracting info for {url}: {str(e)}")
            raise Exception(f"Failed to fetch YouTube info: {str(e)}")

    if not info:
        raise Exception("Could not retrieve video information.")

    formats_list = info.get('formats', [])
    combined_formats = []
    video_only_formats = []
    audio_only_formats = []

    for f in formats_list:
        vcodec = f.get('vcodec', 'none')
        acodec = f.get('acodec', 'none')
        filesize = f.get('filesize') or f.get('filesize_estimate') or 0
        ext = f.get('ext', '')
        format_id = f.get('format_id', '')
        resolution = f.get('resolution') or f"{f.get('width', '?')}x{f.get('height', '?')}"
        fps = f.get('fps')
        tbr = f.get('tbr')  # total average bitrate in Kbit/s
        asr = f.get('asr')  # audio sampling rate

        fmt_item = {
            'format_id': format_id,
            'extension': ext,
            'resolution': resolution,
            'width': f.get('width'),
            'height': f.get('height'),
            'fps': fps,
            'filesize': filesize,
            'filesize_human': format_bytes(filesize),
            'bitrate_kbit': tbr,
            'vcodec': vcodec,
            'acodec': acodec,
            'format_note': f.get('format_note', ''),
        }

        if vcodec != 'none' and acodec != 'none':
            combined_formats.append(fmt_item)
        elif vcodec != 'none' and acodec == 'none':
            video_only_formats.append(fmt_item)
        elif vcodec == 'none' and acodec != 'none':
            fmt_item['sample_rate'] = asr
            audio_only_formats.append(fmt_item)

    # Sort formats cleanly
    combined_formats.sort(key=lambda x: x.get('height') or 0, reverse=True)
    video_only_formats.sort(key=lambda x: x.get('height') or 0, reverse=True)
    audio_only_formats.sort(key=lambda x: x.get('bitrate_kbit') or 0, reverse=True)

    return {
        'id': info.get('id'),
        'title': info.get('title'),
        'description': info.get('description'),
        'duration': info.get('duration'),
        'duration_human': time.strftime('%H:%M:%S', time.gmtime(info.get('duration', 0))) if info.get('duration') else 'Live',
        'uploader': info.get('uploader') or info.get('channel'),
        'view_count': info.get('view_count'),
        'thumbnail': info.get('thumbnail'),
        'url': url,
        'formats': {
            'combined': combined_formats,
            'video_only': video_only_formats,
            'audio_only': audio_only_formats
        }
    }


def get_stream_url(url, format_id='best'):
    """
    Get direct stream URL and HTTP headers for a video format.
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb', 'web']
            }
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get('formats', [])
        
        target = None
        if format_id == 'best':
            # Find best combined format first, otherwise best video format
            for f in formats:
                if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                    target = f
                    break
            if not target and formats:
                target = formats[-1]
        else:
            for f in formats:
                if str(f.get('format_id')) == str(format_id):
                    target = f
                    break

        if not target:
            raise Exception(f"Format ID '{format_id}' not found for video.")

        return {
            'title': info.get('title', 'video'),
            'stream_url': target.get('url'),
            'headers': target.get('http_headers', {}),
            'ext': target.get('ext'),
            'vcodec': target.get('vcodec'),
            'acodec': target.get('acodec'),
        }


def get_direct_download_link(url, quality='best', format_type='video'):
    """
    Get direct stream link and filename for Vercel instant downloads.
    Bypasses server file creation & ffmpeg timeouts completely (<500ms response).
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb', 'web']
            }
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise Exception("Failed to retrieve video metadata.")

    title = info.get('title', 'video')
    # Sanitize title for filename
    safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).rstrip()
    
    formats = info.get('formats', [])
    target_format = None

    if format_type in ['audio', 'mp3', 'm4a']:
        # Find best audio format
        audio_formats = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
        audio_formats.sort(key=lambda x: x.get('tbr') or 0, reverse=True)
        if audio_formats:
            target_format = audio_formats[0]
        ext = 'mp3' if format_type == 'mp3' else (target_format.get('ext') if target_format else 'm4a')
    else:
        # Combined formats (video + audio) for fast single-url download
        combined = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none']
        combined.sort(key=lambda x: x.get('height') or 0, reverse=True)
        
        if combined:
            if quality == '360p':
                match = [f for f in combined if f.get('height') == 360]
                target_format = match[0] if match else combined[-1]
            elif quality == '720p':
                match = [f for f in combined if f.get('height') == 720]
                target_format = match[0] if match else combined[0]
            else:
                target_format = combined[0]
        elif formats:
            target_format = formats[-1]
        
        ext = target_format.get('ext', 'mp4') if target_format else 'mp4'

    if not target_format or not target_format.get('url'):
        raise Exception("No direct media stream found for this video.")

    filename = f"{safe_title}.{ext}"

    return {
        'title': title,
        'filename': filename,
        'quality': quality,
        'format_id': target_format.get('format_id'),
        'download_url': target_format.get('url'),
        'headers': target_format.get('http_headers', {}),
        'filesize_human': format_bytes(target_format.get('filesize') or target_format.get('filesize_estimate') or 0),
        'is_direct': True
    }


def start_download_task(url, format_id='best', format_type='video'):
    """
    Initiate an asynchronous background download task.
    Returns task_id string.
    """
    task_id = str(uuid.uuid4())
    task_info = {
        'task_id': task_id,
        'url': url,
        'format_id': format_id,
        'format_type': format_type,
        'status': 'pending',
        'progress': 0.0,
        'speed': '0 B/s',
        'eta': 'unknown',
        'downloaded_bytes': 0,
        'total_bytes': 0,
        'filename': None,
        'filepath': None,
        'title': None,
        'error': None,
        'created_at': time.time(),
        'completed_at': None
    }

    with TASKS_LOCK:
        DOWNLOAD_TASKS[task_id] = task_info

    thread = threading.Thread(
        target=_download_worker,
        args=(task_id, url, format_id, format_type),
        daemon=True
    )
    thread.start()

    return task_id


def get_task_status(task_id):
    """Retrieve status dictionary for a task."""
    with TASKS_LOCK:
        task = DOWNLOAD_TASKS.get(task_id)
        if not task:
            return None
        return dict(task)


def _download_worker(task_id, url, format_id, format_type):
    """Background thread function that runs yt-dlp to download and post-process."""
    logger.info(f"Starting download worker for task {task_id} (url={url}, format={format_id}, type={format_type})")

    def progress_hook(d):
        with TASKS_LOCK:
            task = DOWNLOAD_TASKS.get(task_id)
            if not task:
                return

            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                
                if total > 0:
                    percent = (downloaded / total) * 100.0
                    task['progress'] = round(min(percent, 99.0), 2)
                else:
                    task['progress'] = 0.0

                task['status'] = 'downloading'
                task['downloaded_bytes'] = downloaded
                task['total_bytes'] = total
                task['speed'] = d.get('_speed_str', '0 B/s').strip()
                task['eta'] = d.get('_eta_str', 'unknown').strip()

            elif d['status'] == 'finished':
                task['status'] = 'merging'
                task['progress'] = 99.0

    # Configure yt-dlp options based on format_type and format_id
    output_template = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title).100s.%(ext)s")

    ydl_opts = {
        'outtmpl': output_template,
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'retries': 10,
        'fragment_retries': 10,
        'concurrent_fragment_downloads': 4,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb', 'web']
            }
        }
    }

    if format_type == 'audio' or format_id == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif format_type == 'audio_m4a':
        ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
    elif format_id and format_id != 'best':
        # If user picked a specific video format (which might be video-only), combine with best audio
        ydl_opts['format'] = f"{format_id}+bestaudio/best/{format_id}"
        ydl_opts['merge_output_format'] = 'mp4'
    else:
        # Best default format (video + audio merged to mp4)
        ydl_opts['format'] = 'bestvideo+bestaudio/best'
        ydl_opts['merge_output_format'] = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_title = info.get('title', 'video')

        # Locate the downloaded file for this task
        downloaded_file = None
        for filename in os.listdir(DOWNLOAD_DIR):
            if filename.startswith(task_id):
                downloaded_file = os.path.join(DOWNLOAD_DIR, filename)
                break

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise Exception("Downloaded file could not be located after completion.")

        with TASKS_LOCK:
            task = DOWNLOAD_TASKS.get(task_id)
            if task:
                task['status'] = 'completed'
                task['progress'] = 100.0
                task['title'] = video_title
                task['filepath'] = downloaded_file
                task['filename'] = os.path.basename(downloaded_file)
                task['completed_at'] = time.time()
                logger.info(f"Task {task_id} completed successfully. File: {task['filename']}")

    except Exception as e:
        logger.error(f"Error in download task {task_id}: {str(e)}")
        with TASKS_LOCK:
            task = DOWNLOAD_TASKS.get(task_id)
            if task:
                task['status'] = 'failed'
                task['error'] = str(e)


def cleanup_expired_tasks(max_age_seconds=3600):
    """Clean up task files older than max_age_seconds."""
    now = time.time()
    with TASKS_LOCK:
        expired_ids = []
        for task_id, task in DOWNLOAD_TASKS.items():
            created_at = task.get('created_at', now)
            if now - created_at > max_age_seconds:
                expired_ids.append(task_id)

        for task_id in expired_ids:
            task = DOWNLOAD_TASKS.pop(task_id, None)
            if task and task.get('filepath') and os.path.exists(task['filepath']):
                try:
                    os.remove(task['filepath'])
                    logger.info(f"Cleaned up expired file for task {task_id}")
                except Exception as e:
                    logger.error(f"Failed to remove expired file {task['filepath']}: {e}")
