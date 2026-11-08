import base64
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Service, now
from media_store import MediaStore
from model_media import build_history, MediaInputError, denies_image

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
URL = 'https://p9-pigeon-sign.ecombdimg.com/customer.png?secret=signature'


class ModelMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MediaStore(self.tmp.name, fetch=lambda *_: (PNG, 'image/png'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def row(self, mid='m', text='', media='present', role='customer'):
        return dict(id=mid, text=text, media=media, role=role)

    def cache(self, mid='m'):
        self.store.ensure(mid, [dict(kind='image', url=URL)])
        self.store.process_one()

    def test_image_then_followup_keeps_actual_pixels(self):
        self.cache()
        result = build_history([self.row(), self.row('next', '这是你们家的产品吧', None)], self.store)
        uri = result[0]['content'][1]['image_url']['url']
        self.assertEqual(base64.b64decode(uri.split(',')[1]), PNG)
        self.assertEqual(result[1]['content'], '这是你们家的产品吧')
        self.assertNotIn('signature', str(result))
        self.assertNotIn('https://', str(result))

    def test_missing_or_pending_image_never_becomes_text_only(self):
        with self.assertRaises(MediaInputError):
            build_history([self.row()], self.store, 0)
        self.store.ensure('m', [dict(kind='image', url=URL)])
        with self.assertRaises(MediaInputError):
            build_history([self.row()], self.store, 0)

    def test_unrelated_image_not_attached(self):
        self.cache('other-customer')
        self.assertEqual(build_history([self.row('m', '你好', None)], self.store), [{'role': 'user', 'content': '你好'}])

    def test_corrupt_image_not_submitted(self):
        self.cache()
        next(self.store.root.iterdir()).write_bytes(b'corrupt')
        with self.assertRaises(MediaInputError):
            build_history([self.row()], self.store, 0)

    def test_old_denial_not_repeated_as_model_history(self):
        result = build_history([self.row('old', '目前我无法查看/识别图片内容', None, 'agent'), self.row('m', '为什么', None)], self.store)
        self.assertEqual(len(result), 1)

    def test_denial_guard(self):
        for text in ['图片没显示', '我无法查看/识别图片', '只能处理文字信息', '没有看到您的具体商品信息']:
            self.assertTrue(denies_image(text), text)
        self.assertFalse(denies_image('图中上方是抽绳垃圾袋，下方是其他店铺的精选好物，您问的是哪一款？'))
        self.assertFalse(denies_image('无法确认图片中的商品是不是本店的。图中上方是抽绳垃圾袋。'))

    def test_file_placeholder_not_sent_as_read_document(self):
        with self.assertRaises(MediaInputError):
            build_history([self.row('m', '[文件]', None)], self.store, 0)


class ServiceMediaTests(unittest.TestCase):
    def test_real_generation_passes_image_and_guards_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = []
            s = Service(tmp, model_call=lambda settings, messages: captured.extend(messages) or '目前图片没显示，请重发')
            try:
                s.media_store.fetch = lambda *_: (PNG, 'image/png')
                s.on_message(dict(id='photo', conversation_id='buyer', role='customer', text='', timestamp=now(), media=[dict(kind='image', url=URL)]))
                # Exercise the genuine image-only receive path, then a text follow-up.
                s.on_message(dict(id='followup', conversation_id='buyer', role='customer', text='这是你们家的产品吧', timestamp=now()))
                s.mode = 'live'
                with self.assertRaises(ValueError):
                    s.generate('live:buyer')
                result = s.conversation('live:buyer')
                self.assertTrue(any(isinstance(m['content'], list) and any(p['type']=='image_url' for p in m['content']) for m in captured))
                self.assertFalse(result['human'])
                self.assertEqual(result['assistance']['status'], 'pending')
                self.assertIsNone(result['draft'])
            finally:
                s.close()
                s.db.close()
