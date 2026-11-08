import asyncio
import sys
from pathlib import Path
import tempfile
import time
import unittest
import json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from feige_api import extract_api_messages,message_endpoint,resolve_video
from feige_transport import FeigeTransport
from vendor.feige import ws_reader,ws_sender
from test_transport import received,message


def response_from_ws(raw):
    dec=ws_reader.decode(raw)
    fields=ws_reader._sub(ws_reader._all(ws_reader._sub(ws_reader._all(ws_reader._sub(ws_reader._all(dec,8)[0]),6)[0]),500)[0])
    actual=ws_reader._sub(ws_reader._all(fields,5)[0])
    return ws_sender.encode([(3,0,0),message(6,[message(2043,[message(1,[message(2,actual)])])])])


class ApiTests(unittest.TestCase):
    def test_video_resolver_uses_authorized_flow_and_releases_responses(self):
        class Response:
            status=200
            disposed=False
            def __init__(self,data):self.data=data
            async def body(self):return json.dumps(self.data).encode()
            async def dispose(self):self.disposed=True
        class Request:
            def __init__(self,url):
                self.calls=[]
                self.responses=[Response({'code':0,'data':{'token':'test_signature=value'}}),Response({'Result':{'Data':{'Status':10,'PlayInfoList':[{'MainPlayUrl':url}]}}})]
            async def get(self,url,**kw):self.calls.append((url,kw));return self.responses[len(self.calls)-1]
        good=Request('https://v11-pigeon.jinritemai.com/test.mp4')
        self.assertEqual(asyncio.run(resolve_video(good,'v12345678')),'https://v11-pigeon.jinritemai.com/test.mp4')
        self.assertTrue(all(x.disposed for x in good.responses))
        self.assertTrue(all(kw['max_redirects']==0 for _,kw in good.calls))
        bad=Request('https://127.0.0.1/private')
        with self.assertRaises(ValueError):asyncio.run(resolve_video(bad,'v12345678'))

    def test_observed_envelope_parses_same_message_fields(self):
        frame=received(text='客户问题',msg_type='text',buyer_id='buyer')
        self.assertEqual(extract_api_messages(response_from_ws(frame)),ws_reader.extract_messages(frame))

    def test_business_error_and_invalid_endpoint_rejected(self):
        with self.assertRaises(ValueError):extract_api_messages(ws_sender.encode([(3,0,401)]))
        self.assertIsNone(message_endpoint('https://evil.test/pigeon_im/v3/message/get_user_message'))
        self.assertIsNone(message_endpoint('https://fxg.jinritemai.com/pigeon_im/v1/message/send_message'))

    def test_http_then_ws_dedup_and_history_no_reply_source(self):
        with tempfile.TemporaryDirectory() as d:
            delivered=[];t=FeigeTransport(d,delivered.append,lambda s:None)
            t._started_at=time.time()-10
            raw=received(timestamp_ms=int((time.time()-100)*1000))
            messages=extract_api_messages(response_from_ws(raw))
            t._accept_messages((1,'http-read'),messages,historical_only=True,enrich=True)
            t._observe_received((1,'socket'),raw)
            self.assertEqual(len(delivered),1)
            self.assertTrue(delivered[0]['historical'])
            self.assertNotIn(messages[0].conversation_id,t._sources)

    def test_live_arrival_during_initial_load_is_promoted_once(self):
        with tempfile.TemporaryDirectory() as d:
            delivered=[];t=FeigeTransport(d,delivered.append,lambda s:None);t._started_at=time.time()-1
            raw=received()
            t._accept_messages((1,'http-read'),extract_api_messages(response_from_ws(raw)),historical_only=True)
            t._observe_received((1,'socket'),raw)
            t._observe_received((1,'socket'),raw)
            self.assertEqual([x['historical'] for x in delivered],[True,False])

    def test_incremental_http_message_recovers_live_source(self):
        with tempfile.TemporaryDirectory() as d:
            delivered=[];t=FeigeTransport(d,delivered.append,lambda s:None);t._started_at=time.time()-1
            t._accept_messages((1,'http-read'),extract_api_messages(response_from_ws(received())))
            self.assertFalse(delivered[0]['historical'])
            self.assertEqual(t._sources[delivered[0]['conversation_id']]['page_id'],1)


if __name__=='__main__':unittest.main()
