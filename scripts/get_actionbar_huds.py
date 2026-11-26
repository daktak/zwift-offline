"""Download in-game HUD and action-bar icons from the official CDN.

These icons are referenced by the Zwift game client (Zwift.xapk) but are not
covered by scripts/get_zca_images.py (which targets the Companion app) or by
get_videos.py. Run this script while cdn.zwift.com and
d1x0fcturdg7f6.cloudfront.net still resolve to the official servers, then
re-run it after a client update.

Files are saved below storage/cdn/static/, mirroring the URL path after the
host. The cloudfront phoneicons are flattened into phoneicons/ so a single
lookup path works for both sources.
"""
import argparse
import concurrent.futures
import json
import logging
import os
from pathlib import Path, PurePosixPath
import sys
import threading
import time
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'storage' / 'cdn' / 'static'
STATUS_PATH = ROOT / 'logs' / 'actionbar-hud-download-status.json'
LOG = logging.getLogger('actionbar-hud-download')
BLOCK = 1024 * 1024

# (host_key, relative_path) pairs. host_key selects the base URL.
CDN_BASES = {
    'cdn': 'https://cdn.zwift.com/',
    'cloudfront': 'https://d1x0fcturdg7f6.cloudfront.net/',
}

# Hardcoded in Zwift.xapk (libzwiftjni.so string pool).
PHONEICONS_CDN = [
    'phoneicons/coffee_stop.png',
    'phoneicons/Icon_Boost_Disabled.png',
    'phoneicons/Icon_Boost.png',
    'phoneicons/Icon_Charge_Boost_Disabled.png',
    'phoneicons/Icon_Charge_Boost.png',
    'phoneicons/Icon_Fence.png',
    'phoneicons/Icon_HUD_Off.png',
    'phoneicons/Icon_HUD_On.png',
    'phoneicons/Icon_Power_Graph.png',
    'phoneicons/Icon_Trash_Aero.png',
    'phoneicons/Icon_Trash_Anvil.png',
    'phoneicons/Icon_Trash_Burrito.png',
    'phoneicons/Icon_Trash_Feather.png',
    'phoneicons/Icon_Trash_Invisibility.png',
    'phoneicons/Icon_Trash_Steamroller.png',
    'phoneicons/Icon_Trash_Van.png',
]

PHONEICONS_CLOUDFRONT = [
    'phoneicons/Icon_Bell.png',
    'phoneicons/Icon_Elbow.png',
    'phoneicons/Icon_RideOn.png',
    'phoneicons/Icon_Turn_Arrow_Left.png',
    'phoneicons/Icon_Turn_Arrow_Right.png',
    'phoneicons/Icon_Turn_Arrow_Up.png',
    'phoneicons/Icon_Wave.png',
    'phoneicons/Icon_Workout_Easier.png',
    'phoneicons/Icon_Workout_FF.png',
    'phoneicons/Icon_Workout_Harder.png',
    'phoneicons/Icon_Workout_Pause.png',
    'phoneicons/Icon_Workout_Play.png',
]


class UnavailableError(Exception):
    """The live CDN does not have this image (HTTP 403 or 404)."""


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def local_path(relpath):
    p = PurePosixPath(relpath)
    if not relpath or p.is_absolute() or '..' in p.parts or '\\' in relpath or ':' in relpath:
        raise ValueError('Unsafe CDN path: ' + relpath)
    result = DEST.joinpath(*p.parts)
    if not result.resolve().is_relative_to(DEST.resolve()):
        raise ValueError('Path outside storage/cdn/static: ' + relpath)
    return result


def url_paths(actionbar_list=None):
    """Collect (host_key, relative_path) pairs to download."""
    items = []
    for relpath in PHONEICONS_CDN:
        items.append(('cdn', relpath))
    for relpath in PHONEICONS_CLOUDFRONT:
        items.append(('cloudfront', relpath))
    if actionbar_list:
        with open(actionbar_list, encoding='utf-8') as stream:
            for line in stream:
                name = line.strip()
                if not name or name.startswith('#'):
                    continue
                items.append(('cdn', 'static/zc/ACTION_BAR/' + name))
    result = []
    seen = set()
    for host_key, relpath in items:
        local_path(relpath)
        key = (host_key, relpath)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


class Downloader:
    def __init__(self, args, items):
        self.args = args
        self.items = items
        self.local = threading.local()
        self.status = dict(started=time.strftime('%Y-%m-%dT%H:%M:%S%z'), state='starting',
                           files=len(items), existing=0, downloaded=0, skipped=0, failed=0,
                           failed_files=[], skipped_files=[])
        self.last_progress = 0

    def progress(self, force=False):
        if not force and time.monotonic() - self.last_progress < 10:
            return
        self.last_progress = time.monotonic()
        self.status['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        atomic_json(STATUS_PATH, self.status)
        LOG.info('%s | files %d/%d | existing %d | downloaded %d | skipped %d | failed %d',
                 self.status['state'], self.status['existing'] + self.status['downloaded'],
                 self.status['files'], self.status['existing'], self.status['downloaded'],
                 self.status['skipped'], self.status['failed'])

    def session(self, host_key):
        cache = getattr(self.local, 'sessions', None)
        if cache is None:
            cache = {}
            self.local.sessions = cache
        session = cache.get(host_key)
        if session is None:
            session = requests.Session()
            session.headers.update({'User-Agent': 'zoffline-actionbar-huds/1.0',
                                    'Accept-Encoding': 'identity'})
            cache[host_key] = session
        return session

    def transfer(self, host_key, relpath):
        target = local_path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + '.part')
        base = CDN_BASES[host_key]
        with self.session(host_key).get(base + quote(relpath, safe='/'),
                                       stream=True, timeout=(15, 60)) as response:
            if response.status_code in (403, 404):
                raise UnavailableError(response.status_code)
            response.raise_for_status()
            with part.open('wb') as stream:
                for block in response.iter_content(BLOCK):
                    if block:
                        stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
        if part.stat().st_size == 0:
            part.unlink()
            raise ValueError('Empty response')
        os.replace(part, target)

    def download(self, host_key, relpath):
        for attempt in range(self.args.retries):
            try:
                self.transfer(host_key, relpath)
                return 'ok'
            except UnavailableError:
                return 'skipped'
            except (requests.RequestException, OSError, ValueError) as exc:
                LOG.error('Attempt %d/%d %s/%s: %s', attempt + 1, self.args.retries,
                          host_key, relpath, exc)
                if isinstance(exc, OSError) and not isinstance(exc, requests.RequestException):
                    raise
                if attempt + 1 < self.args.retries:
                    time.sleep(min(30, 2 ** (attempt + 1)))
        return 'failed'

    def run(self):
        LOG.info('START %s files=%d destination=%s', self.status['started'],
                 self.status['files'], DEST)
        DEST.mkdir(parents=True, exist_ok=True)
        self.status['state'] = 'checking'
        pending = []
        for host_key, relpath in self.items:
            target = local_path(relpath)
            if not self.args.force and target.is_file() and target.stat().st_size > 0:
                self.status['existing'] += 1
            else:
                pending.append((host_key, relpath))
        self.progress(True)

        self.status['state'] = 'downloading'
        if self.args.limit:
            pending = pending[:self.args.limit]
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            futures = {pool.submit(self.download, hk, rp): (hk, rp) for hk, rp in pending}
            for future in concurrent.futures.as_completed(futures):
                host_key, relpath = futures[future]
                try:
                    result = future.result()
                except OSError:
                    raise
                if result == 'ok':
                    self.status['downloaded'] += 1
                    LOG.info('OK %s/%s', host_key, relpath)
                else:
                    self.status[result] += 1
                    self.status[result + '_files'].append('%s/%s' % (host_key, relpath))
                    LOG.info('%s %s/%s', result.upper(), host_key, relpath)
                self.progress()

        self.status['state'] = 'complete'
        self.progress(True)
        LOG.info('END existing=%d downloaded=%d skipped=%d failed=%d',
                 self.status['existing'], self.status['downloaded'],
                 self.status['skipped'], self.status['failed'])
        return 1 if self.status['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='List URLs without downloading')
    parser.add_argument('--verify-only', action='store_true',
                        help='Report missing/empty files without downloading')
    parser.add_argument('--force', action='store_true', help='Re-download existing files')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--limit', type=int, help='Download only the first N pending files')
    parser.add_argument('--actionbar-list', metavar='PATH',
                        help='Text file with one ACTION_BAR filename per line')
    args = parser.parse_args()
    if args.workers < 1 or args.retries < 1 or (args.limit is not None and args.limit < 1):
        parser.error('Invalid worker/retry/file count')

    items = url_paths(args.actionbar_list)
    if args.dry_run:
        for host_key, relpath in items:
            print(CDN_BASES[host_key] + relpath)
        print('%d images' % len(items), file=sys.stderr)
        return 0

    (ROOT / 'logs').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(ROOT / 'logs' / 'actionbar-hud-download.log',
                                                      encoding='utf-8'),
                                  logging.StreamHandler(sys.stdout)])

    if args.verify_only:
        missing = []
        for _host_key, relpath in items:
            target = local_path(relpath)
            if not target.is_file() or target.stat().st_size == 0:
                missing.append(relpath)
        atomic_json(STATUS_PATH, dict(state='verified', files=len(items),
                                      present=len(items) - len(missing), missing=missing))
        print('present %d/%d' % (len(items) - len(missing), len(items)))
        for path in missing[:20]:
            print('MISSING ' + path)
        return 1 if missing else 0

    return Downloader(args, items).run()


if __name__ == '__main__':
    sys.exit(main())
