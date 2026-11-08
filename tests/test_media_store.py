import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from media_store import MediaStore, validate_url, fetch_https, media_extension

PNG = b'\x89PNG\r\n\x1a\n' + b'test-content'
URL = 'https://p9-pigeon-sign.ecombdimg.com/image.png?signature=old'

class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=100
        self.calls=[]
        self.store=self.new()
    def new(self, fetch=None):
        return MediaStore(self.temp.name,fetch=fetch or self.fetch,clock=lambda:self.now,max_attempts=2)
    def fetch(self,url,limit):
        self.calls.append((url,limit))
        return PNG,'image/png'
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def enqueue(self, mid='m', url=URL, kind='image'):
        self.store.ensure(mid,[dict(kind=kind,url=url)])
    def test_download_atomic_local_url_and_duplicate_content(self):
        self.enqueue(); self.enqueue('other')
        self.assertTrue(self.store.process_one()); self.assertTrue(self.store.process_one())
        entry=self.store.public_media('m')[0]
        self.assertEqual(entry['status'],'ready')
        self.assertNotIn('signature',str(entry))
        self.assertEqual(entry['url'],self.store.public_media('other')[0]['url'])
        self.assertEqual(len(list(self.store.root.iterdir())),1)
        self.assertEqual(self.store.read_asset(entry['url'].split('/')[-1]),(PNG,'image/png'))
        self.enqueue(); self.assertFalse(self.store.process_one())
    def test_restart_recovers_inflight_task(self):
        self.enqueue()
        self.store.db.execute("UPDATE jobs SET status='fetching'"); self.store.db.commit()
        self.store.close(); self.store=self.new()
        self.assertEqual(self.store.public_media('m')[0]['status'],'pending')
        self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['status'],'ready')
    def test_missing_source_becomes_pending_and_ready(self):
        self.enqueue(url='')
        self.assertEqual(self.store.public_media('m')[0]['status'],'awaiting_source')
        self.assertFalse(self.store.process_one())
        self.enqueue(); self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['status'],'ready')
    def test_bounded_retry_and_fresh_url_resets_failed(self):
        def fail(*_): raise OSError('secret signed URL must not appear')
        self.store.fetch=fail; self.enqueue(); self.store.process_one()
        self.assertFalse(self.store.process_one())
        self.now+=10; self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['status'],'failed')
        self.assertEqual(self.store.public_media('m')[0]['error'],'download_failed')
        self.store.fetch=self.fetch
        self.enqueue(url=URL.replace('old','fresh')); self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['status'],'ready')
    def test_ready_survives_expired_source_and_restart(self):
        self.enqueue(); self.store.process_one(); old=self.store.public_media('m')
        self.store.close(); self.store=self.new()
        self.enqueue(url=URL.replace('old','new'))
        self.assertFalse(self.store.process_one())
        self.assertEqual(old,self.store.public_media('m'))
    def test_missing_cached_file_can_recover(self):
        self.enqueue(); self.store.process_one()
        asset=self.store.public_media('m')[0]['url'].split('/')[-1]
        (self.store.root/asset).unlink()
        self.enqueue(); self.assertTrue(self.store.process_one())
        self.assertEqual(self.store.public_media('m')[0]['status'],'ready')
    def test_corrupted_cache_requeues_instead_of_serving_bad_bytes(self):
        self.enqueue(); self.store.process_one()
        asset=self.store.public_media('m')[0]['url'].split('/')[-1]
        (self.store.root/asset).write_bytes(b'corrupt')
        self.assertIsNone(self.store.read_asset(asset))
        self.assertEqual(self.store.public_media('m')[0]['status'],'pending')
        self.store.process_one()
        self.assertEqual(self.store.read_asset(asset)[0],PNG)
    def test_payload_size_limit_is_enforced_with_injected_fetch(self):
        self.store.fetch=lambda *_:(PNG*3,'image/png')
        self.enqueue()
        with patch.dict('media_store.LIMITS',{'image':len(PNG)}): self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['error'],'too_large')
    def test_new_source_while_old_fetch_inflight_is_not_overwritten(self):
        def change(url,limit):
            self.enqueue(url=URL.replace('old','new'))
            return PNG,'image/png'
        self.enqueue(); self.store.fetch=change; self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['status'],'pending')
        self.store.fetch=self.fetch; self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['status'],'ready')
    def test_invalid_type_and_failed_atomic_write_never_ready(self):
        self.store.fetch=lambda *_:(b'<html>error</html>','image/png')
        self.enqueue(); self.store.process_one()
        self.assertEqual(self.store.public_media('m')[0]['error'],'unsupported_media')
        self.assertEqual(list(self.store.root.iterdir()),[])
        self.store.fetch=self.fetch; self.enqueue(url=URL.replace('old','new'))
        with patch('media_store.os.replace',side_effect=OSError('disk full')):
            self.store.process_one()
        self.assertEqual(list(self.store.root.iterdir()),[])
        self.assertNotEqual(self.store.public_media('m')[0]['status'],'ready')
    def test_path_and_source_rejection(self):
        for name in ('../media.sqlite','/media.sqlite','0'*64+'.svg','0'*64+'.png/../../x'):
            self.assertIsNone(self.store.read_asset(name))
        for url in ('http://p9.ecombdimg.com/x','https://127.0.0.1/x','https://ecombdimg.com.evil.test/x',
                    'https://user:password@ecombdimg.com/x','https://ecombdimg.com:444/x','file:///x'):
            with self.assertRaises(ValueError): validate_url(url)
    def test_private_dns_and_untrusted_redirect_rejected(self):
        with patch('media_store.socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
            with self.assertRaisesRegex(ValueError,'non_public_address'): fetch_https(URL,1000)
        class Response:
            status=302
            def getheader(self,*_): return 'https://evil.test/private'
        class Connection:
            def __init__(self,*_): pass
            def request(self,*_,**__): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch('media_store.socket.getaddrinfo',return_value=[(None,None,None,None,('8.8.8.8',443))]),patch('media_store._PinnedHTTPS',Connection):
            with self.assertRaisesRegex(ValueError,'untrusted_source'): fetch_https(URL,1000)
    def test_kind_limits_and_video_signature(self):
        self.enqueue(kind='video'); self.store.fetch=lambda *_:(b'\x00\x00\x00\x18ftypisom' + b'video','video/mp4')
        self.store.process_one()
        self.assertTrue(self.store.public_media('m')[0]['url'].endswith('.mp4'))
        with self.assertRaises(ValueError): media_extension(PNG,'video')

if __name__=='__main__': unittest.main()
