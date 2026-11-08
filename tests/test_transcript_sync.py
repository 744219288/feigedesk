import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from transcript_sync import transcript_rows, valid_media, SNAPSHOT_JS
import test_core

class TranscriptTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    live = test_core.CoreTests.live
    inbound = test_core.CoreTests.inbound
    def test_offline_snapshot_recovers_both_sides_without_auto_reply(self):
        self.live()
        self.s.on_message({'snapshot':[
            dict(id='123_CsAssign_',role='system',text='接入',clock='昨天 12:00'),
            dict(id='offline-image',role='customer',text='',clock='昨天 12:00:01',media=[{'kind':'image','url':'https://p9-pigeon-sign.ecombdimg.com/test.png'}]),
            dict(id='offline-reply',role='agent',text='收到',clock='昨天 12:00:02')]})
        result=self.s.conversation('live:123')['messages']
        self.assertEqual([m['role'] for m in result],['system','customer','agent'])
        self.assertFalse(self.s.timers)
        self.assertIsNone(self.s._latest('live:123'))

    def test_snapshot_and_wire_share_explicit_talk_identity(self):
        self.live()
        self.s.on_message({'snapshot':[dict(id='123_Welcome_',role='agent',text='hi',clock='昨天 12:00')]})
        self.s.on_message(dict(id='wire',conversation_id='123',role='customer',text='hi',timestamp=test_core.now(),shop_scope='shop_id:1',shop_scope_type='shop_id',buyer_id_type='security_sender_id',buyer_id='buyer'))
        self.assertEqual(len(self.s.conversation('live:123')['messages']),2)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM conversations WHERE id LIKE 'live:%' AND id<>'live:abc'").fetchone()[0],1)

    def test_first_image_before_assign_belongs_to_new_talk(self):
        now=datetime(2026,10,23,13,tzinfo=timezone.utc)
        rows=[dict(id='close_non_process_1',role='system',text='closed',clock='12:00'),
              dict(id='first-image',role='customer',text='',clock='12:01:01',media=[dict(kind='image',url='https://example.com/a.png')]),
              dict(id='2_CsAssign_',role='system',text='接入',clock='12:01')]
        result=list(transcript_rows(rows,now))
        self.assertEqual(result[0]['conversation_id'],'2')
        self.assertEqual(result[0]['id'],'first-image')
    def test_both_sides_welcome_dedup_and_no_auto(self):
        self.live()
        for mid,role,text in [('a','customer','你好'),('b','agent','Hi，欢迎光临')]:
            item=dict(id=mid,conversation_id='abc',role=role,text=text,timestamp=1000,transcript=True)
            self.s.on_message(item); self.s.on_message(item)
        messages=self.s.conversation('live:abc')['messages']
        self.assertEqual(len(messages),3)
        self.assertEqual([m['text'] for m in messages[:2]],['你好','Hi，欢迎光临'])
        self.assertFalse(self.s.timers)
        self.assertFalse(self.s.conversation('live:abc')['human'])
        self.assertEqual(self.s._latest('live:abc')['id'],'live:1')

    def test_late_wire_echo_reconciles_dom_id(self):
        self.live()
        self.s.on_message(dict(id='client',conversation_id='abc',role='agent',text='reply',timestamp=1000,transcript=True))
        self.s.on_message(dict(id='server',client_message_id='client',conversation_id='abc',role='agent',text='reply',timestamp=1000,historical=True))
        messages=self.s.conversation('live:abc')['messages']
        self.assertEqual(len(messages),2)
        self.assertEqual(messages[0]['id'],'live:server')

    def test_rendered_boundary_never_uses_nickname(self):
        now=datetime(2026,10,23,13,tzinfo=timezone.utc)
        rows=[dict(id='unknown',role='customer',text='ignore',clock='12:00:00'),
              dict(id='123_Welcome_',role='agent',text='welcome',clock='12:01:00'),
              dict(id='uuid',role='customer',text='hello',clock='12:02:00')]
        result=list(transcript_rows(rows,now))
        self.assertEqual([r['conversation_id'] for r in result],['123','123'])
        self.assertEqual([r['role'] for r in result],['agent','customer'])

    def test_media_only_both_sides_and_late_loading(self):
        self.live()
        for role in ('customer','agent'):
            item=dict(id=role,conversation_id='abc',role=role,text='',timestamp=1000,transcript=True,
                      media=[dict(kind='image',url='https://example.com/test.png')])
            self.s.on_message(item); self.s.on_message(item)
        result=self.s.conversation('live:abc')['messages']
        self.assertEqual(len(result),3)
        self.assertTrue(all(m['media'] and m['media'][0]['kind']=='image' for m in result[:2]))
        self.assertTrue(all(not m['media'][0]['url'].startswith('https:') for m in result[:2]))
        self.assertFalse(self.s.timers)

    def test_media_urls_cannot_execute_scripts(self):
        for url in ('javascript:alert(1)','file:///secret','http://localhost/x','https://user:pass@example.com/x','blob:foo'):
            self.assertEqual(valid_media([dict(kind='image',url=url)]),[])

    def test_close_boundary_does_not_attach_next_customer_to_old_talk(self):
        now=datetime(2026,10,23,13,tzinfo=timezone.utc)
        rows=[dict(id='123_Welcome_',role='agent',text='hi',clock='12:00:00'),
              dict(id='close_non_process_123',role='system',text='closed',clock='12:01'),
              dict(id='next',role='customer',text='new',clock='12:02')]
        self.assertEqual(len(list(transcript_rows(rows,now))),2)

if __name__=='__main__': unittest.main()
