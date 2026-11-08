import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from media_processing import prepare_attachment, AttachmentError
from model_media import build_history,MediaInputError
from media_store import MediaStore,validate_url
from vendor.feige.ws_reader import attachment_data
from core import Service,now


def pdf_fixture(text='Return period: 7 days. Order TEST-426.'):
    # Small valid PDF with a native text layer; no dependency on PDF authoring tools.
    stream=f'BT /F1 20 Tf 30 130 Td ({text}) Tj ET'.encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data=b'%PDF-1.4\n';offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data));data+=f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    start=len(data)
    data+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return data+f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode()


def video_fixture(path):
    import cv2,numpy as np
    out=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'mp4v'),10,(400,240))
    if not out.isOpened():raise RuntimeError('Test video encoder missing')
    for i in range(30):
        frame=np.full((240,400,3),255,dtype=np.uint8)
        cv2.rectangle(frame,(20+i*8,90),(70+i*8,140),(0,0,255),-1)
        cv2.putText(frame,'TEST: RED BOX MOVES RIGHT',(10,35),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,0,0),2)
        out.write(frame)
    out.release()
    return path.read_bytes()


class AttachmentTests(unittest.TestCase):
    def test_native_pdf_contains_text_and_actual_page_image(self):
        result=prepare_attachment(pdf_fixture(),'file','application/pdf')
        self.assertEqual(result['pages'],1)
        self.assertIn('TEST-426',str(result['parts']))
        images=[p for p in result['parts'] if p['type']=='image_url']
        self.assertEqual(len(images),1)
        self.assertTrue(base64.b64decode(images[0]['image_url']['url'].split(',')[1]).startswith(b'\xff\xd8'))

    def test_scanned_pdf_has_each_page_pixels(self):
        from PIL import Image,ImageDraw
        page=Image.new('RGB',(600,250),'white');ImageDraw.Draw(page).text((30,70),'SCAN-849',fill='black',font_size=36)
        stream=io.BytesIO();page.save(stream,format='PDF',save_all=True,append_images=[page])
        result=prepare_attachment(stream.getvalue(),'file','application/pdf')
        self.assertEqual(result['pages'],2)
        self.assertEqual(sum(p['type']=='image_url' for p in result['parts']),2)

    def test_broken_pdf_fails_without_fake_text(self):
        with self.assertRaises(AttachmentError):prepare_attachment(b'%PDF-1.4 corrupt','file','application/pdf')

    def test_oversized_document_never_silently_drops_pages(self):
        from PIL import Image
        page=Image.new('RGB',(100,100),'white')
        stream=io.BytesIO();page.save(stream,format='PDF',save_all=True,append_images=[page]*24)
        with self.assertRaisesRegex(AttachmentError,'24'):
            prepare_attachment(stream.getvalue(),'file','application/pdf')

    def test_video_samples_include_beginning_and_end_and_audio_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            data=video_fixture(Path(d)/'movie.mp4')
        result=prepare_attachment(data,'video','video/mp4')
        self.assertGreaterEqual(result['frames'],2)
        self.assertFalse(result['audio_transcribed'])
        times=[p['text'] for p in result['parts'] if p['type']=='text']
        self.assertIn('0.00',times[1]);self.assertIn('2.90',times[-1])

    def test_pdf_download_then_model_input_uses_bytes_not_source(self):
        with tempfile.TemporaryDirectory() as d:
            store=MediaStore(d,fetch=lambda *_:(pdf_fixture(),'application/pdf'))
            try:
                store.ensure('m',[{'kind':'file','url':'https://p3.byteimg.com/doc.pdf?private=token'}])
                store.process_one()
                result=build_history([dict(id='m',text='退货期限是什么？',media='present',role='customer')],store)
                self.assertIn('7 days',str(result));self.assertNotIn('private=token',str(result))
            finally:store.close()

    def test_file_document_and_video_metadata_do_not_treat_cover_as_video(self):
        media,_=attachment_data({'type':'file_document','documentInfo':json.dumps({'url':'https://p3.byteimg.com/test.pdf','name':'test.pdf'})},'')
        self.assertEqual(media[0]['kind'],'file')
        media,_=attachment_data({'type':'video','msg_render_model':json.dumps({'render_body':{'vid':'v123456789','coverURL':'https://p3.byteimg.com/cover.png'}})},'[视频]')
        self.assertEqual(media,[{'kind':'video','url':'','video_id':'v123456789'}])

    def test_video_cdn_boundary(self):
        validate_url('https://v11-pigeon.jinritemai.com/video.mp4')
        for url in ['https://v11-pigeon.jinritemai.com.evil.test/a','https://pigeon.jinritemai.com/backstage/currentuser','https://127.0.0.1/a']:
            with self.assertRaises(ValueError):validate_url(url)

    def test_pdf_service_generation(self):
        with tempfile.TemporaryDirectory() as d:
            captured=[]
            service=Service(d,model_call=lambda settings,messages:captured.extend(messages) or '资料写明退货期限为7天。')
            try:
                service.media_store.fetch=lambda *_:(pdf_fixture(),'application/pdf')
                service.on_message(dict(id='doc',conversation_id='buyer',role='customer',text='期限是多少？',timestamp=now(),media=[dict(kind='file',url='https://p3.byteimg.com/doc.pdf')]))
                service.mode='live'
                result=service.generate('live:buyer')
                self.assertIn('7天',result['draft']['text'])
                self.assertIn('TEST-426',str(captured))
            finally:service.close();service.db.close()

    def test_pending_attachment_retries_without_handoff_or_send(self):
        from test_core import FakeTransport
        with tempfile.TemporaryDirectory() as d:
            service=Service(d,FakeTransport,lambda *_:'unused')
            try:
                service.connect()
                service.on_message(dict(id='pending',conversation_id='buyer',role='customer',text='[视频]',timestamp=now()))
                service.settings['auto_reply']=True
                with patch.object(service,'generate',side_effect=MediaInputError('等待附件',retryable=True)),patch('core.threading.Timer') as timer:
                    service._auto('live:buyer','live:pending',service.revision)
                    timer.return_value.start.assert_called_once()
                    self.assertIsNone(service.conversation('live:buyer')['assistance'])
                    self.assertFalse(service.transport.calls)
                service.timers.clear()
            finally:service.close();service.db.close()


if __name__=='__main__':unittest.main()
