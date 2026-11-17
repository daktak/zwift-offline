"""Download images requested by the Zwift Companion app from the official CDN.

The Companion app uses absolute https://cdn.zwift.com/static/... URLs. Run this
script on the zoffline host while cdn.zwift.com is NOT redirected to zoffline
(remove the hosts entry / disable fake DNS first), then re-run it whenever
data/game_info.txt or the Companion app is updated.

Files are saved below storage/cdn/static/, mirroring the URL path after /static/.
They are served by both the HTTP CDN handler and the HTTPS Flask server.
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
GAME_INFO = ROOT / 'data' / 'game_info.txt'
BASE_URL = 'https://cdn.zwift.com/static/'
STATUS_PATH = ROOT / 'logs' / 'zca-image-download-status.json'
LOG = logging.getLogger('zca-image-download')
BLOCK = 1024 * 1024


class UnavailableError(Exception):
    """The live CDN does not have this image (HTTP 403 or 404)."""


# Hardcoded in zca.apk (DEX string pool). The pace-partner images currently return
# 403 from the live CDN; they are attempted anyway and skipped on failure.
APK_IMAGES = [
    'zc/ROUTES/WATOPIA_Accelerate_to_Elevate.png',
    'events_headers/cycling/special/yumezi_race_ride_2021may.jpg',
    'events_headers/cycling/special/Zwift_labs_2024_Eh.jpg',
    'events_headers/cycling/workouts/wat_wko_7.jpg',
    'web/pace-partners/bowie.png',
    'web/pace-partners/coco.png',
    'web/pace-partners/diesel.png',
]

# World-header images use lowercase world names. Verified against the live CDN.
WORLDS = [
    'watopia', 'makuri', 'london', 'newyork', 'richmond', 'paris', 'innsbruck',
    'yorkshire', 'scotland', 'france', 'critcity', 'bologna', 'gravelmountain',
]


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


def url_paths():
    """Collect and validate all image paths that need to be served from the CDN."""
    with GAME_INFO.open(encoding='utf-8-sig') as stream:
        data = json.load(stream)

    paths = set()

    def walk(value):
        if isinstance(value, dict):
            for _key, item in value.items():
                if isinstance(item, str) and item.startswith(BASE_URL):
                    path = item[len(BASE_URL):].split('?', 1)[0]
                    if path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
                        paths.add(path)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    paths.update(APK_IMAGES)

    for world in WORLDS:
        paths.add('world_headers/ActivityFeed_%s.png' % world)
        if world != 'gravelmountain':
            paths.add('world_headers/ZwiftingNow_%s.jpg' % world)

    plan_images = set()
    for plan in data.get('trainingPlans', []):
        image_url = plan.get('imageUrl', '')
        if image_url.startswith(BASE_URL):
            plan_images.add(image_url[len(BASE_URL):])
    for image in plan_images:
        paths.add('training_plans/' + Path(image).name)

    result = sorted(paths)
    for path in result:
        local_path(path)
    return result


class Downloader:
    def __init__(self, args, paths):
        self.args = args
        self.paths = paths
        self.local = threading.local()
        self.status = dict(started=time.strftime('%Y-%m-%dT%H:%M:%S%z'), state='starting',
                           files=len(paths), existing=0, downloaded=0, skipped=0, failed=0,
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

    def session(self):
        session = getattr(self.local, 'session', None)
        if session is None:
            session = requests.Session()
            session.headers.update({'User-Agent': 'zoffline-zca-images/1.0',
                                    'Accept-Encoding': 'identity'})
            self.local.session = session
        return session

    def transfer(self, relpath):
        target = local_path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + '.part')
        with self.session().get(BASE_URL + quote(relpath, safe='/'),
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

    def download(self, relpath):
        for attempt in range(self.args.retries):
            try:
                self.transfer(relpath)
                return 'ok'
            except UnavailableError:
                return 'skipped'
            except (requests.RequestException, OSError, ValueError) as exc:
                LOG.error('Attempt %d/%d %s: %s', attempt + 1, self.args.retries, relpath, exc)
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
        for relpath in self.paths:
            target = local_path(relpath)
            if not self.args.force and target.is_file() and target.stat().st_size > 0:
                self.status['existing'] += 1
            else:
                pending.append(relpath)
        self.progress(True)

        self.status['state'] = 'downloading'
        pending = pending[:self.args.limit] if self.args.limit else pending
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            futures = {pool.submit(self.download, path): path for path in pending}
            for future in concurrent.futures.as_completed(futures):
                relpath = futures[future]
                try:
                    result = future.result()
                except OSError:
                    raise
                if result == 'ok':
                    self.status['downloaded'] += 1
                    LOG.info('OK %s', relpath)
                else:
                    self.status[result] += 1
                    self.status[result + '_files'].append(relpath)
                    LOG.info('%s %s', result.upper(), relpath)
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
    args = parser.parse_args()
    if args.workers < 1 or args.retries < 1 or (args.limit is not None and args.limit < 1):
        parser.error('Invalid worker/retry/file count')

    paths = url_paths()
    if args.dry_run:
        for path in paths:
            print(BASE_URL + path)
        print('%d images' % len(paths), file=sys.stderr)
        return 0

    (ROOT / 'logs').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(ROOT / 'logs' / 'zca-image-download.log',
                                                      encoding='utf-8'),
                                  logging.StreamHandler(sys.stdout)])

    if args.verify_only:
        missing = [p for p in paths
                   if not local_path(p).is_file() or local_path(p).stat().st_size == 0]
        atomic_json(STATUS_PATH, dict(state='verified', files=len(paths),
                                      present=len(paths) - len(missing), missing=missing))
        print('present %d/%d' % (len(paths) - len(missing), len(paths)))
        for path in missing[:20]:
            print('MISSING ' + path)
        return 1 if missing else 0

    return Downloader(args, paths).run()


if __name__ == '__main__':
    sys.exit(main())
