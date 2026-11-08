'''Downloading videos/audio through yt-dlp.

yt-dlp is run as a separate process (python -m yt_dlp, or the yt-dlp
executable if the module isn't installed) so that it doesn't interact with
gevent's monkey patching. Its progress output is parsed in a greenlet and
exposed through a small JSON API used by the watch page and /downloads.
'''
from youtube import yt_app, util
import settings

import flask
from flask import request
import gevent
import gevent.lock
import collections
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
QUALITIES = ('best', '2160', '1440', '1080', '720', '480', '360')
KINDS = ('video', 'm4a')
MAX_JOBS_KEPT = 100

jobs = collections.OrderedDict()
_semaphore = None
_semaphore_size = None


def _get_semaphore():
    global _semaphore, _semaphore_size
    size = max(1, int(settings.ytdlp_max_concurrent or 1))
    if _semaphore is None or size != _semaphore_size:
        _semaphore = gevent.lock.BoundedSemaphore(size)
        _semaphore_size = size
    return _semaphore


def download_directory():
    directory = (settings.ytdlp_download_directory or '').strip()
    if not directory:
        directory = os.path.join(os.path.expanduser('~'), 'Downloads',
                                 'youtube-local')
    directory = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(directory, exist_ok=True)
    return directory


def ytdlp_command():
    '''Command prefix for invoking yt-dlp, or None if it's not available'''
    if importlib.util.find_spec('yt_dlp') is not None:
        return [sys.executable, '-m', 'yt_dlp']
    exe = shutil.which('yt-dlp')
    if exe:
        return [exe]
    return None


def ffmpeg_location():
    '''Path to ffmpeg if it can be found, else None'''
    configured = (settings.ytdlp_ffmpeg_location or '').strip().strip('"')
    if configured:
        configured = os.path.expanduser(configured)
        if os.path.isdir(configured):
            found = shutil.which('ffmpeg', path=configured)
            return found
        if os.path.isfile(configured):
            return configured
        return None
    return shutil.which('ffmpeg')


def build_args(video_id, kind, quality, ffmpeg):
    url = 'https://www.youtube.com/watch?v=' + video_id
    args = [
        '--no-playlist',
        '--no-mtime',
        '--newline',
        '--progress',
        '--quiet',
        '--no-warnings',
        '--encoding', 'utf-8',
        '--progress-template',
        'download:[ytl-progress] %(progress.downloaded_bytes)s '
        '%(progress.total_bytes)s %(progress.total_bytes_estimate)s '
        '%(progress.speed)s %(progress.eta)s',
        '--print', 'before_dl:[ytl-title] %(title)s',
        '--print', 'after_move:[ytl-file] %(filepath)s',
        '-P', download_directory(),
        '-o', '%(title)s [%(id)s].%(ext)s',
    ]
    if ffmpeg:
        args += ['--ffmpeg-location', ffmpeg, '--embed-metadata']

    if kind == 'm4a':
        if ffmpeg:
            # Prefer native m4a (AAC) so no re-encoding happens; otherwise
            # take the best audio and convert it.
            args += ['-f', 'bestaudio[ext=m4a]/bestaudio/best',
                     '-x', '--audio-format', 'm4a', '--audio-quality', '0']
        else:
            args += ['-f', 'bestaudio[ext=m4a]']
    else:
        height_filter = '' if quality == 'best' else '[height<=%s]' % quality
        if ffmpeg:
            # Best video + best audio, merged into an mp4. At equal
            # resolution prefer H.264/AAC so the file plays everywhere;
            # VP9/AV1 are only used when needed for higher resolutions.
            args += [
                '-f', 'bv*%s+ba/b%s' % (height_filter, height_filter),
                '-S', 'res,vcodec:h264,acodec:aac,ext:mp4:m4a',
                '--merge-output-format', 'mp4',
            ]
        else:
            # Without ffmpeg only pre-merged (progressive) formats work
            args += ['-f', 'b%s/b' % height_filter]

    if settings.route_tor == 2:
        args += ['--proxy', 'socks5://127.0.0.1:%d/' % settings.tor_port]

    args += ['--', url]
    return args


def _new_job(video_id, kind, quality):
    job = {
        'id': uuid.uuid4().hex[:12],
        'video_id': video_id,
        'kind': kind,
        'quality': quality,
        'title': None,
        'status': 'queued',     # queued, downloading, processing, finished, error, cancelled
        'part': 1,
        'percent': 0.0,
        'downloaded_bytes': 0,
        'total_bytes': None,
        'speed': None,
        'eta': None,
        'filepath': None,
        'filename': None,
        'error': None,
        'warning': None,
        'created': time.time(),
        '_process': None,
        '_cancel': False,
    }
    jobs[job['id']] = job
    while len(jobs) > MAX_JOBS_KEPT:
        oldest_id = next(iter(jobs))
        if jobs[oldest_id]['status'] in ('queued', 'downloading', 'processing'):
            break
        del jobs[oldest_id]
    return job


def _num(s):
    if s in (None, '', 'NA', 'None'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _handle_line(job, line):
    if line.startswith('[ytl-progress] '):
        parts = line.split()[1:]
        parts += [None] * (5 - len(parts))
        downloaded, total, total_est, speed, eta = (_num(p) for p in parts[:5])
        total = total or total_est
        if downloaded is not None:
            # yt-dlp downloads video and audio separately; a drop in bytes
            # means the next stream started
            if downloaded < job['downloaded_bytes'] and job['percent'] > 50:
                job['part'] += 1
            job['downloaded_bytes'] = downloaded
        job['total_bytes'] = total
        job['speed'] = speed
        job['eta'] = eta
        if total and downloaded is not None:
            job['percent'] = round(min(100.0, downloaded * 100.0 / total), 1)
            if job['percent'] >= 100:
                job['status'] = 'processing'
            else:
                job['status'] = 'downloading'
    elif line.startswith('[ytl-title] '):
        job['title'] = line[len('[ytl-title] '):]
    elif line.startswith('[ytl-file] '):
        job['filepath'] = line[len('[ytl-file] '):]
        job['filename'] = os.path.basename(job['filepath'])
    elif line.startswith('ERROR:'):
        job['error'] = line[len('ERROR:'):].strip()
    elif line.strip():
        job['_last_line'] = line.strip()


def _run_job(job):
    with _get_semaphore():
        if job['_cancel']:
            job['status'] = 'cancelled'
            return
        command = ytdlp_command()
        if command is None:
            job['status'] = 'error'
            job['error'] = ('yt-dlp is not installed. Install it with '
                            '"pip install yt-dlp".')
            return
        ffmpeg = ffmpeg_location()
        if not ffmpeg:
            if job['kind'] == 'video':
                job['warning'] = ('ffmpeg not found: downloading a pre-merged '
                                  'format, which is usually limited to 360p.')
            else:
                job['warning'] = 'ffmpeg not found: metadata will not be embedded.'
        try:
            args = build_args(job['video_id'], job['kind'], job['quality'],
                              ffmpeg)
        except OSError as e:
            job['status'] = 'error'
            job['error'] = 'Could not create download folder: ' + str(e)
            return

        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        creationflags = 0
        if os.name == 'nt':
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        job['status'] = 'downloading'
        try:
            proc = subprocess.Popen(
                command + args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env,
                creationflags=creationflags,
            )
        except OSError as e:
            job['status'] = 'error'
            job['error'] = 'Failed to start yt-dlp: ' + str(e)
            return
        job['_process'] = proc
        for raw_line in proc.stdout:
            _handle_line(job, raw_line.decode('utf-8', 'replace').rstrip('\r\n'))
        returncode = proc.wait()
        job['_process'] = None

        if job['_cancel']:
            job['status'] = 'cancelled'
        elif returncode == 0 and job['filepath'] and os.path.isfile(job['filepath']):
            job['status'] = 'finished'
            job['percent'] = 100.0
        else:
            job['status'] = 'error'
            if not job['error']:
                job['error'] = (job.get('_last_line')
                                or 'yt-dlp exited with code %d' % returncode)


def start_download(video_id, kind, quality):
    # Reuse an in-progress or finished job for the same request
    for job in reversed(jobs.values()):
        if (job['video_id'], job['kind'], job['quality']) != (video_id, kind, quality):
            continue
        if job['status'] in ('queued', 'downloading', 'processing'):
            return job
        if job['status'] == 'finished' and job['filepath'] and os.path.isfile(job['filepath']):
            return job
    job = _new_job(video_id, kind, quality)
    gevent.spawn(_run_job, job)
    return job


def public_job(job):
    return {k: v for k, v in job.items() if not k.startswith('_')}


def _validate():
    video_id = request.values.get('video_id', '')
    kind = request.values.get('kind', 'video')
    quality = request.values.get('quality', 'best')
    if not VIDEO_ID_RE.match(video_id):
        flask.abort(400, 'Invalid video id')
    if kind not in KINDS:
        flask.abort(400, 'Invalid kind')
    if quality not in QUALITIES:
        flask.abort(400, 'Invalid quality')
    return video_id, kind, quality


def _wants_json():
    return (request.values.get('format') == 'json'
            or request.accept_mimetypes.best == 'application/json')


@yt_app.route('/ytl-api/download', methods=['POST'])
def ytdlp_download():
    job = start_download(*_validate())
    if _wants_json():
        return flask.jsonify(public_job(job))
    return flask.redirect(util.URL_ORIGIN + '/downloads', 303)


@yt_app.route('/ytl-api/downloads', methods=['GET'])
def ytdlp_jobs_json():
    return flask.jsonify([public_job(j) for j in reversed(jobs.values())])


@yt_app.route('/ytl-api/downloads/<job_id>', methods=['GET'])
def ytdlp_job_json(job_id):
    job = jobs.get(job_id)
    if job is None:
        flask.abort(404)
    return flask.jsonify(public_job(job))


@yt_app.route('/ytl-api/downloads/<job_id>/cancel', methods=['POST'])
def ytdlp_cancel(job_id):
    job = jobs.get(job_id)
    if job is None:
        flask.abort(404)
    job['_cancel'] = True
    proc = job['_process']
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
    elif job['status'] == 'queued':
        job['status'] = 'cancelled'
    if _wants_json():
        return flask.jsonify(public_job(job))
    return flask.redirect(util.URL_ORIGIN + '/downloads', 303)


@yt_app.route('/ytl-api/downloads/<job_id>/file', methods=['GET'])
def ytdlp_file(job_id):
    '''Send the finished file to the browser (Save As)'''
    job = jobs.get(job_id)
    if job is None or job['status'] != 'finished' or not job['filepath']:
        flask.abort(404)
    path = job['filepath']
    if not os.path.isfile(path):
        flask.abort(410, 'File was moved or deleted')
    return flask.send_file(path, as_attachment=True,
                           download_name=os.path.basename(path))


def format_size(num):
    if not num:
        return ''
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if num < 1024 or unit == 'GiB':
            return '%.1f %s' % (num, unit) if unit != 'B' else '%d B' % num
        num /= 1024.0


@yt_app.route('/downloads', methods=['GET'])
def downloads_page():
    job_list = [public_job(j) for j in reversed(jobs.values())]
    any_active = any(j['status'] in ('queued', 'downloading', 'processing')
                     for j in job_list)
    return flask.render_template(
        'downloads.html',
        jobs=job_list,
        any_active=any_active,
        download_directory=download_directory(),
        ytdlp_available=ytdlp_command() is not None,
        ffmpeg_path=ffmpeg_location(),
        format_size=format_size,
    )
