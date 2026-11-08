"""Standalone Feige bridge. User-owned persistent browser; no eCan service.

Read: passive CDP WebSocket frames. Write: site's own composer, guarded by
the exact source-message IDs received with its talk_id. Never route by nickname.
Actual acceptance requires the browser's outgoing talk/client ID and matching
inbound server echo. No manual-send bootstrap or expiring template is required.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from vendor.feige import ws_reader, ws_sender
from transcript_sync import SNAPSHOT_JS, CONTEXT_JS, transcript_rows
from inbox_journal import InboxJournal
from feige_api import message_endpoint, extract_api_messages, resolve_video, MAX_BODY

MAX_FRAME = 2 * 1024 * 1024
CONFIRM_TIMEOUT = 12

# This stores socket handles only in the dedicated page. It does not export
# cookies, tokens or credentials. No binding grants a webpage host privileges.
SOCKET_INIT = r"""(() => {
  if (window.__feigeDeskSockets) return;
  const sockets = new Set();
  const Native = window.WebSocket;
  class ObservedSocket extends Native {
    constructor(...args) {
      super(...args); sockets.add(this);
      this.__fdRecv = 0; this.__fdSent = 0;
      this.addEventListener('message', () => ++this.__fdRecv);
      this.addEventListener('close', () => sockets.delete(this));
    }
    send(data) { super.send(data); ++this.__fdSent; }
  }
  Object.defineProperty(window, '__feigeDeskSockets', {value: sockets});
  window.WebSocket = ObservedSocket;
})();"""
SOCKET_STATE_JS = r"""({url}) => {
  const sockets = [...(window.__feigeDeskSockets || [])].filter(
    s => s.url === url && s.readyState === WebSocket.OPEN);
  if (sockets.length !== 1) return null;
  return {recv: sockets[0].__fdRecv, sent: sockets[0].__fdSent};
}"""
SEND_JS = r"""({url, payload, snapshot}) => {
  const sockets = [...(window.__feigeDeskSockets || [])].filter(
    s => s.url === url && s.readyState === WebSocket.OPEN);
  if (sockets.length !== 1) return 'missing';
  if (sockets[0].__fdRecv !== snapshot.recv || sockets[0].__fdSent !== snapshot.sent)
    return 'changed';
  const bytes = Uint8Array.from(atob(payload), c => c.charCodeAt(0));
  sockets[0].send(bytes); return 'sent';
}"""

# Selectors are grounded in upstream site_tools.py. Identity is strengthened:
# a talk ID comes from an actual WS message and is joined to that message's
# exact DOM data-id, never the customer display name or message text.
DOM_JS = r"""(arg) => {
  const visible = el => !!el && el.isConnected && el.getBoundingClientRect().width > 0 &&
    el.getBoundingClientRect().height > 0 && getComputedStyle(el).visibility !== 'hidden';
  const rows = [...document.querySelectorAll('[data-qa-id="qa-conversation-chat-item"]')].filter(visible);
  const sourceIds = new Set(arg.source_ids || []);
  const normal = v => String(v == null ? '' : v).replace(/^3:/, '');
  const idKeys = /^(talk_id|talkId|conversation_id|conversationId|conversation_short_id|conversationShortId)$/;
  function rowIdentity(row) {
    const ids = new Set(), keys = new Set();
    for (const attr of row.attributes) {
      if (/^data-(talk-id|conversation-id|conversation-short-id)$/.test(attr.name)) {
        if (attr.value) ids.add(normal(attr.value)); keys.add(attr.name);
      }
    }
    const seen = new Set();
    function walk(obj, depth) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj) || seen.has(obj) || depth > 4) return;
      seen.add(obj);
      for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(obj))) {
        if (!('value' in descriptor)) continue;
        const value = descriptor.value;
        if (idKeys.test(key) && (typeof value === 'string' || typeof value === 'bigint')) {
          if (value) ids.add(normal(value)); keys.add(key);
        } else if (idKeys.test(key) && typeof value === 'number' && Number.isSafeInteger(value)) {
          ids.add(String(value)); keys.add(key);
        } else if (/^(data|item|conversation|conversationInfo|chat|session|info|props)$/.test(key)) {
          walk(value, depth + 1);
        }
      }
    }
    for (const key of Object.keys(row)) {
      if (key.startsWith('__reactProps$') || key.startsWith('__reactEventHandlers$')) walk(row[key], 0);
      else if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) walk(row[key]?.memoizedProps, 0);
    }
    return {ids: [...ids], keys: [...keys]};
  }
  function bubbles() {
    return [...document.querySelectorAll('[data-qa-id="qa-message-warpper"]')].filter(visible).map(w => {
      const agent = w.querySelector('[class*="messageIsMe"]');
      const customer = w.querySelector('[class*="messageNotMe"]');
      const row = w.querySelector('.Ie29C7uLyEjZzd8JeS8A');
      let role = agent ? 'agent' : customer ? 'customer' : '';
      if (!role && row) role = String(row.style.flexDirection).includes('reverse') ? 'agent' : 'customer';
      const ids = [w, ...w.querySelectorAll('[data-id]')].map(e => e.getAttribute('data-id')).filter(Boolean);
      return {role, ids};
    }).filter(x => x.role);
  }
  function activeProof() {
    const b = bubbles(); const last = b[b.length - 1];
    if (arg.manual) {
      if (!b.some(x=>x.ids.some(id=>sourceIds.has(id)))) return false;
    } else if (!last || last.role !== 'customer' || !last.ids.some(id => sourceIds.has(id))) return false;
    // If the page exposes an explicit selected-row ID, it must agree too.
    const selected = rows.filter(r => r.getAttribute('aria-selected') === 'true' ||
      r.getAttribute('data-state') === 'active' || r.classList.contains('active'));
    for (const r of selected) {
      const ids = rowIdentity(r).ids;
      if (ids.length && (ids.length !== 1 || ids[0] !== arg.talk)) return false;
    }
    return true;
  }
  const inputs = [...document.querySelectorAll('[data-qa-id="qa-send-message-textarea"]')].filter(visible);
  function buttons() {
    let b = [...document.querySelectorAll('[data-qa-id="qa-send-message-button"], [data-qa-id="qa-send-btn"]')].filter(visible);
    if (!b.length) b = [...document.querySelectorAll('button')].filter(e => visible(e) && /^发送(?:\s*[(（].*[)）])?$/.test(e.textContent.trim()));
    return [...new Set(b)].filter(e => !e.disabled && e.getAttribute('aria-disabled') !== 'true');
  }
  function value(input) { return 'value' in input ? input.value : input.textContent; }
  function setValue(input, text) {
    if (input.tagName === 'TEXTAREA') Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, text);
    else if (input.tagName === 'INPUT') Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, text);
    else input.textContent = text;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }
  if (arg.action === 'sync_probe') return {
    row_count:rows.length,
    has_manual_draft:inputs.some(i=>String(value(i)).trim()),
    matched:bubbles().some(b=>b.ids.some(id=>sourceIds.has(id)))
  };
  if (arg.action === 'sync_history') {
    if(inputs.some(i=>String(value(i)).trim())) return {ok:false};
    let p=document.querySelector('[data-qa-id="qa-message-warpper"]')?.parentElement;
    while(p && p!==document.body){
      if(p.scrollHeight>p.clientHeight+20 && /auto|scroll/.test(getComputedStyle(p).overflowY)){
        const before=p.scrollTop;
        p.scrollTop=Math.max(0,before-Math.max(200,p.clientHeight*.8));
        return {ok:true,moved:p.scrollTop!==before};
      }
      p=p.parentElement;
    }
    return {ok:false};
  }
  if (arg.action === 'sync_recent') {
    if(inputs.some(i=>String(value(i)).trim())) return {ok:false};
    const tab=[...document.querySelectorAll('[role="tab"]')].find(e=>visible(e)&&e.textContent.trim()==='最近联系');
    if(tab && tab.getAttribute('aria-selected')!=='true'){tab.click();return {ok:true};}
    return {ok:false};
  }
  if (arg.action === 'probe') {
    // Structural diagnostics only. Labels inside chat bubbles are customer
    // content, not evidence that the sidebar identifies an acquisition source.
    const sourceLabels = new Set(['来源商品','咨询来源','来源信息','进线来源','当前咨询商品','咨询商品']);
    function nearbyStructure(el) {
      const result=[];
      for (let p=el.parentElement, depth=0; p && p!==document.body && depth<3; p=p.parentElement,depth++) {
        const nodes=[p,...p.querySelectorAll('*')].slice(0,60);
        result.push({depth,tag:p.tagName,qa:p.getAttribute('data-qa-id')||'',
          classes:[...p.classList].slice(0,12),nodes:nodes.map(n=>({tag:n.tagName,
            qa:n.getAttribute('data-qa-id')||'',classes:[...n.classList].slice(0,8),
            product_attribute_names:[...n.attributes].map(a=>a.name).filter(x=>/^data-(goods|product|item|sku)(-|$)/.test(x))
          }))});
      }
      return result;
    }
    const productLabels = [...document.querySelectorAll('span,div,label,h3,h4')].filter(el =>
      visible(el) && !el.children.length && !el.closest('[data-qa-id="qa-message-warpper"]') &&
      sourceLabels.has((el.textContent || '').trim())).slice(0,12).map(el => ({
        label:el.textContent.trim(), attribute_names:[...el.attributes].map(a=>a.name),
        parent_tag:el.parentElement?.tagName || '',
        sibling_image_count:el.parentElement?.querySelectorAll('img').length || 0,
        sibling_link_count:el.parentElement?.querySelectorAll('a').length || 0,
        nearby_structure:nearbyStructure(el)
      }));
    const details = rows.slice(0, 30).map(r => ({attribute_names:[...r.attributes].map(a=>a.name),
      identity_keys:rowIdentity(r).keys, identity_count:rowIdentity(r).ids.length}));
    return {row_count: rows.length, rows: details, input_count: inputs.length, send_button_count: buttons().length,
      product_probe:{source_verified:activeProof(), labels:productLabels},
      message_count:bubbles().length, source_match_count:bubbles().filter(b=>b.ids.some(id=>sourceIds.has(id))).length,
      exact_talk_rows: rows.filter(r=>rowIdentity(r).ids.includes(arg.talk)).length,
      open_sockets:[...(window.__feigeDeskSockets||[])].filter(s=>s.readyState===WebSocket.OPEN).length,
      actual_im_socket_count:[...(window.__feigeDeskSockets||[])].filter(s=>{
        if(s.readyState!==WebSocket.OPEN) return false;
        try { const u=new URL(s.url); return u.protocol==='wss:' && u.hostname==='ws.fxg.jinritemai.com'; }
        catch { return false; }
      }).length};
  }
  if (arg.action === 'locate') {
    if (activeProof()) return {ok:true, active:true};
    const matches = rows.filter(r => { const ids=rowIdentity(r).ids; return ids.length===1 && ids[0]===arg.talk; });
    if (matches.length === 1) { matches[0].click(); return {ok:true, clicked:true}; }
    return {ok:false, reason:matches.length>1?'ambiguous_rows':'not_visible', rows:rows.length};
  }
  if (arg.action === 'open_index') {
    if (arg.index < 0 || arg.index >= Math.min(rows.length, 40)) return {ok:false};
    rows[arg.index].click(); return {ok:true};
  }
  if (arg.action === 'check') return {ok:activeProof()};
  if (arg.action === 'native_upload') {
    if(!arg.manual || !activeProof()) return {ok:false,reason:'wrong_conversation'};
    if(inputs.length!==1 || inputs.some(i=>String(value(i)).trim())) return {ok:false,reason:'manual_draft'};
    if(window.__fdNativeOperation) return {ok:false,reason:'busy'};
    if([...document.querySelectorAll('[data-qa-id$="popup-submit"]')].some(visible)) return {ok:false,reason:'existing_upload_preview'};
    const qa={image:'qa-send-img',video:'qa-send-video',file:'qa-send-file'}[arg.kind];
    const candidates=[...document.querySelectorAll('[data-qa-id="'+qa+'"]')].filter(visible);
    const file=candidates.length===1?candidates[0].closest('label')?.querySelector('input[type=file]'):null;
    if(!file || file.disabled) return {ok:false,reason:'upload_unavailable'};
    const bytes=Uint8Array.from(atob(arg.payload),c=>c.charCodeAt(0));
    const dt=new DataTransfer();dt.items.add(new File([bytes],arg.name,{type:arg.mime}));
    file.files=dt.files;
    window.__fdNativeOperation={token:arg.token};
    const cover=document.createElement('div');cover.id='fd-native-operation';
    cover.textContent='正在通过飞鸽发送素材，请稍候…';
    Object.assign(cover.style,{position:'fixed',inset:'0',zIndex:'2147483647',background:'#ffffff88',display:'grid',placeItems:'center',fontSize:'20px'});
    document.body.append(cover);
    file.dispatchEvent(new Event('change',{bubbles:true}));
    return {ok:true,submitted:true};
  }
  if(arg.action==='native_confirm'){
    if(!arg.manual || !activeProof() || window.__fdNativeOperation?.token!==arg.token) return {ok:false,reason:'wrong_conversation'};
    const qa={image:'qa-send-img-popup-submit',video:'qa-send-file-popup-submit',file:'qa-send-file-popup-submit'}[arg.kind];
    const matches=[...document.querySelectorAll('[data-qa-id="'+qa+'"]')].filter(visible);
    if(matches.length!==1)return {ok:false,reason:'preview_not_ready'};
    const panel=matches[0].closest('[role=dialog]') || matches[0].parentElement?.parentElement;
    if(!panel || !panel.textContent.includes(arg.name))return {ok:false,reason:'preview_file_changed'};
    if(matches[0].disabled || matches[0].getAttribute('aria-disabled')==='true')return {ok:false,reason:'preview_not_ready'};
    matches[0].click();return {ok:true,submitted:true};
  }
  if(arg.action==='native_release'){
    if(window.__fdNativeOperation?.token===arg.token){delete window.__fdNativeOperation;document.getElementById('fd-native-operation')?.remove();}
    return {ok:true};
  }
  if(arg.action==='native_coupon'){
    if(!arg.manual || !activeProof()) return {ok:false,reason:'wrong_conversation'};
    if(inputs.length!==1 || inputs.some(i=>String(value(i)).trim())) return {ok:false,reason:'manual_draft'};
    const names=[...document.querySelectorAll('*')].filter(e=>visible(e)&&!e.children.length&&!e.closest('[data-qa-id="qa-message-warpper"]')&&e.textContent.trim()===arg.coupon.name);
    if(names.length!==1)return {ok:false,reason:'coupon_not_unique'};
    let parent=names[0],button=null;
    for(let depth=0;parent&&depth<7;depth++,parent=parent.parentElement){
      const matches=[...parent.querySelectorAll('*')].filter(e=>visible(e)&&!e.children.length&&e.textContent.trim()==='立即发送');
      if(matches.length===1){button=matches[0];break;}if(matches.length>1)break;
    }
    if(!button)return {ok:false,reason:'coupon_button_missing'};
    if(arg.probe)return {ok:true};
    button.click();return {ok:true,submitted:true};
  }
  if (arg.action === 'abort') {
    const t = window.__feigeDeskDraft;
    if (t && t.token===arg.token) {
      if (t.input.isConnected && value(t.input)===t.text) setValue(t.input, '');
      delete window.__feigeDeskDraft;
    }
    return {ok:true};
  }
  if (arg.action === 'prepare') {
    if (!activeProof()) return {ok:false, reason:'source_changed'};
    if (inputs.length !== 1) return {ok:false, reason:'composer_not_unique'};
    const input = inputs[0];
    if (String(value(input)).trim()) return {ok:false, reason:'existing_manual_draft'};
    input.focus(); setValue(input, arg.text);
    window.__feigeDeskDraft = {token:arg.token, input, text:arg.text, talk:arg.talk};
    return {ok:value(input)===arg.text};
  }
  if (arg.action === 'commit') {
    const t=window.__feigeDeskDraft;
    if (!t || t.token!==arg.token || t.talk!==arg.talk || !t.input.isConnected ||
        value(t.input)!==arg.text || !activeProof()) return {ok:false, reason:'source_changed'};
    const b=buttons();
    if(b.length!==1) return {ok:false, reason:'send_button_not_unique'};
    // Final identity/source proof and click share one JS turn: there is no
    // asynchronous gap where the active conversation can switch.
    b[0].click(); delete window.__feigeDeskDraft;
    return {ok:true, submitted:true};
  }
  return {ok:false, reason:'unknown_action'};
}"""


def _feige_page(url):
    return urlparse(url).hostname == 'im.jinritemai.com'


def _im_channel_ready(url, probe):
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname == 'im.jinritemai.com'
            and parsed.path.rstrip('/') == '/pc_seller_v2/main/workspace'
            and probe.get('actual_im_socket_count', 0) > 0)


def build_exact_frame(template, conversation_id, text, client_id):
    """Fail closed on unfamiliar frame shape; preserve receiver and talk ID."""
    dec = ws_reader.decode(template)
    if (not dec or ws_sender.sent_talk(template) != conversation_id
            or not ws_sender.frame_receiver_id(template)
            or ws_sender.get_path(dec, ws_sender.CLIENT_ID_PATH) is None
            or ws_sender.encode(dec) != template):
        raise ValueError('发送模板不能证明会话归属，请在飞鸽中人工回复该会话后重试。')
    old = ws_sender.get_path(dec, ws_sender.CLIENT_ID_PATH)[1]
    if not isinstance(old, tuple) or old[0] != 'str' or not old[1]:
        raise ValueError('模板消息标识缺失，不能安全发送。')
    old_id = old[1]
    dec = ws_sender.set_path(dec, ws_sender.TEXT_PATH, (2, ('str', text)))
    # Mirror client IDs can occur both at .8 and in repeated kv entries.
    def replace(fields):
        result = []
        for f, wt, val in fields:
            if wt == 2 and isinstance(val, tuple):
                if val[0] == 'msg':
                    val = ('msg', replace(val[1]))
                elif val[0] == 'str' and val[1] == old_id:
                    val = ('str', client_id)
                elif val[0] == 'str' and val[1].startswith('\n'):
                    # The upstream decoder classifies printable protobuf kv
                    # submessages as strings. Update the mirrored client ID
                    # by decoding/re-encoding that exact kv entry too.
                    inner = ws_reader.decode(val[1].encode('utf-8'))
                    if inner and len(inner) == 2:
                        k, v = inner
                        if (k[:2] == (1, 2) and k[2] in
                                (('str', 's:client_message_id'), ('str', 'client_message_id'))
                                and v == (2, 2, ('str', old_id))):
                            val = ('msg', [k, (2, 2, ('str', client_id))])
            result.append((f, wt, val))
        return result
    frame = ws_sender.encode(replace(dec))
    if (ws_sender.sent_talk(frame) != conversation_id
            or ws_sender.frame_receiver_id(frame) != ws_sender.frame_receiver_id(template)
            or ws_sender.frame_text(frame) != text):
        raise ValueError('发送帧校验失败，消息未发送。')
    return frame


class FeigeTransport:
    def __init__(self, data_dir, on_message, on_status):
        self.data_dir = Path(data_dir)
        self.on_message = on_message
        self.on_status = on_status
        self._lock = threading.RLock()
        self._status = {'state': 'disconnected', 'detail': '尚未打开专用飞鸽浏览器'}
        self._thread = None
        self._loop = None
        self._shutdown = None
        self._context = None
        self._routes = {}
        self._receivers = {}
        self._pending = {}
        self._seen = set()
        self._history_seen = set()
        self._message_aliases = {}
        self._sockets = {}
        self._attached = set()
        self._started_at = 0
        self._last_turn = {}
        self._last_ts_ms = {}
        self._stop_requested = threading.Event()
        self._cdp_sessions = []
        self._pages = {}
        self._sources = {}
        self._manual_sources = {}
        self._native_pending = None
        self._dom_pending = {}
        self._dom_lock = None
        self._journal = InboxJournal(self.data_dir)
        self.pending_sync = lambda: []
        self._sync_cursor = 0
        self._api_tasks = set()
        self._media_tasks = set()
        self._media_pending = set()
        self._official = None
        self._business_identities = {}
        self._business_tasks = set()
        self._diag = {'frames_received': 0, 'frames_sent': 0, 'parsed_messages': 0,
                      'skipped_origin': 0, 'skipped_opcode': 0, 'parse_errors': 0,
                      'attached_pages': 0, 'last_error': '', 'pages': [], 'socket_hosts': [],
                      'identity_mappings': {}}

    def diagnostics(self):
        with self._lock:
            return json.loads(json.dumps(self._diag))

    async def _watch_status(self):
        while not self._shutdown.is_set():
            try:
                self._journal.replay(self.on_message)
                pages = []
                for p in self._context.pages:
                    u = urlparse(p.url)
                    pages.append({'origin': u.scheme+'://'+(u.hostname or ''), 'path': u.path})
                with self._lock:
                    self._diag['pages'] = pages
                    self._diag['socket_hosts'] = sorted({urlparse(u).hostname for u in self._sockets.values() if urlparse(u).hostname})
                for page in self._context.pages:
                    if not _feige_page(page.url):
                        continue
                    talk = next(reversed(self._sources), '')
                    source = self._sources.get(talk, {})
                    probe = await asyncio.wait_for(page.evaluate(DOM_JS, {'action':'probe', 'talk':talk,
                        'source_ids':source.get('ids', [])}), timeout=3)
                    self._diag['dom'] = probe
                    bundle = await asyncio.wait_for(page.evaluate(CONTEXT_JS), timeout=3)
                    # Confirm on a second observation of the same transcript.
                    # Keep observations per transcript while cycling customers.
                    key=tuple(r.get('id','') for r in bundle['snapshot'])
                    signature=json.dumps(bundle['entry'],ensure_ascii=False)
                    seen=getattr(self,'_entry_observations',{})
                    entry=bundle['entry'] if key and seen.get(key)==signature else None
                    seen[key]=signature
                    if len(seen)>100:seen.pop(next(iter(seen)))
                    self._entry_observations=seen
                    self._journal.deliver({'id':'visible-snapshot','conversation_id':'snapshot','snapshot':bundle['snapshot'],'entry':entry},self.on_message)
                    await self._recover_pending_media(page)
                    # A specific live IM channel is useful even when no
                    # conversations are visible. Do not claim login from URL.
                    if _im_channel_ready(page.url, probe):
                        if self.status()['state'] in ('waiting_login','reconnecting'):
                            self._set_status('connected', '飞鸽消息通道已连接，等待新消息。')
                    elif self.status()['state']=='connected':
                        self._set_status('reconnecting','飞鸽消息通道正在恢复，未确认发送的消息不会重复发送。')
                    break
                self.data_dir.mkdir(parents=True, exist_ok=True)
                (self.data_dir/'transport-diagnostics.json').write_text(json.dumps(self.diagnostics(), ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception as exc:
                self._diag['last_error'] = 'diagnostics:'+type(exc).__name__
            await asyncio.sleep(2)

    async def _recover_pending_media(self, page):
        """Enrich only the visible transcript; never navigate the operator's page."""
        pending = self.pending_sync()
        self._diag['pending_attachment_sync'] = len(pending)
        self._diag['attachment_recovery_mode'] = 'visible-read-only'
        if not pending:
            return
        if self._dom_lock is None:
            self._dom_lock = asyncio.Lock()
        if self._dom_lock.locked():
            return
        async with self._dom_lock:
            rows = await asyncio.wait_for(page.evaluate(SNAPSHOT_JS), 4)
            visible_ids = {row.get('id') for row in rows if row.get('id')}
            for item in pending:
                if not visible_ids.intersection(item['ids']):
                    continue
                for value in transcript_rows(rows, anchor={'talk':item['talk'],'ids':item['ids']}):
                    self._journal.deliver(value,self.on_message)

    def status(self):
        with self._lock:
            return dict(self._status)

    def _set_status(self, state, detail):
        with self._lock:
            self._status = {'state': state, 'detail': detail}
        try:
            self.on_status(dict(self._status))
        except Exception:
            pass

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._routes.clear(); self._receivers.clear(); self._seen.clear()
            self._history_seen.clear()
            self._sockets.clear(); self._attached.clear(); self._pending.clear()
            self._last_turn.clear()
            self._last_ts_ms.clear()
            self._cdp_sessions.clear()
            self._pages.clear(); self._sources.clear(); self._dom_pending.clear()
            self._manual_sources.clear();self._native_pending=None
            self._business_identities=self._journal.identities()
            self._dom_lock = None
            self._stop_requested.clear()
            self._set_status('starting', '正在打开独立飞鸽登录窗口')
            self._thread = threading.Thread(target=self._run, daemon=True, name='feige-browser')
            self._thread.start()
        return self.status()

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:
            self._set_status('error', '浏览器启动或连接失败（%s）。请检查 Chrome 安装和是否已有专用窗口。' % type(exc).__name__)
        finally:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()
            self._loop = None
            self._context = None
            self._official = None
            self._business_identities.clear()
            self._routes.clear(); self._sockets.clear()

    async def _main(self):
        from playwright.async_api import async_playwright
        self._shutdown = asyncio.Event()
        self._started_at = time.time()
        profile = getattr(self, 'browser_profile_dir', self.data_dir / 'browser-profile')
        profile.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            args = dict(user_data_dir=str(profile), headless=False,
                        viewport={'width': 1280, 'height': 850})
            # Opt-in, loopback-only diagnostics for an explicitly requested REA session.
            if os.environ.get('FEIGE_REA_CDP') == '1':
                args['args'] = ['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0']
            try:
                self._context = await pw.chromium.launch_persistent_context(channel='chrome', **args)
            except Exception:
                self._context = await pw.chromium.launch_persistent_context(**args)
            ctx = self._context
            from official_services import OfficialServices
            self._official = OfficialServices(ctx)
            asyncio.create_task(self._watch_status())
            if self._stop_requested.is_set():
                await ctx.close()
                self._set_status('disconnected', '专用飞鸽浏览器已关闭')
                return
            await ctx.add_init_script(SOCKET_INIT)
            ctx.on('close', lambda _: self._shutdown.set())
            ctx.on('page', lambda page: asyncio.create_task(self._attach(page)))
            pages = list(ctx.pages)
            page = pages[0] if pages else await ctx.new_page()
            # One profile represents one shop; don't reuse restored extra tabs.
            for other in pages[1:]:
                await other.close()
            await self._attach(page)
            self._set_status('waiting_login', '请在专用窗口登录飞鸽。收到真实消息后显示连接成功；这里只监听启动后的消息。')
            await page.goto('https://im.jinritemai.com/pc_seller_v2/main/workspace', wait_until='domcontentloaded', timeout=60000)
            if self._stop_requested.is_set():
                self._shutdown.set()
            await self._shutdown.wait()
            await ctx.close()
            self._set_status('disconnected', '专用飞鸽浏览器已关闭')

    async def _attach(self, page):
        if id(page) in self._attached:
            return
        self._attached.add(id(page))
        self._pages[id(page)] = page
        def business_request(request):
            pending=self._native_pending
            if pending and pending['page_id']==id(page):
                from native_sender import match_card_request
                if match_card_request(pending,request):
                    if pending.get('http_request') is not None:pending['ambiguous']=True
                    else:pending['http_request']=request
            if self._official and _feige_page(page.url):
                from official_services import PATHS
                parsed=urlparse(request.url)
                if parsed.scheme=='https' and parsed.netloc=='pigeon.jinritemai.com' and parsed.path in tuple(PATHS[n] for n in ('orders','products','aftersales','logistics','product_details')):
                    request._feige_principal=self._official.principal
        page.on('request',business_request)
        def api_response(response):
            pending=self._native_pending
            if pending and pending.get('http_request') is response.request:
                from native_sender import observe_card_result
                task=asyncio.create_task(observe_card_result(self,page,response,pending))
                self._api_tasks.add(task);task.add_done_callback(self._api_tasks.discard)
            if self._official:
                from official_services import PATHS
                parsed = urlparse(response.url)
                if parsed.scheme=='https' and parsed.netloc=='pigeon.jinritemai.com' and parsed.path in tuple(PATHS[n] for n in ('orders','products','aftersales','logistics','product_details')) and len(self._business_tasks)<8:
                    task=asyncio.create_task(self._official.observe(response))
                    self._business_tasks.add(task)
                    task.add_done_callback(self._business_tasks.discard)
            if message_endpoint(response.url) and len(self._api_tasks)<8:
                task=asyncio.create_task(self._receive_message_api(page,response))
                self._api_tasks.add(task)
                task.add_done_callback(self._api_tasks.discard)
        page.on('response', api_response)
        try:
            cdp = await self._context.new_cdp_session(page)
            self._cdp_sessions.append(cdp)
            def created(event):
                self._sockets[(id(page), event['requestId'])] = event['url']
            def closed(event):
                key = (id(page), event['requestId'])
                self._sockets.pop(key, None)
                for talk, route in list(self._routes.items()):
                    if route['socket'] == key:
                        self._routes.pop(talk, None)
            cdp.on('Network.webSocketCreated', created)
            cdp.on('Network.webSocketClosed', closed)
            cdp.on('Network.webSocketFrameReceived', lambda e: self._frame(page, e, False))
            cdp.on('Network.webSocketFrameSent', lambda e: self._frame(page, e, True))
            await cdp.send('Network.enable')
            self._diag['attached_pages'] += 1
        except Exception as exc:
            self._diag['last_error'] = 'attach:'+type(exc).__name__
            self._attached.discard(id(page))

    async def _receive_message_api(self, page, response):
        path=message_endpoint(response.url)
        if not path or response.status!=200 or not _feige_page(page.url):
            return
        try:
            length=response.headers.get('content-length')
            if length and int(length)>MAX_BODY:
                raise ValueError('message_api_size')
            raw=await asyncio.wait_for(response.body(),15)
            messages=extract_api_messages(raw)
            self._diag['api_responses']=self._diag.get('api_responses',0)+1
            self._diag['api_messages']=self._diag.get('api_messages',0)+len(messages)
            self._diag['last_api_path']=path
            # Initial/history batches never auto-reply. Incremental batches may
            # recover messages since this session started, using the same dedup.
            self._accept_messages((id(page),'http-read'),messages,
                historical_only=path!='/pigeon_im/v3/message/get_user_message',enrich=True)
        except Exception as exc:
            self._diag['api_errors']=self._diag.get('api_errors',0)+1
            self._diag['last_api_error']=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('message_api_') else type(exc).__name__

    def _frame(self, page, event, sent):
        self._diag['frames_sent' if sent else 'frames_received'] += 1
        if not _feige_page(page.url):
            self._diag['skipped_origin'] += 1
            return
        key = (id(page), event.get('requestId'))
        url = self._sockets.get(key)
        frame = event.get('response', {})
        payload = frame.get('payloadData', '')
        if frame.get('opcode') != 2 or len(payload) > MAX_FRAME * 1.4:
            self._diag['skipped_opcode'] += 1
            return
        try:
            raw = base64.b64decode(payload, validate=True)
            if len(raw) > MAX_FRAME:
                return
            if sent:
                self._observe_sent(page, key, url, raw)
            else:
                self._observe_received(key, raw)
        except Exception as exc:
            self._diag['parse_errors'] += 1
            self._diag['last_error'] = 'frame:'+type(exc).__name__
            # Never leak raw frames, auth-bearing URLs or customer content in logs.
            return

    def _observe_sent(self, page, key, url, raw):
        talk = ws_sender.sent_talk(raw)
        receiver = ws_sender.frame_receiver_id(raw)
        if not talk or not receiver or ws_sender.frame_text(raw) is None:
            return
        # A captured real outgoing message is authoritative for this socket's
        # conversation, but no cross-conversation cloning is supported.
        self._routes[talk] = {'page': page, 'socket': key, 'url': url,
                              'raw': raw, 'receiver': receiver, 'at': time.monotonic()}
        dec = ws_reader.decode(raw)
        client = ws_sender.get_path(dec, ws_sender.CLIENT_ID_PATH) if dec else None
        cid = client[1][1] if client and isinstance(client[1], tuple) else ''
        pending = self._dom_pending.get(talk)
        native=self._native_pending
        if native and native['page_id']==id(page) and native.get('armed') and cid:
            from native_sender import outgoing_kind,outgoing_shop
            kind=outgoing_kind(raw)
            if kind==native['kind'] and receiver.decode('utf8')==native['buyer'] and outgoing_shop(raw)==native['scope']:
                existing=self._business_identities.get(talk)
                conflict=talk in self._business_identities and existing!={'talk':talk,'buyer':native['buyer'],'scope':native['scope']}
                if conflict or native.get('client_id') and native['client_id']!=cid:
                    native['ambiguous']=True
                else:
                    native.update(client_id=cid,socket=key,talk=talk)
                    self._pending[cid]=native
        if (pending and pending.get('armed') and cid and ws_sender.frame_text(raw) == pending['text']
                and id(page) == pending['page_id']):
            pending['socket'] = key
            pending['client_id'] = cid
            self._pending[cid] = pending
        if cid not in self._pending:
            self._last_turn[talk] = ('agent', cid)

    def _observe_received(self, socket_key, raw):
        self._accept_messages(socket_key, ws_reader.extract_messages(raw))

    def _accept_messages(self, socket_key, messages, historical_only=False, enrich=False):
        for m in messages:
            self._diag['parsed_messages'] += 1
            if m.sender_role not in ('1', '2') or not m.conversation_id or not m.text:
                continue
            talk = m.conversation_id
            buyer_id = m.buyer_id if m.sender_role == '1' else ''
            scope_type = 'shop_id' if m.shop_id else 'pigeon_cid' if m.pigeon_cid else ''
            shop_scope = (scope_type + ':' + (m.shop_id or m.pigeon_cid)) if scope_type else ''
            if buyer_id:
                identity = {'buyer': buyer_id, 'scope': shop_scope, 'talk': talk}
                self._journal.remember_identity(identity)
                previous = self._business_identities.get(talk)
                # Conflicting identities remain blocked for this connection.
                if talk not in self._business_identities or previous == identity:
                    self._business_identities[talk] = identity
                else:
                    self._business_identities[talk] = None
                # Historical messages can supply identity evidence without
                # becoming a new source message or triggering a reply.
                evidence = {'buyer_sha256': hashlib.sha256(buyer_id.encode()).hexdigest(),
                            'scope_sha256': hashlib.sha256(shop_scope.encode()).hexdigest() if shop_scope else '',
                            'buyer_id_type':'security_sender_id', 'shop_scope_type':scope_type}
                with self._lock:
                    mappings = self._diag['identity_mappings']
                    entry = mappings.setdefault(talk, {'observations': [], 'conflict': False})
                    if evidence not in entry['observations']:
                        entry['observations'].append(evidence)
                        entry['conflict'] = len(entry['observations']) > 1
                    if len(mappings) > 2000:
                        mappings.pop(next(iter(mappings)))
            pending = self._pending.get(m.client_msg_id)
            own = bool(pending and m.sender_role == '2' and pending['talk'] == talk
                       and pending['socket'] == socket_key and
                       (pending.get('text') == m.text if 'kind' not in pending else
                        not pending.get('ambiguous') and (not m.shop_id or 'shop_id:'+m.shop_id==pending['scope'])
                        and __import__('native_sender').incoming_kind(m)==pending['kind']))
            if own:
                pending['message_id'] = m.msg_id
                pending['event'].set()
                if 'kind' in pending:
                    buyer_id=pending['buyer'];shop_scope=pending['scope'];scope_type='shop_id'
                    self._business_identities[talk]={'talk':talk,'buyer':buyer_id,'scope':shop_scope}
                    self._journal.remember_identity(self._business_identities[talk])
            msg_id = m.msg_id or m.client_msg_id or hashlib.sha256(
                (talk + str(m.ts_ms) + m.text + m.sender_role).encode()).hexdigest()
            dedup = (talk, msg_id)
            duplicate = dedup in self._seen
            aliases = list(dict.fromkeys(x for x in (msg_id, m.client_msg_id, m.wire_msg_id) if x))
            manual=self._manual_sources.setdefault(talk,{'ids':[],'page_id':socket_key[0]})
            manual['ids']=list(dict.fromkeys(manual['ids']+aliases))[-150:]
            manual['page_id']=socket_key[0]
            new_alias = bool(set(aliases) - self._message_aliases.get(dedup,set()))
            source = self._sources.get(talk)
            if source and source['id'] == msg_id:
                source['ids'] = list(dict.fromkeys(source['ids'] + aliases))
            promoted = (duplicate and dedup in self._history_seen and not historical_only
                        and m.ts_ms/1000 >= self._started_at and m.ts_ms >= self._last_ts_ms.get(talk,0))
            if duplicate and not promoted and not ((enrich and m.media) or new_alias or own):
                continue
            historical = historical_only or (duplicate and not promoted) or not m.ts_ms or m.ts_ms / 1000 < self._started_at
            # Socket reconnects can replay older frames. Confirm our own echo
            # above, but never roll a conversation's decision state backwards.
            if m.ts_ms < self._last_ts_ms.get(talk, 0):
                historical = True
            if not historical:
                self._last_ts_ms[talk] = m.ts_ms
                self._last_turn[talk] = ('customer' if m.sender_role == '1' else 'agent', msg_id)
                if m.sender_role == '1' and m.sender_uid:
                    self._receivers[talk] = m.sender_uid.encode('utf-8')
                if m.sender_role == '1':
                    self._sources[talk] = {'id':msg_id, 'ids':aliases, 'page_id':socket_key[0]}
            self._set_status('connected', '已捕获飞鸽真实消息，可按配置规则回复。')
            data = {'id': msg_id, 'conversation_id': talk, 'name': m.customer_name or talk,
                    'text': m.text, 'role': 'customer' if m.sender_role == '1' else 'agent',
                    'timestamp': m.ts_ms / 1000 if m.ts_ms else time.time(),
                    'msg_type': m.msg_type or 'text', 'historical': historical,
                    'client_message_id': m.client_msg_id, 'own': own,
                    'wire_message_id': m.wire_msg_id,
                    'buyer_id': buyer_id, 'buyer_id_type': ('verified_outbound_receiver' if own and pending and 'kind' in pending else 'security_sender_id') if buyer_id else '',
                    'shop_scope': shop_scope, 'shop_scope_type': scope_type,
                    'product': m.product if m.sender_role == '1' else None}
            data['media'] = m.media
            data['attachment_payload'] = m.attachment_payload
            try:
                self._journal.deliver(data, self.on_message)
                self._schedule_media_resolution(data)
                self._seen.add(dedup)
                self._message_aliases.setdefault(dedup,set()).update(aliases)
                if historical and not duplicate:
                    self._history_seen.add(dedup)
                elif not historical:
                    self._history_seen.discard(dedup)
                if len(self._seen) > 20000:
                    self._seen.clear(); self._seen.add(dedup)
                    self._message_aliases={dedup:set(aliases)}
                    self._history_seen.intersection_update(self._seen)
            except Exception as exc:
                self._diag['last_error'] = 'message_storage:' + type(exc).__name__
                raise

    def _schedule_media_resolution(self, data):
        if not self._context or not self._loop or not self._loop.is_running():
            return
        for item in data.get('media',[]):
            vid=item.get('video_id') if item.get('kind')=='video' and not item.get('url') else None
            key=(data['conversation_id'],data['id'],vid)
            if not vid or key in self._media_pending or len(self._media_tasks)>=8:
                continue
            self._media_pending.add(key)
            async def resolve(data=data,vid=vid,key=key):
                try:
                    for attempt in range(3):
                        try:
                            url=await resolve_video(self._context.request,vid)
                            enriched=dict(data,historical=True,media=[dict(x,url=url) if x.get('video_id')==vid else x for x in data['media']])
                            self._journal.deliver(enriched,self.on_message)
                            self._diag['resolved_videos']=self._diag.get('resolved_videos',0)+1
                            return
                        except Exception:
                            if attempt<2:
                                await asyncio.sleep(3*(attempt+1))
                    self._diag['video_resolution_errors']=self._diag.get('video_resolution_errors',0)+1
                finally:
                    self._media_pending.discard(key)
            task=asyncio.create_task(resolve())
            self._media_tasks.add(task)
            task.add_done_callback(self._media_tasks.discard)

    def send(self, conversation_id, text, expected_source_id=None):
        if (not isinstance(conversation_id, str) or not conversation_id or
                not isinstance(text, str) or not text.strip() or len(text) > 4000):
            return {'status': 'failed', 'detail': '会话或消息无效，消息未发送。'}
        loop = self._loop
        if self._stop_requested.is_set() or not loop or not loop.is_running():
            return {'status': 'failed', 'detail': '浏览器未连接，消息未发送。'}
        future = asyncio.run_coroutine_threadsafe(self._send(conversation_id, text, expected_source_id), loop)
        try:
            return future.result(timeout=CONFIRM_TIMEOUT + 35)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {'status': 'unknown', 'detail': '发送状态未知，请在飞鸽核对，勿直接重发。'}
        except Exception:
            return {'status': 'unknown', 'detail': '发送连接中断，请在飞鸽核对是否送达。'}

    async def _send(self, talk, text, expected_source_id=None):
        if self._dom_lock is None:
            self._dom_lock = asyncio.Lock()
        async with self._dom_lock:
            return await self._send_dom(talk, text, expected_source_id)

    async def _send_dom(self, talk, text, expected_source_id=None):
        source = self._sources.get(talk)
        if not source:
            return {'status':'failed', 'detail':'尚未捕获该会话的真实客户消息，消息未发送。'}
        expected = expected_source_id or source['id']
        def source_matches():
            return (not self._stop_requested.is_set() and
                    self._last_turn.get(talk) == ('customer', expected) and
                    self._sources.get(talk, {}).get('id') == expected)
        if not source_matches():
            return {'status':'failed', 'detail':'客户消息已更新或人工已回复，请重新生成草稿。消息未发送。'}
        page = self._pages.get(source['page_id'])
        if not page or not _feige_page(page.url):
            return {'status':'failed', 'detail':'原飞鸽窗口已关闭或离开工作台，消息未发送。'}
        args = {'talk':talk, 'source_ids':source['ids'], 'text':text, 'token':str(uuid.uuid4())}
        async def evaluate(action, **extra):
            return await asyncio.wait_for(page.evaluate(DOM_JS, {**args, 'action':action, **extra}), timeout=4)
        submitted = False
        pending = None
        try:
            found = await evaluate('locate')
            if found.get('clicked'):
                await asyncio.sleep(.25)
            active = await evaluate('check')
            if not active.get('ok'):
                # Opening a row is read-only with respect to message sending.
                # We scan by index only to reveal the exact source ID, never to
                # choose a recipient by name. Commit cannot run without proof.
                probe = await evaluate('probe')
                for index in range(min(probe.get('row_count', 0), 40)):
                    if not source_matches():
                        break
                    await evaluate('open_index', index=index)
                    await asyncio.sleep(.15)
                    active = await evaluate('check')
                    if active.get('ok'):
                        break
            if not active.get('ok') or not source_matches():
                return {'status':'failed', 'detail':'无法用客户源消息ID核验目标会话，消息未发送。请检查飞鸽会话是否可见。'}
            prepared = await evaluate('prepare')
            if not prepared.get('ok'):
                reason = prepared.get('reason', '')
                detail = '飞鸽存在未发送的人工草稿，请先处理。' if reason == 'existing_manual_draft' else '飞鸽输入框或客户源消息校验失败。'
                return {'status':'failed', 'detail':detail+'消息未发送。'}
            await asyncio.sleep(.1)
            if not source_matches():
                return {'status':'failed', 'detail':'填写期间客户消息已更新或已人工回复，消息未发送。'}
            pending = {'talk':talk, 'text':text, 'page_id':id(page), 'socket':None,
                       'event':asyncio.Event(), 'armed':True}
            self._dom_pending[talk] = pending
            # Any exception from this point may be after click; never retry.
            submitted = True
            result = await evaluate('commit')
            if not result.get('ok'):
                submitted = False
                return {'status':'failed', 'detail':'提交前会话、输入内容或发送按钮已变化，消息未发送。'}
            try:
                await asyncio.wait_for(pending['event'].wait(), timeout=CONFIRM_TIMEOUT)
                return {'status':'confirmed', 'detail':'已通过飞鸽发送并收到服务端回声确认。',
                        'message_id':pending.get('message_id') or pending.get('client_id')}
            except asyncio.TimeoutError:
                return {'status':'unknown', 'detail':'已提交，但尚未捕获匹配的服务端回声，请在飞鸽核对，勿直接重发。'}
        except Exception as exc:
            self._diag['last_error'] = 'dom_send:'+type(exc).__name__
            if submitted:
                return {'status':'unknown', 'detail':'发送过程返回不明确，请在飞鸽核对，勿直接重发。'}
            return {'status':'failed', 'detail':'飞鸽会话定位或填写失败，消息未发送。'}
        finally:
            if pending:
                self._pending.pop(pending.get('client_id'), None)
                self._dom_pending.pop(talk, None)
            if not submitted:
                try:
                    await evaluate('abort')
                except Exception:
                    pass

    def official_query(self, section, body=None, expected=None):
        loop = self._loop
        if not loop or not loop.is_running() or not self._official or self.status()['state'] != 'connected':
            raise ValueError('请先连接飞鸽，再读取官方数据')
        future = asyncio.run_coroutine_threadsafe(self._official_query(section, body or {}, expected), loop)
        try:
            return future.result(timeout=6 if section=='identity' else 27 if section=='catalog' else 65)
        except TimeoutError:
            future.cancel()
            raise ValueError('官方查询超时，请稍后刷新') from None

    async def _official_query(self, section, body, expected):
        if section=='product_details':
            account=await self._official.authenticate();principal=self._official.principal
            scope='shop_id:'+str(account['ShopId'])
            if body.get('scope') and body['scope']!=scope:raise ValueError('商品查询店铺已变化')
            cached=self._official.cache.get('product_details')
            if not body.get('refresh') and cached and cached['scope']==scope and time.time()-cached['fetched_at']<30:return cached
            if self._dom_lock is None:self._dom_lock=asyncio.Lock()
            async with self._dom_lock:
                page=next((p for p in self._context.pages if _feige_page(p.url)),None)
                if not page:raise ValueError('官方飞鸽页面不可用')
                since=time.time()
                # Explicit refresh uses the normal page UI, never replaying a denied request.
                await page.get_by_text('订单',exact=True).click(timeout=4000)
                await page.get_by_text('商品',exact=True).click(timeout=4000)
                for _ in range(40):
                    await asyncio.sleep(.25)
                    cached=self._official.cache.get('product_details')
                    if cached and cached['scope']==scope and cached['fetched_at']>=since:
                        await self._official.verify_unchanged(principal)
                        return cached
                raise ValueError('未收到完整的官方商品响应，请在飞鸽检查商品页权限或验证提示')
        if section == 'catalog':
            result=await asyncio.wait_for(self._official.catalog(body),25)
            details=self._official.cache.get('product_details')
            if details and details['scope']==result['scope'] and time.time()-details['fetched_at']<30:
                result['details']=details
            return result
        if section == 'identity':
            account = await self._official.authenticate()
            if body.get('scope') != 'shop_id:' + str(account.get('ShopId','')):
                raise ValueError('店铺身份已变化，不能沿用其他店铺资料')
            return {'shop_name':account.get('ShopName',''), 'scope':body['scope']}
        if section == 'overview':return await self._official.overview()
        if section == 'replies':return await self._official.quick_replies(body)
        if section in ('customer','coupons'):
            expected = expected or {}
            identity = self._business_identities.get(expected.get('talk'))
            if not identity or identity['scope'] != expected.get('scope') or hashlib.sha256(identity['buyer'].encode()).hexdigest() != expected.get('buyer_hash'):
                raise ValueError('当前客户身份尚未核实，不能读取订单')
            if section=='coupons':return await self._official.coupons(body,dict(identity))
            if body.get('action','orders') in ('orders','aftersales') and body.get('page',0)==0:
                await self._official.authenticate()
                if 'shop_id:'+self._official.shop_id!=identity['scope']:raise ValueError('店铺已变化')
                if self._dom_lock is None:self._dom_lock=asyncio.Lock()
                async with self._dom_lock:
                    anchor=self._manual_sources.get(identity['talk'])
                    page=self._pages.get(anchor['page_id']) if anchor else None
                    if not page:raise ValueError('请在飞鸽选中此客户以读取订单')
                    proof={'talk':identity['talk'],'source_ids':anchor['ids'],'manual':True}
                    if not (await page.evaluate(DOM_JS,{**proof,'action':'check'})).get('ok'):
                        await page.evaluate(DOM_JS,{**proof,'action':'locate'});await asyncio.sleep(.25)
                    if not (await page.evaluate(DOM_JS,{**proof,'action':'check'})).get('ok'):raise ValueError('当前官方会话不符，未读取订单')
                    since=time.time();principal=self._official.principal
                    await page.get_by_text('订单',exact=True).click(timeout=4000)
                    await page.get_by_text('售后中',exact=True).click(timeout=4000)
                    await page.get_by_text('全部',exact=True).click(timeout=4000)
                    for _ in range(40):
                        cached=self._official.observed.get((principal,identity['buyer'],'orders',0))
                        if cached and cached['at']>=since:break
                        await asyncio.sleep(.25)
                    else:raise ValueError('未收到该客户官方订单响应，请在飞鸽检查验证或权限提示')
            return await self._official.customer(body, dict(identity))
        raise ValueError('不支持的官方查询')

    def stop(self):
        self._stop_requested.set()
        loop = self._loop
        if loop and loop.is_running() and self._shutdown:
            loop.call_soon_threadsafe(self._shutdown.set)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=8)
        return self.status()
