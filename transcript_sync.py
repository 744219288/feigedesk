"""Read rendered conversations without modifying the Feige page."""
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

def valid_media(items):
    result = []
    for item in items[:20] if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get('kind') not in ('image', 'video', 'file'):
            continue
        url = item.get('url', '')
        if url == '':
            entry={'kind':item['kind'],'url':''}
            if entry not in result:result.append(entry)
            continue
        if not isinstance(url, str) or len(url) > 16000:
            continue
        try:
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                continue
        except ValueError:
            continue
        entry = {'kind':item['kind'], 'url':url}
        if entry not in result:
            result.append(entry)
    return result

SNAPSHOT_JS = r"""() => [...document.querySelectorAll('[data-qa-id="qa-message-warpper"]')].map(w => {
  const holder = w.querySelector('[data-id]');
  const bubble = w.querySelector('.messageIsMe, .messageNotMe');
  const row = w.querySelector('.Ie29C7uLyEjZzd8JeS8A');
  const clock = w.querySelector('[multiple-click-times="5"]') || w.querySelector('.rcHPT4n3TlQD0Nu4sSiv');
  const media = row ? [...row.querySelectorAll('img,video')].filter(e=>e.alt!=='头像').map(e=>({kind:e.tagName==='VIDEO'?'video':'image',url:e.currentSrc || e.src || ''})) : [];
  return {id:holder?.getAttribute('data-id') || '',
    role:bubble ? (bubble.classList.contains('messageIsMe') ? 'agent' : 'customer') : row ? (row.style.flexDirection==='row-reverse' ? 'agent' : 'customer') : 'system',
    text:bubble ? bubble.innerText : media.length ? '' : w.innerText,
    clock:clock?.innerText || '', media};
})"""

# Read the header and transcript in one evaluation so a customer switch cannot
# combine two independent page reads. Only explicit official source labels count.
CONTEXT_JS = r"""() => {
  const rows = (""" + SNAPSHOT_JS + r""")();
  const visible = e => !!e && e.getClientRects().length > 0;
  const header = document.querySelector('#topbar-sec-container');
  const channel = header?.querySelector('.AzMmxwmwwqzN1dn7P2cL');
  const fields = [];
  for (const e of document.querySelectorAll('span,div,label')) {
    if (!visible(e) || e.closest('[data-qa-id="qa-message-warpper"]') || e.children.length) continue;
    const label = e.textContent.trim().replace(/[：:]$/, '');
    if (!['来源商品','进线来源','咨询来源','来源页面','来源渠道'].includes(label)) continue;
    const parent = e.parentElement;
    if (!visible(parent)) continue;
    const value = parent.innerText.trim().replace(e.innerText,'').trim().replace(/^[：:]\s*/,'');
    if (value && value.length <= 1000 && !value.includes('\n\n')) fields.push({label,value});
  }
  return {snapshot:rows, entry:{channel:visible(channel)?channel.innerText.trim():'',fields}};
}"""

def transcript_rows(rows, now=None, anchor=None):
    now = now or datetime.now().astimezone()
    talk = ''
    after_close = False
    anchors = set(anchor.get('ids', [])) if anchor else set()
    for index,row in enumerate(rows):
        mid = row.get('id', '')
        marker = re.fullmatch(r'(\d+)_(?:CsAssign|Welcome)_', mid)
        if marker:
            talk = marker[1]
            after_close = False
        if mid in anchors:
            talk = anchor['talk']
        if not talk and re.fullmatch(r'close_non_process_\d+',mid):
            after_close=True
            continue
        if not talk and after_close and row.get('role') in ('customer','agent'):
            for following in rows[index+1:]:
                next_marker=re.fullmatch(r'(\d+)_(?:CsAssign|Welcome)_',following.get('id',''))
                if next_marker:
                    talk=next_marker[1]
                    break
                if re.fullmatch(r'close_non_process_\d+',following.get('id','')):
                    break
        media = valid_media(row.get('media'))
        if not media and isinstance(row.get('media'),list):
            kinds={v.get('kind') for v in row['media'] if isinstance(v,dict)} & {'image','video','file'}
            media=[{'kind':kind,'url':''} for kind in sorted(kinds)]
        if not talk or not mid or (not row.get('text') and not media):
            continue
        clock = row.get('clock', '').strip()
        match = re.fullmatch(r'(?:(昨天|前天|\d{4}-\d{2}-\d{2})\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?', clock)
        if not match:
            continue
        day, hour, minute, second = match.groups()
        date = now
        if day in ('昨天', '前天'):
            date -= timedelta(days=1 if day == '昨天' else 2)
        elif day:
            date = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=now.tzinfo)
        stamp = date.replace(hour=int(hour), minute=int(minute), second=int(second or 0), microsecond=0).timestamp()
        # Allow the realtime path to process new messages first.
        if stamp > now.timestamp() - 15:
            continue
        text = row.get('text', '').strip()
        if row['role'] == 'system' and text.startswith(clock):
            text = text[len(clock):].strip()
        if text or media:
            yield dict(id=mid, conversation_id=talk, role=row['role'], text=text,
                       timestamp=stamp, historical=True, transcript=True, media=media)
        if re.fullmatch(r'close_non_process_\d+', mid):
            talk = ''
            after_close = True
