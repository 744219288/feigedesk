"""User-selected outbound assets. Client never supplies a filesystem path."""
import base64,hashlib,io,json,re,uuid
from pathlib import Path

LIMIT=16*1024*1024
FORMATS={'.png':('image','image/png'),'.jpg':('image','image/jpeg'),'.jpeg':('image','image/jpeg'),
         '.gif':('image','image/gif'),'.mp4':('video','video/mp4'),'.pdf':('file','application/pdf')}

class OutboundAssets:
    def __init__(self,directory):
        self.directory=Path(directory)/'outbound-assets';self.directory.mkdir(exist_ok=True,parents=True)
    def upload(self,body):
        name=body.get('name');encoded=body.get('data')
        if not isinstance(name,str) or not re.fullmatch(r'[^\\/:\x00-\x1f]{1,150}',name):raise ValueError('文件名无效')
        ext=Path(name).suffix.lower()
        if ext not in FORMATS:raise ValueError('仅支持 PNG/JPG/GIF、MP4 和 PDF')
        if not isinstance(encoded,str) or len(encoded)>((LIMIT+2)//3)*4:raise ValueError('素材最大 16 MiB')
        try:raw=base64.b64decode(encoded,validate=True)
        except Exception:raise ValueError('素材编码无效') from None
        if not raw or len(raw)>LIMIT:raise ValueError('素材为空或超过 16 MiB')
        kind,mime=FORMATS[ext]
        if kind=='image':
            if len(raw)>10*1024*1024:raise ValueError('图片最大 10 MiB')
            from PIL import Image
            try:
                with Image.open(io.BytesIO(raw)) as im:
                    if im.width*im.height>40000000:raise ValueError('图片像素过大')
                    expected={'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.gif':'GIF'}[ext]
                    if im.format!=expected:raise ValueError('图片格式与扩展名不符')
                    im.verify()
            except Exception:raise ValueError('图片损坏或格式不支持') from None
        if kind=='video' and (len(raw)<16 or raw[4:8]!=b'ftyp'):raise ValueError('不是有效 MP4 文件')
        if kind=='file' and (not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]):raise ValueError('不是完整 PDF 文件')
        aid=uuid.uuid4().hex
        meta={'id':aid,'name':name,'kind':kind,'mime':mime,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
              'md5':hashlib.md5(raw).hexdigest(),'extension':ext}
        (self.directory/(aid+ext)).write_bytes(raw)
        (self.directory/(aid+'.json')).write_text(json.dumps(meta,ensure_ascii=False),encoding='utf8')
        return meta
    def read(self,aid):
        if not isinstance(aid,str) or not re.fullmatch('[0-9a-f]{32}',aid):raise ValueError('素材编号无效')
        try:
            meta=json.loads((self.directory/(aid+'.json')).read_text(encoding='utf8'))
            if meta['extension'] not in FORMATS:raise ValueError('素材扩展名无效')
            raw=(self.directory/(aid+meta['extension'])).read_bytes()
            if len(raw)!=meta['size'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise ValueError('素材已经变化')
            return meta,raw
        except (OSError,KeyError,json.JSONDecodeError):raise ValueError('找不到已上传素材') from None
