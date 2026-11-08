/* Persistent operator unread state, incoming alerts and explicitly taught experience. */
(() => {
  const activityNames={waiting:'已收到，正在接待',generating:'正在理解并准备回复',ready:'回复已准备',sending:'正在发送',replied:'已回复',failed:'回复未完成，请处理',unknown:'发送结果待核实'};
  const bar=el('div',undefined,'banner');bar.style.flexShrink='0';bar.id='inboxReminder';
  const total=el('strong','暂无未读来信'),sound=el('button','声音提醒：开');sound.id='soundToggle';sound.style.cssText='font-size:11px;padding:3px 5px;margin-top:5px';
  bar.append(total,el('br'),sound);$('conversationList').before(bar);
  sound.onclick=()=>run(sound,async()=>{await api('/api/settings',{notify_sound:!state.settings.notify_sound});await refresh()});
  const alert=el('aside');alert.id='inboundAlert';alert.hidden=true;alert.setAttribute('role','status');alert.setAttribute('aria-live','polite');
  alert.style.cssText='position:fixed;right:20px;top:90px;z-index:30;width:310px;padding:14px;background:#fff9df;border:1px solid #d9ae45;border-radius:10px;box-shadow:0 6px 24px #0002';
  const caption=el('strong'),preview=el('p'),view=el('button','查看来信'),dismiss=el('button','稍后看');preview.style.cssText='margin:8px 0;overflow-wrap:anywhere';alert.append(caption,preview,view,dismiss);document.body.append(alert);
  let alertCid=null,lastMode=null;const known=new Map(),read=new Map();let readBusy=false;
  dismiss.onclick=()=>{alert.hidden=true};view.onclick=async()=>{if(!alertCid)return;await selectConversation(alertCid);$('messages').scrollTop=$('messages').scrollHeight;await window.receptionRead(current);alert.hidden=true};
  window.receptionUpdate=data=>{
    if(lastMode!==data.mode){known.clear();read.clear();alert.hidden=true;lastMode=data.mode}
    let count=0,candidate=null;for(const c of data.conversations||[]){count+=c.unread||0;const seq=c.latest_event?.seq||0,previous=known.get(c.id);known.set(c.id,seq);
      if(seq&&(previous!==undefined?seq>previous:c.unread>0)&&(!candidate||seq>candidate.latest_event.seq))candidate=c;
    }
    if(candidate){alertCid=candidate.id;caption.textContent=(candidate.name||'客户')+' 发来新消息';preview.textContent=candidate.latest_event.preview;alert.hidden=false;}
    total.textContent=count?count+' 条未读来信（查看后清除）':'暂无未读来信';document.title=(count?'('+count+'条未读) ':'')+'飞鸽工作台';sound.textContent='声音提醒：'+(data.settings.notify_sound?'开':'关');
  };
  const status=el('p','', 'busy');status.id='receptionProgress';$('chatSubtitle').after(status);
  window.receptionRead=async c=>{
    if(!c||selected!==c.id)return;status.textContent=activityNames[c.activity]||'';
    const box=$('messages');if(readBusy||document.visibilityState!=='visible'||!document.hasFocus()||box.scrollHeight-box.scrollTop-box.clientHeight>100)return;
    const through=c.read_through||0;if(through<=(read.get(c.id)||0))return;readBusy=true;
    try{await api('/api/read',{conversation_id:c.id,through});read.set(c.id,through)}catch{}finally{readBusy=false}
  };
  $('messages').addEventListener('scroll',()=>window.receptionRead(current));window.addEventListener('focus',()=>window.receptionRead(current));
  const teach=el('button','教它这样回');teach.id='teachBtn';teach.style.fontSize='12px';$('generateBtn').before(teach);
  const dialog=el('dialog');dialog.id='lessonDialog';dialog.style.cssText='width:min(560px,90vw);max-height:85vh;overflow:auto;border:1px solid #cad9cd;border-radius:12px;padding:22px';
  const title=el('h2','店铺经验'),hint=el('p','把你确认过的正确说法记下来，后续类似问题会参考。保存不会发送给客户。','intro');
  const form=el('form'),q=el('textarea'),a=el('textarea'),scope=el('select'),list=el('div');q.id='lessonQuestion';a.id='lessonAnswer';scope.id='lessonScope';q.maxLength=1000;a.maxLength=2000;q.required=a.required=true;
  q.rows=2;a.rows=3;for(const [v,t] of [['product','只用于当前商品'],['shop','全店通用说法']]){const o=el('option',t);o.value=v;scope.append(o)}
  for(const [label,input] of [['适用问题',q],['正确回复',a],['适用范围',scope]]){const l=el('label',undefined,'field');l.append(el('span',label),input);form.append(l)}
  const save=el('button','记住这次纠正','primary');save.type='submit';const close=el('button','关闭');close.type='button';close.onclick=()=>dialog.close();form.append(save,close);dialog.append(title,hint,form,el('h3','当前店铺已记住的经验'),list);document.body.append(dialog);
  let lessonCid=null;
  function renderLessons(items){list.replaceChildren();for(const item of items||[]){const card=el('article',undefined,'kcard');card.append(el('h3',item.question),el('p',item.answer),el('small',item.product_id?'商品 '+item.product_id:'全店通用'));const remove=el('button','删除');remove.onclick=()=>run(remove,async()=>{if(!confirm('删除这条店铺经验？'))return;const r=await api('/api/lesson',{action:'delete',id:item.id,conversation_id:lessonCid});renderLessons(r.lessons)});card.append(remove);list.append(card)}}
  teach.onclick=()=>{if(!current||modeDemo()){notify('请先选择真实客户会话',true);return}lessonCid=current.id;const question=[...current.messages].reverse().find(m=>m.role==='customer');q.value=question?.text||'客户询问图片中的商品';a.value=$('reply').value.trim()||[...current.messages].reverse().find(m=>m.role==='agent')?.text||'';scope.value=current.product_context?'product':'shop';scope.options[0].disabled=!current.product_context;renderLessons(current.lessons);dialog.showModal();a.focus()};
  form.onsubmit=e=>{e.preventDefault();run(save,async()=>{if(lessonCid!==selected)throw Error('会话已变化，请重新打开经验表单');const r=await api('/api/lesson',{conversation_id:lessonCid,question:q.value,answer:a.value,product_only:scope.value==='product'});renderLessons(r.lessons);await fetchConversation();notify('已记住，下次遇到适用的问题会参考；没有发送消息。')})};
})();
