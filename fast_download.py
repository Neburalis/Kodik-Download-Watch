from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
from hashlib import md5
from getters import get_download_link
import json
import requests
import os
import subprocess
import stat
import threading
import time
from urllib.parse import urljoin, urlparse
import uuid

_download_locks = {}
_download_locks_guard = threading.Lock()
_NORMALIZATION_PROFILE = "h264-cfr-24000-1001-crf18-aac160-v1"
_LOCK_ROOT = '.tmp-locks'


def _open_real_directory(path: str, create: bool = False) -> int:
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(entry_stat.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    directory_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        for child in os.listdir(directory_fd):
            _remove_tree_at(directory_fd, child)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _clear_directory_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        _remove_tree_at(directory_fd, name)


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
        lock_root_fd = _open_real_directory(_LOCK_ROOT, create=True)
        try:
            file_lock_fd = os.open(
                hsh + '.lock',
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=lock_root_fd,
            )
        finally:
            os.close(lock_root_fd)
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

def fast_download(id: str, id_type: str, seria_num: int, translation_id: str, quality: str, token: str, filename: str = 'result', metadata: dict | None = None) -> tuple[str, tuple | None]:
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


def fast_download_open(id: str, id_type: str, seria_num: int, translation_id: str, quality: str, token: str, filename: str = 'result', metadata: dict | None = None):
    check_ffmpeg()
    metadata = dict(metadata or {})
    hsh = build_cache_hash(id, id_type, translation_id, seria_num, quality, metadata)
    with _download_lock(hsh):
        download_hash, link_data = _fast_download_locked(
            id, id_type, seria_num, translation_id, quality, token, filename, metadata, hsh
        )
        return download_hash, link_data, _open_cache_file(download_hash)

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
) -> tuple[str, tuple | None]:
    tmp_fd = _open_real_directory('tmp', create=True)
    cache_fd = None
    try:
        try:
            cache_stat = os.stat(hsh, dir_fd=tmp_fd, follow_symlinks=False)
        except FileNotFoundError:
            cache_stat = None
        if cache_stat is not None and not stat.S_ISDIR(cache_stat.st_mode):
            raise OSError('Fast-download cache entry is not a real directory')
        if cache_stat is not None:
            try:
                get_path(hsh)
            except FileNotFoundError:
                _remove_tree_at(tmp_fd, hsh)
            else:
                return (hsh, None)
        os.mkdir(hsh, 0o700, dir_fd=tmp_fd)
        os.fsync(tmp_fd)
        cache_fd = os.open(
            hsh,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=tmp_fd,
        )

        request_id_type = id_type
        if request_id_type == 'sh':
            request_id_type = 'shikimori'
        elif request_id_type == 'kp':
            request_id_type = 'kinopoisk'
        link_data = get_download_link(
            id, request_id_type, seria_num, translation_id, token
        )
        link = link_data[0]
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
                    local_name + '.ts',
                    directory_fd=cache_fd,
                )
                for segment_url, local_name in segments
            ]
            for future in futures:
                future.result()

        temporary_stem = '.encode-' + uuid.uuid4().hex
        combine_segments(
            '',
            segments_count=len(segments),
            name=temporary_stem,
            metadata=metadata,
            directory_fd=cache_fd,
        )
        temporary_fd = os.open(
            temporary_stem + '.mp4',
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=cache_fd,
        )
        try:
            temporary_stat = os.fstat(temporary_fd)
            if not stat.S_ISREG(temporary_stat.st_mode) or temporary_stat.st_size <= 0:
                raise RuntimeError('FFmpeg did not produce a non-empty MP4 file')
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        current_stat = os.stat(hsh, dir_fd=tmp_fd, follow_symlinks=False)
        opened_stat = os.fstat(cache_fd)
        if (
            not stat.S_ISDIR(current_stat.st_mode)
            or (current_stat.st_dev, current_stat.st_ino)
            != (opened_stat.st_dev, opened_stat.st_ino)
        ):
            raise OSError('Fast-download cache directory changed during download')
        os.replace(
            temporary_stem + '.mp4',
            'result.mp4',
            src_dir_fd=cache_fd,
            dst_dir_fd=cache_fd,
        )
        os.fsync(cache_fd)
        for artifact in os.listdir(cache_fd):
            if artifact != 'result.mp4':
                artifact_stat = os.stat(
                    artifact, dir_fd=cache_fd, follow_symlinks=False
                )
                if stat.S_ISREG(artifact_stat.st_mode) or stat.S_ISLNK(artifact_stat.st_mode):
                    os.unlink(artifact, dir_fd=cache_fd)
        return (hsh, link_data)
    except Exception:
        if cache_fd is not None:
            _clear_directory_fd(cache_fd)
            try:
                current_stat = os.stat(hsh, dir_fd=tmp_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                opened_stat = os.fstat(cache_fd)
                if (
                    stat.S_ISDIR(current_stat.st_mode)
                    and (current_stat.st_dev, current_stat.st_ino)
                    == (opened_stat.st_dev, opened_stat.st_ino)
                ):
                    os.rmdir(hsh, dir_fd=tmp_fd)
        raise
    finally:
        if cache_fd is not None:
            os.close(cache_fd)
        os.close(tmp_fd)

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

def download_segment(link: str, path: str, attempts: int = 5, directory_fd: int | None = None):
    for attempt in range(attempts):
        try:
            res = requests.get(link, timeout=30)
            res.raise_for_status()
            open_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
            file_fd = os.open(path, open_flags, 0o600, dir_fd=directory_fd)
            with os.fdopen(file_fd, 'wb') as f:
                f.write(res.content)
            return
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25 * (2 ** attempt))

def combine_segments(directory: str, segments_count: int, name: str = 'result', metadata: dict | None = None, hwaccel: str | None = None, directory_fd: int | None = None):
    directory_ref = directory_fd if directory_fd is not None else directory
    files = [
        filename
        for filename in os.listdir(directory_ref)
        if filename.endswith('.ts') and filename[:-3].isdigit()
    ]
    if len(files) != segments_count:
        raise ValueError(f'expected {segments_count} HLS segments, found {len(files)}')
    r = ''
    for file in sorted(files, key=lambda x: int(x[:-3])):
        r += "file '"+file+"'\n"
    if directory_fd is None:
        files_stream = open(directory+'files.txt', 'w')
        concat_path = directory+'files.txt'
        output_path = directory+name+'.mp4'
    else:
        files_fd = os.open(
            'files.txt',
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        files_stream = os.fdopen(files_fd, 'w')
        descriptor_path = f'/proc/self/fd/{directory_fd}/'
        concat_path = descriptor_path + 'files.txt'
        output_path = descriptor_path + name + '.mp4'
    with files_stream as f:
        f.write(r)
    command = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '1', '-i', concat_path,
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
    command.append(output_path)
    run_options = {'check': True}
    if directory_fd is not None:
        run_options['pass_fds'] = (directory_fd,)
    subprocess.run(command, **run_options)

def _open_cache_file(hsh: str):
    if os.path.basename(hsh) != hsh or hsh in {'.', '..'}:
        raise OSError('Invalid fast-download cache key')
    cache_root_fd = _open_real_directory('tmp')
    try:
        cache_fd = os.open(
            hsh,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=cache_root_fd,
        )
    finally:
        os.close(cache_root_fd)
    try:
        result_fd = os.open(
            'result.mp4',
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=cache_fd,
        )
    except FileNotFoundError:
        raise FileNotFoundError(
            f'Result .mp4 file not found in "{hsh}" directory'
        ) from None
    finally:
        os.close(cache_fd)
    result_stat = os.fstat(result_fd)
    if not stat.S_ISREG(result_stat.st_mode) or result_stat.st_size <= 0:
        os.close(result_fd)
        raise FileNotFoundError(
            f'Result .mp4 file not found in "{hsh}" directory'
        )
    return os.fdopen(result_fd, 'rb')


def get_path(hsh: str) -> str:
    result_path = os.path.join('tmp', hsh, 'result.mp4')
    with _open_cache_file(hsh):
        pass
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
    cache_fd = _open_real_directory('tmp', create=True)
    lock_root_fd = _open_real_directory(_LOCK_ROOT, create=True)
    try:
        for name in os.listdir(cache_fd):
            lock_fd = os.open(
                name + '.lock',
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=lock_root_fd,
            )
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    _remove_tree_at(cache_fd, name)
                except FileNotFoundError:
                    pass
            finally:
                os.close(lock_fd)
    finally:
        os.close(lock_root_fd)
        os.close(cache_fd)
