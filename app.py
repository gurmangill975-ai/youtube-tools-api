import os
import shutil
import requests
from flask import Flask, request, jsonify, Response, send_file, render_template
from flask_cors import CORS
import downloader

app = Flask(__name__)

# Enable CORS for full API support (cross-origin frontend/mobile integration)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Ensure templates directory exists
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(TEMPLATES_DIR, exist_ok=True)


@app.route('/')
def home():
    """Developer API Playground dashboard."""
    return render_template('playground.html')


@app.route('/health', methods=['GET'])
@app.route('/api/health', methods=['GET'])
def health_check():
    """
    Health check endpoint for deployment monitoring (Render, Railway, AWS, Heroku).
    """
    ffmpeg_installed = bool(shutil.which('ffmpeg'))
    return jsonify({
        'status': 'healthy',
        'service': 'youtube-tools-api',
        'ffmpeg_available': ffmpeg_installed
    })


@app.route('/api/search', methods=['GET'])
def search_videos():
    """
    Search YouTube videos by title, keywords, playlist, or channel.
    Query params: ?q=<query>&limit=20&page=1
    """
    query = request.args.get('q') or request.args.get('query')
    limit = request.args.get('limit', 20)
    page = request.args.get('page', 1)

    try:
        limit = int(limit)
        page = int(page)
    except ValueError:
        limit = 20
        page = 1

    if not query:
        return jsonify({'error': 'Parameter "q" or "query" is required'}), 400

    try:
        data = downloader.search_videos(query, limit=limit, page=page)
        return jsonify({
            'success': True,
            'query': query,
            'total_fetched': data['total_fetched'],
            'page': data['page'],
            'limit': data['limit'],
            'results': data['results']
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/info', methods=['GET'])
def get_info():
    """
    Extract video info and available formats.
    Query param: ?url=<youtube_url>
    """
    video_url = request.args.get('url')
    if not video_url:
        return jsonify({'error': 'Parameter "url" is required'}), 400

    try:
        info = downloader.extract_video_info(video_url)
        return jsonify({'success': True, 'data': info})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/stream-url', methods=['GET'])
def get_stream_url():
    """
    Get direct stream URL and headers for a video.
    Query params: ?url=<youtube_url>&format_id=<format_id>
    """
    video_url = request.args.get('url')
    format_id = request.args.get('format_id', 'best')
    if not video_url:
        return jsonify({'error': 'Parameter "url" is required'}), 400

    try:
        stream_data = downloader.get_stream_url(video_url, format_id)
        return jsonify({'success': True, 'data': stream_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/stream', methods=['GET'])
def stream_proxy():
    """
    Proxy video stream with Range Request support for browser playback/seeking.
    Query params: ?url=<youtube_url>&format_id=<format_id>
    """
    video_url = request.args.get('url')
    format_id = request.args.get('format_id', 'best')
    if not video_url:
        return jsonify({'error': 'Parameter "url" is required'}), 400

    try:
        stream_info = downloader.get_stream_url(video_url, format_id)
        target_url = stream_info['stream_url']
        target_headers = stream_info.get('headers', {})
    except Exception as e:
        return jsonify({'error': f'Failed to resolve stream URL: {str(e)}'}), 500

    # Forward client Range header if present
    req_headers = dict(target_headers)
    client_range = request.headers.get('Range')
    if client_range:
        req_headers['Range'] = client_range

    try:
        r = requests.get(target_url, headers=req_headers, stream=True, timeout=15)
    except Exception as e:
        return jsonify({'error': f'Failed to proxy stream: {str(e)}'}), 502

    # Forward relevant HTTP response headers from YouTube
    response_headers = {}
    for header_name in ['Content-Type', 'Content-Range', 'Accept-Ranges', 'Content-Length']:
        if header_name in r.headers:
            response_headers[header_name] = r.headers[header_name]

    if 'Content-Type' not in response_headers:
        response_headers['Content-Type'] = 'video/mp4'

    def generate_chunks():
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        except Exception as e:
            app.logger.error(f"Error during stream chunking: {e}")

    return Response(
        generate_chunks(),
        status=r.status_code,
        headers=response_headers
    )


@app.route('/api/download/direct', methods=['GET', 'POST'])
def direct_download_link():
    """
    Vercel-Optimized Instant Download Endpoint.
    Returns direct stream URL and filename in <500ms without server disk storage or timeouts.
    Query/Body: url, quality ('360p', '720p', '1080p', 'best'), format_type ('video', 'audio', 'mp3')
    """
    data = request.get_json(silent=True) or {}
    video_url = data.get('url') or request.args.get('url')
    quality = data.get('quality') or request.args.get('quality', 'best')
    format_type = data.get('format_type') or request.args.get('format_type', 'video')

    if not video_url:
        return jsonify({'error': 'Parameter "url" is required'}), 400

    try:
        download_info = downloader.get_direct_download_link(video_url, quality=quality, format_type=format_type)
        return jsonify({
            'success': True,
            'data': download_info
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/download/proxy', methods=['GET'])
def download_proxy():
    """
    Direct Stream Download Proxy: Streams YouTube video/audio directly to client
    with Content-Disposition attachment header so the browser/app downloads it as a file.
    Query params: ?url=<youtube_url>&format_id=<format_id>&filename=<filename>
    """
    video_url = request.args.get('url')
    format_id = request.args.get('format_id', 'best')
    custom_name = request.args.get('filename')

    if not video_url:
        return jsonify({'error': 'Parameter "url" is required'}), 400

    try:
        stream_info = downloader.get_stream_url(video_url, format_id)
        target_url = stream_info['stream_url']
        target_headers = stream_info.get('headers', {})
        ext = stream_info.get('ext', 'mp4')
        title = stream_info.get('title', 'video')
    except Exception as e:
        return jsonify({'error': f'Failed to resolve stream URL: {str(e)}'}), 500

    if not custom_name:
        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).strip()
        custom_name = f"{safe_title}.{ext}"

    req_headers = dict(target_headers)
    client_range = request.headers.get('Range')
    if client_range:
        req_headers['Range'] = client_range

    try:
        r = requests.get(target_url, headers=req_headers, stream=True, timeout=15)
    except Exception as e:
        return jsonify({'error': f'Failed to proxy download: {str(e)}'}), 502

    response_headers = {
        'Content-Disposition': f'attachment; filename="{custom_name}"',
        'Content-Type': r.headers.get('Content-Type', 'application/octet-stream')
    }
    for header_name in ['Content-Range', 'Accept-Ranges', 'Content-Length']:
        if header_name in r.headers:
            response_headers[header_name] = r.headers[header_name]

    def generate_chunks():
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        except Exception as e:
            app.logger.error(f"Error during download proxy chunking: {e}")

    return Response(
        generate_chunks(),
        status=r.status_code,
        headers=response_headers
    )


@app.route('/api/download/start', methods=['POST'])
def start_download():
    """
    Start an asynchronous background download task.
    JSON payload: {"url": "<youtube_url>", "format_id": "best", "format_type": "video|audio|merged"}
    """
    data = request.get_json(silent=True) or {}
    video_url = data.get('url') or request.args.get('url')
    format_id = data.get('format_id') or request.args.get('format_id', 'best')
    format_type = data.get('format_type') or request.args.get('format_type', 'video')

    if not video_url:
        return jsonify({'error': 'Parameter "url" is required'}), 400

    try:
        task_id = downloader.start_download_task(
            url=video_url,
            format_id=format_id,
            format_type=format_type
        )
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Download task initiated in background.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/download/status/<task_id>', methods=['GET'])
def get_download_status(task_id):
    """
    Check the live status of a download task.
    """
    status_data = downloader.get_task_status(task_id)
    if not status_data:
        return jsonify({'success': False, 'error': 'Task ID not found'}), 404

    return jsonify({'success': True, 'task': status_data})


@app.route('/api/download/file/<task_id>', methods=['GET'])
def retrieve_file(task_id):
    """
    Download the finished video/audio file for a completed task.
    """
    task = downloader.get_task_status(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    if task['status'] != 'completed':
        return jsonify({
            'error': f'Task is not completed yet. Current status: {task["status"]}',
            'progress': task.get('progress', 0)
        }), 400

    filepath = task.get('filepath')
    filename = task.get('filename')

    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File not found on server'}), 404

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    env = os.environ.get('FLASK_ENV', 'development')

    print(f"Starting YouTube Tools API server on http://{host}:{port} (env: {env})")
    
    if env == 'production':
        try:
            from waitress import serve
            print("Running with Waitress production server...")
            serve(app, host=host, port=port)
        except ImportError:
            app.run(host=host, port=port)
    else:
        app.run(host=host, port=port, debug=True)
