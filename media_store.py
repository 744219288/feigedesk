"""Durable, local media cache. Source URLs never leave the private database.

Only the downloaded file is exposed to the local UI. No cookies, account headers,
environment proxy settings or browser credentials are used by the downloader.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit
import uuid

ALLOWED_DOMAINS = ('ecombdimg.com', 'douyinpic.com', 'byteimg.com')
LIMITS = {'image': 30 * 1024 * 1024, 'video': 100 * 1024 * 1024, 'file': 30 * 1024 * 1024}
ASSET_RE = re.compile(r'[0-9a-f]{64}\.(?:jpg|png|gif|webp|bmp|avif|mp4|webm|pdf)\Z')
TYPES = {'jpg':'image/jpeg', 'png':'image/png', 'gif':'image/gif',
         'webp':'image/webp', 'bmp':'image/bmp', 'avif':'image/avif',
         'mp4':'video/mp4', 'webm':'video/webm', 'pdf':'application/pdf'}


def validate_url(url):
    if not isinstance(url, str) or not url or len(url) > 16000 or any(ord(c) < 32 for c in url):
        raise ValueError('invalid_source')
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('invalid_source')
    if not (any(host == domain or host.endswith('.' + domain) for domain in ALLOWED_DOMAINS)
            or re.fullmatch(r'v\d+-pigeon\.jinritemai\.com',host)):
        raise ValueError('untrusted_source')
    return p


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=12, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Pin the verified DNS result rather than resolving again on connection.
        sock = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch_https(url, limit):
    for _ in range(5):
        p = validate_url(url)
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError('non_public_address')
        conn = _PinnedHTTPS(p.hostname, addresses[0])
        try:
            path = (p.path or '/') + ('?' + p.query if p.query else '')
            conn.request('GET', path, headers={'User-Agent':'FeigeDesk-Media/1.0', 'Accept-Encoding':'identity'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader('Location')
                if not target:
                    raise ValueError('invalid_redirect')
                url = urljoin(url, target)
                validate_url(url)
                continue
            if response.status != 200:
                raise ValueError('source_unavailable')
            length = response.getheader('Content-Length')
            if length and int(length) > limit:
                raise ValueError('too_large')
            chunks, count = [], 0
            deadline = time.monotonic() + 45
            while True:
                part = response.read(min(65536, limit + 1 - count))
                if not part:
                    break
                count += len(part)
                if count > limit:
                    raise ValueError('too_large')
                if time.monotonic() > deadline:
                    raise ValueError('download_timeout')
                chunks.append(part)
            if length and count != int(length):
                raise ValueError('incomplete_download')
            return b''.join(chunks), response.getheader('Content-Type', '').split(';')[0]
        finally:
            conn.close()
    raise ValueError('too_many_redirects')


def media_extension(data, kind):
    if kind == 'file' and data.startswith(b'%PDF-'): return 'pdf'
    if kind == 'image':
        if data.startswith(b'\xff\xd8\xff'): return 'jpg'
        if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'png'
        if data.startswith((b'GIF87a', b'GIF89a')): return 'gif'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'webp'
        if data.startswith(b'BM'): return 'bmp'
        if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'): return 'avif'
    if kind == 'video':
        if data[4:8] == b'ftyp' and data[8:12] not in (b'avif', b'avis'): return 'mp4'
        if data.startswith(b'\x1aE\xdf\xa3'): return 'webm'
    raise ValueError('unsupported_media')


class MediaStore:
    def __init__(self, data_dir, fetch=None, clock=time.time, max_attempts=4):
        self.root = Path(data_dir) / 'media'
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / 'media.sqlite', check_same_thread=False, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            mid TEXT, slot INTEGER, kind TEXT, source TEXT, status TEXT,
            attempts INTEGER DEFAULT 0, due REAL DEFAULT 0, asset TEXT,
            error TEXT DEFAULT '', PRIMARY KEY(mid,slot))''')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
        for name in ('analysis_status','analysis_detail'):
            if name not in columns:
                self.db.execute('ALTER TABLE jobs ADD COLUMN '+name+" TEXT DEFAULT ''")
        self.db.execute("UPDATE jobs SET analysis_status='' WHERE analysis_status='processing'")
        self.db.execute("UPDATE jobs SET status='pending',due=0 WHERE status='fetching'")
        self.db.commit()
        self.fetch, self.clock, self.max_attempts = fetch or fetch_https, clock, max(1, max_attempts)
        self.lock = threading.RLock()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.closed = False

    def ensure(self, mid, media):
        if not isinstance(mid, str) or not mid or not isinstance(media, list):
            return
        with self.lock:
            if self.closed: return
            for slot, item in enumerate(media[:20]):
                if not isinstance(item, dict) or item.get('kind') not in LIMITS: continue
                kind, source = item['kind'], item.get('url') or ''
                status, error = ('pending', '') if source else ('awaiting_source', '')
                if source:
                    try: validate_url(source)
                    except (ValueError, TypeError): status, error, source = 'failed', 'invalid_source', ''
                old = self.db.execute('SELECT * FROM jobs WHERE mid=? AND slot=?', (mid,slot)).fetchone()
                if old and old['kind'] == kind:
                    same_resource = source and old['source'] and urlsplit(source)._replace(query='',fragment='') == urlsplit(old['source'])._replace(query='',fragment='')
                    if old['status'] == 'ready' and old['asset'] and (self.root / old['asset']).is_file() and not (self.root / old['asset']).is_symlink() and (not source or same_resource):
                        continue
                    if old['source'] == source and old['status'] not in ('awaiting_source', 'ready'):
                        continue
                self.db.execute('INSERT OR REPLACE INTO jobs(mid,slot,kind,source,status,error) VALUES (?,?,?,?,?,?)',
                                (mid,slot,kind,source,status,error))
            self.db.commit()
        self.wake.set()

    def public_media(self, mid):
        with self.lock:
            ready = self.db.execute("SELECT asset FROM jobs WHERE mid=? AND status='ready'", (mid,)).fetchall()
            for row in ready:
                if not row['asset'] or not (self.root / row['asset']).is_file() or (self.root / row['asset']).is_symlink():
                    self.db.execute("UPDATE jobs SET status='pending',attempts=0,due=0,error='cache_missing',asset=NULL WHERE mid=? AND asset=?", (mid,row['asset']))
                    self.wake.set()
            self.db.commit()
            rows = self.db.execute('SELECT * FROM jobs WHERE mid=? ORDER BY slot', (mid,)).fetchall()
        return [dict(kind=r['kind'], url='/media/'+r['asset'] if r['status']=='ready' and r['asset'] else '',
                     status='pending' if r['status']=='fetching' else r['status'], error=r['error'],
                     analysis_status=r['analysis_status'],analysis_detail=r['analysis_detail']) for r in rows]

    def analysis_state(self, mid, asset, status, detail=''):
        with self.lock:
            if self.closed:return
            self.db.execute('UPDATE jobs SET analysis_status=?,analysis_detail=? WHERE mid=? AND asset=?',
                            (status,detail[:240],mid,asset))
            self.db.commit()

    def process_one(self):
        with self.lock:
            if self.closed or self.stop_event.is_set(): return False
            row = self.db.execute("SELECT * FROM jobs WHERE status='pending' AND due<=? ORDER BY due,mid,slot LIMIT 1", (self.clock(),)).fetchone()
            if not row: return False
            self.db.execute("UPDATE jobs SET status='fetching' WHERE mid=? AND slot=?", (row['mid'],row['slot']))
            self.db.commit()
        temp = None
        try:
            data, _mime = self.fetch(row['source'], LIMITS[row['kind']])
            if not isinstance(data, bytes) or len(data) > LIMITS[row['kind']]: raise ValueError('too_large')
            ext = media_extension(data, row['kind'])
            asset = hashlib.sha256(data).hexdigest() + '.' + ext
            final = self.root / asset
            if not final.is_file() or final.is_symlink():
                temp = self.root / (uuid.uuid4().hex + '.part')
                with temp.open('xb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, final)
                temp = None
            with self.lock:
                self.db.execute("UPDATE jobs SET status='ready',asset=?,error='' WHERE mid=? AND slot=? AND source=? AND status='fetching'",
                                (asset,row['mid'],row['slot'],row['source']))
                self.db.commit()
        except Exception as exc:
            code = str(exc) if isinstance(exc, ValueError) and str(exc) in {'too_large','unsupported_media','source_unavailable','non_public_address','invalid_redirect','untrusted_source','invalid_source','download_timeout','too_many_redirects','incomplete_download'} else 'download_failed'
            attempts = row['attempts'] + 1
            status = 'failed' if attempts >= self.max_attempts or code in ('too_large','unsupported_media','non_public_address','untrusted_source','invalid_source') else 'pending'
            with self.lock:
                self.db.execute("UPDATE jobs SET status=?,attempts=?,due=?,error=? WHERE mid=? AND slot=? AND source=? AND status='fetching'",
                                (status,attempts,self.clock()+min(300,2**attempts),code,row['mid'],row['slot'],row['source']))
                self.db.commit()
        finally:
            if temp:
                try: temp.unlink()
                except OSError: pass
        return True

    def start(self):
        with self.lock:
            if self.closed or self.thread and self.thread.is_alive(): return
            self.thread = threading.Thread(target=self._run, name='media-cache', daemon=True)
            self.thread.start()

    def _run(self):
        try:
            while not self.stop_event.is_set():
                if not self.process_one():
                    self.wake.wait(1); self.wake.clear()
        finally:
            with self.lock:
                if self.closed: self.db.close()

    def read_asset(self, name):
        if not isinstance(name, str) or not ASSET_RE.fullmatch(name): return None
        path = self.root / name
        if not path.is_file() or path.is_symlink(): return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != name.split('.')[0]:
            with self.lock:
                self.db.execute("UPDATE jobs SET status='pending',asset=NULL,attempts=0,due=0,error='cache_corrupt' WHERE asset=?", (name,))
                self.db.commit()
            # A corrupt content-addressed file must not block the atomic replacement.
            path.unlink(missing_ok=True)
            self.wake.set()
            return None
        return data, TYPES[name.rsplit('.',1)[1]]

    def close(self):
        with self.lock:
            if self.closed: return
            self.closed = True
            self.stop_event.set(); self.wake.set()
            thread = self.thread
        if thread: thread.join(timeout=2)
        else:
            with self.lock: self.db.close()
