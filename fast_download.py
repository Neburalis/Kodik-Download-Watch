from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
from hashlib import md5
from getters import get_download_link
import json
import requests
import os
import subprocess
import shutil
import stat
import threading
import time
from urllib.parse import urljoin, urlparse
import uuid

_download_locks = {}
_download_locks_guard = threading.Lock()
_NORMALIZATION_PROFILE = "h264-cfr-24000-1001-crf18-aac160-v1"
_LOCK_ROOT = '.tmp-locks'


def build_cache_hash(
    anime_id: object,
    id_type: str,
    translation_id: object,
    episode: int,
    quality: object,
    metadata: dict | None,
) -> str:
    payload = {
        "anime_id": str(anime_id),
        "id_type": str(id_type),
        "translation_id": str(translation_id),
        "episode": episode,
        "quality": str(quality),
        "normalization_profile": _NORMALIZATION_PROFILE,
        "metadata": metadata or {},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return md5(encoded).hexdigest() + "~"


@contextmanager
def _download_lock(hsh: str):
    with _download_locks_guard:
        entry = _download_locks.get(hsh)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _download_locks[hsh] = entry
        entry["users"] += 1
    entry["lock"].acquire()
    file_lock_fd = None
    try:
        os.makedirs(_LOCK_ROOT, exist_ok=True)
        file_lock_fd = os.open(
            os.path.join(_LOCK_ROOT, hsh + '.lock'),
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        fcntl.flock(file_lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        if file_lock_fd is not None:
            fcntl.flock(file_lock_fd, fcntl.LOCK_UN)
            os.close(file_lock_fd)
        entry["lock"].release()
        with _download_locks_guard:
            entry["users"] -= 1
            if entry["users"] == 0 and _download_locks.get(hsh) is entry:
                _download_locks.pop(hsh, None)

def fast_download(id: str, id_type: str, seria_num: int, translation_id: str, quality: str, token: str, filename: str = 'result', metadata: dict | None = None) -> tuple[str, str | None]:
    """
    Эта функция обеспечивает быструю загрузку засчет параллельной загрузки нескольких фрагментов.
    :id: Id сериала на Шикимори/Кинопоиске
    :id_type: тип id 'shikimori' или 'kinopoisk' ('sh' или 'kp')
    :seria_num: номер серии
    :translation_id: id переода/субтитров (Прим: 640 - Anilibria.TV)
    :token: Токен Kodik

    Возвращает хэш значение по которому можно получить путь до файла с результатом и ссылку на файл
    """
    check_ffmpeg() # Проверка на досутпность ffmpeg из модуля subprocess
    metadata = dict(metadata or {})
    hsh = build_cache_hash(id, id_type, translation_id, seria_num, quality, metadata)
    with _download_lock(hsh):
        return _fast_download_locked(
            id, id_type, seria_num, translation_id, quality, token, filename, metadata, hsh
        )

def _fast_download_locked(
    id: str,
    id_type: str,
    seria_num: int,
    translation_id: str,
    quality: str,
    token: str,
    filename: str,
    metadata: dict,
    hsh: str,
) -> tuple[str, str | None]:
    cache_root = os.path.join('tmp', hsh)
    os.makedirs('tmp', exist_ok=True)
    if os.path.isdir(cache_root):
        try:
            get_path(hsh)
        except FileNotFoundError:
            shutil.rmtree(cache_root)
        else:
            return (hsh, None)
    elif os.path.lexists(cache_root):
        os.unlink(cache_root)
    os.mkdir(cache_root, 0o700)

    try:
        request_id_type = id_type
        if request_id_type == 'sh':
            request_id_type = 'shikimori'
        elif request_id_type == 'kp':
            request_id_type = 'kinopoisk'
        link = get_download_link(
            id, request_id_type, seria_num, translation_id, token
        )[0]
        manifest_url = 'https:' + link + quality + '.mp4:hls:manifest.m3u8'
        manifest = get_url_data_with_retries(manifest_url)
        segments = get_segments(manifest, 'https:' + link)
        if not segments:
            raise ValueError('The HLS manifest does not contain video segments')
        workers = min(16, len(segments))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    download_segment,
                    segment_url,
                    os.path.join(cache_root, local_name + '.ts'),
                )
                for segment_url, local_name in segments
            ]
            for future in futures:
                future.result()

        temporary_stem = '.encode-' + uuid.uuid4().hex
        temporary_path = os.path.join(cache_root, temporary_stem + '.mp4')
        output_path = os.path.join(cache_root, 'result.mp4')
        combine_segments(
            cache_root + os.sep,
            segments_count=len(segments),
            name=temporary_stem,
            metadata=metadata,
        )
        if not os.path.isfile(temporary_path) or os.path.getsize(temporary_path) <= 0:
            raise RuntimeError('FFmpeg did not produce a non-empty MP4 file')
        os.replace(temporary_path, output_path)
        cache_fd = os.open(cache_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(cache_fd)
        finally:
            os.close(cache_fd)
        for artifact in os.listdir(cache_root):
            if artifact != 'result.mp4':
                artifact_path = os.path.join(cache_root, artifact)
                if os.path.isfile(artifact_path) or os.path.islink(artifact_path):
                    os.remove(artifact_path)
        return (hsh, link)
    except Exception:
        shutil.rmtree(cache_root, ignore_errors=True)
        raise

def get_segments(manifest: str, original_link: str) -> list[list[str]]:
    segments = []
    for line in manifest.splitlines():
        segment_reference = line.strip()
        if not segment_reference or segment_reference.startswith('#'):
            continue
        if any(character.isspace() for character in segment_reference):
            continue
        segment_url = urljoin(original_link, segment_reference)
        parsed = urlparse(segment_url)
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.netloc
            or not parsed.path.lower().endswith('.ts')
        ):
            continue
        segments.append([segment_url, str(len(segments))])
    return segments

def get_url_data_with_retries(url: str, attempts: int = 5) -> str:
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25 * (2 ** attempt))
    raise RuntimeError('unreachable')

def download_segment(link: str, path: str, attempts: int = 5):
    for attempt in range(attempts):
        try:
            res = requests.get(link, timeout=30)
            res.raise_for_status()
            with open(path, 'wb') as f:
                f.write(res.content)
            return
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25 * (2 ** attempt))

def combine_segments(directory: str, segments_count: int, name: str = 'result', metadata: dict | None = None, hwaccel: str | None = None):
    files = [
        filename
        for filename in os.listdir(directory)
        if filename.endswith('.ts') and filename[:-3].isdigit()
    ]
    if len(files) != segments_count:
        raise ValueError(f'expected {segments_count} HLS segments, found {len(files)}')
    r = ''
    for file in sorted(files, key=lambda x: int(x[:-3])):
        r += "file '"+file+"'\n"
    with open(directory+'files.txt', 'w') as f:
        f.write(r)
    command = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '1', '-i', directory+'files.txt',
        '-map', '0:v:0', '-map', '0:a:0',
        '-map_metadata', '0', '-map_chapters', '0',
        '-vf', 'settb=AVTB,setpts=PTS-STARTPTS,fps=24000/1001',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
        '-pix_fmt', 'yuv420p',
        '-r:v', '24000/1001', '-fps_mode:v', 'cfr',
        '-af', 'asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0',
        '-c:a', 'aac', '-b:a', '160k', '-ar', '48000',
        '-movflags', '+faststart+use_metadata_tags',
    ]
    for key, value in sorted((metadata or {}).items()):
        command.extend(['-metadata', f'{key}={value}'])
    command.append(directory+name+'.mp4')
    subprocess.run(command, check=True)

def get_path(hsh: str) -> str:
    result_path = os.path.join('tmp', hsh, 'result.mp4')
    try:
        result_stat = os.stat(result_path, follow_symlinks=False)
    except FileNotFoundError:
        raise FileNotFoundError(
            f'Result .mp4 file not found in "{hsh}" directory'
        ) from None
    if not stat.S_ISREG(result_stat.st_mode) or result_stat.st_size <= 0:
        raise FileNotFoundError(
            f'Result .mp4 file not found in "{hsh}" directory'
        )
    return result_path

def check_ffmpeg():
    """
    Raises ModuleNotFound error if ffmpeg isn't installed or can't be used by subprocess
    """
    try:
        subprocess.call('ffmpeg', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        raise ModuleNotFoundError('Ffmpeg is required to use fast download.')
    
def clear_tmp():
    """
    Clear inactive fast-download cache entries.
    """
    os.makedirs('tmp', exist_ok=True)
    os.makedirs(_LOCK_ROOT, exist_ok=True)
    for name in os.listdir('tmp'):
        lock_fd = os.open(
            os.path.join(_LOCK_ROOT, name + '.lock'),
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            cache_path = os.path.join('tmp', name)
            if os.path.isdir(cache_path) and not os.path.islink(cache_path):
                shutil.rmtree(cache_path)
            elif os.path.lexists(cache_path):
                os.unlink(cache_path)
        finally:
            os.close(lock_fd)
