import tempfile
import unittest
from inbox_journal import InboxJournal

class InboxTests(unittest.TestCase):
    def test_failed_delivery_survives_restart_and_replays_without_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            journal=InboxJournal(folder)
            item=dict(id='m1',conversation_id='talk',text='hello')
            def fail(_):raise OSError('database busy')
            with self.assertRaises(OSError):journal.deliver(item,fail)
            journal.close()
            restored=InboxJournal(folder); received=[]
            restored.replay(received.append); restored.replay(received.append)
            self.assertEqual(len(received),1)
            self.assertTrue(received[0]['historical'])
            restored.close()

    def test_success_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            journal=InboxJournal(folder); received=[]
            journal.deliver(dict(id='m1',conversation_id='a'),received.append)
            journal.replay(received.append)
            self.assertEqual(len(received),1)
            journal.close()
