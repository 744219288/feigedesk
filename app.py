"""Desktop entrypoint and loopback-only API."""
import argparse
import json
import secrets
import sys
import threading
import re
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from core import Service

ROOT = Path(__file__).resolve().parent
HOME = Path(sys.executable).parent if getattr(sys, 'frozen', False) else ROOT


def make_server(service, port=0):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, payload, html=False, content_type=None):
            data = payload if html else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type or ('text/html; charset=utf-8' if html else 'application/json; charset=utf-8'))
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {'error': '不允许的访问来源'})
            p = urlparse(self.path)
            try:
                if p.path.startswith('/media/'):
                    if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                        return self.respond(403, {'error':'不允许的访问来源'})
                    asset=service.media_store.read_asset(p.path[len('/media/'):])
                    if not asset:
                        return self.respond(404, {'error':'附件尚未保存'})
                    data,mime=asset
                    full=len(data); start=0; end=full-1; status=200
                    range_header=self.headers.get('Range')
                    if range_header:
                        match=re.fullmatch(r'bytes=(\d+)-(\d*)',range_header)
                        if not match or int(match[1])>=full:
                            self.send_response(416); self.send_header('Content-Range',f'bytes */{full}'); self.end_headers(); return
                        start=int(match[1]); end=min(int(match[2]) if match[2] else end,end)
                        if end<start:
                            return self.respond(400,{'error':'无效附件范围'})
                        status=206
                    self.send_response(status)
                    self.send_header('Content-Type',mime)
                    if mime == 'application/pdf':
                        self.send_header('Content-Disposition','attachment; filename="customer-document.pdf"')
                    self.send_header('Content-Length',str(end-start+1))
                    self.send_header('Accept-Ranges','bytes')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Cache-Control','private, max-age=31536000, immutable')
                    if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{full}')
                    self.end_headers(); self.wfile.write(data[start:end+1]); return
                if p.path == '/':
                    return self.respond(200, (ROOT/'static/index.html').read_bytes(), True)
                if p.path == '/static/official.js':
                    return self.respond(200, (ROOT/'static/official.js').read_bytes(), True, 'text/javascript; charset=utf-8')
                if p.path == '/static/reception.js':
                    return self.respond(200, (ROOT/'static/reception.js').read_bytes(), True, 'text/javascript; charset=utf-8')
                if p.path == '/static/chains.js':
                    return self.respond(200,(ROOT/'static/chains.js').read_bytes(),True,'text/javascript; charset=utf-8')
                if p.path == '/api/outbound_jobs':
                    cid=parse_qs(p.query).get('conversation_id',[''])[0]
                    with service.lock:
                        service._require(cid)
                        jobs=[dict(r) for r in service.db.execute('SELECT * FROM outbound_jobs WHERE cid=? ORDER BY created_at DESC LIMIT 30',(cid,))]
                    return self.respond(200,{'items':jobs})
                if p.path == '/api/state':
                    return self.respond(200, {**service.state(), 'token': token})
                if p.path == '/api/diagnostics':
                    return self.respond(200, service.transport.diagnostics())
                if p.path == '/api/conversation':
                    return self.respond(200, service.conversation(parse_qs(p.query).get('id',[''])[0]))
                return self.respond(404, {'error': '不存在的接口'})
            except ValueError as exc:
                self.respond(400, {'error': str(exc)})
            except Exception:
                self.respond(500, {'error': '读取失败，请重启程序或检查本地数据'})

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if self.path == '/api/activate':
                if not self.valid_host() or self.headers.get('Origin') != origin or not secrets.compare_digest(self.headers.get('X-Feige-Activation',''), self.server.activation_token):
                    return self.respond(403, {'error':'窗口唤起请求未通过校验'})
                try:
                    activated=bool(self.server.activate_desktop and self.server.activate_desktop())
                    return self.respond(200 if activated else 409, {'activated':activated})
                except Exception:
                    return self.respond(409, {'activated':False})
            if not self.valid_host() or self.headers.get('Origin', origin) != origin or not secrets.compare_digest(self.headers.get('X-Desk-Token', ''), token):
                return self.respond(403, {'error': '请求来源校验失败，请刷新窗口'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > (23*1024*1024 if self.path=='/api/assets/upload' else 150000):
                    raise ValueError('请求过大')
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('请求格式无效')
                cid = str(body.get('conversation_id',''))
                routes = {
                    '/api/connect': service.connect, '/api/disconnect': service.disconnect,
                    '/api/demo': service.demo, '/api/live': service.live,
                    '/api/settings': lambda: service.update_settings(body),
                    '/api/knowledge': lambda: service.knowledge(body),
                    '/api/read': lambda: service.mark_read(body),
                    '/api/lesson': lambda: service.lesson(body),
                    '/api/official': lambda: service.official_query(body),
                    '/api/products/sync': lambda: service.sync_products(body),
                    '/api/assets/upload': lambda: service.outbound_assets.upload(body),
                    '/api/send_asset': lambda: service.manual_outbound(body),
                    '/api/generate': lambda: service.generate(cid),
                    '/api/send': lambda: service.send(cid,body.get('text',''),body.get('source_message_id','')),
                    '/api/human': lambda: service.human(cid,body.get('human',True)),
                    '/api/test_model': lambda: service.test_model(body.get('text','')),
                    '/api/demo_message': lambda: service.demo_message(body.get('text','')),
                }
                fn = routes.get(self.path)
                if not fn:
                    return self.respond(404, {'error': '不存在的接口'})
                self.respond(200, fn())
            except (ValueError, TypeError) as exc:
                self.respond(400, {'error': str(exc)[:300]})
            except Exception:
                self.respond(500, {'error': '操作失败，请检查浏览器连接和本地配置'})
    server=ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.activation_token=secrets.token_urlsafe(32)
    server.activate_desktop=None
    return server


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--media-worker':
        from media_processing import worker
        worker(sys.argv[2], sys.argv[3])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--data-dir', type=Path, default=HOME/'data')
    args = parser.parse_args()
    # Two desktop instances must not compete over the same shop profile/database.
    import msvcrt
    args.data_dir.mkdir(parents=True, exist_ok=True)
    instance_file = open(args.data_dir/'instance.lock', 'a+b')
    if instance_file.seek(0, 2) == 0:
        instance_file.write(b'0')
        instance_file.flush()
    instance_file.seek(0)
    try:
        msvcrt.locking(instance_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        instance_file.close()
        from desktop_instance import wake_existing
        if wake_existing(args.data_dir):return
        import webview
        webview.create_window('飞鸽工作台暂未响应', html='<html lang="zh"><body style="font-family:sans-serif;padding:30px"><h2>工作台正在启动或暂未响应</h2><p>请稍后再次打开。没有启动第二个接待服务，已有聊天数据不会被覆盖。</p></body></html>', width=480, height=240)
        webview.start()
        return
    from feige_transport import FeigeTransport
    service = Service(args.data_dir, FeigeTransport)
    server = make_server(service, args.port)
    url = f'http://127.0.0.1:{server.server_port}'
    if args.browser:
        print(url, flush=True)
        webbrowser.open(url)
        try:
            server.serve_forever()
        finally:
            service.close()
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            import webview
            from desktop_instance import publish_instance,clear_instance,show_desktop,show_when_loaded
            window=webview.create_window('飞鸽工作台 · 独立版', url, width=1320, height=900, min_size=(1000,700))
            def notify_inbound():
                if service.settings.get('notify_sound',True):
                    import winsound
                    winsound.PlaySound('SystemNotification',winsound.SND_ALIAS|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
            service.on_inbound=notify_inbound
            server.activate_desktop=lambda:show_desktop(window)
            runtime_path=publish_instance(args.data_dir,server)
            webview.start(show_when_loaded,(window,))
        finally:
            if 'runtime_path' in locals():clear_instance(runtime_path,server.activation_token)
            server.activate_desktop=None
            service.close()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
