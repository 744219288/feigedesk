/* Explicit manual actions; no automation trigger and no arbitrary local-file path. */
(()=>{
 const open=el('button','素材与商品');open.id='chainsBtn';$('modeBtn').before(open);
 const dialog=el('dialog',undefined,'official-dialog');dialog.setAttribute('aria-label','素材发送与商品知识');
 const head=el('div',undefined,'official-head'),heading=el('h2','素材与商品'),close=el('button','关闭');head.style.cssText='display:flex;justify-content:space-between;align-items:center';head.append(heading,close);
 const body=el('div',undefined,'official-body'),status=el('p','','official-note');status.setAttribute('role','status');
 dialog.append(head,status,body);document.body.append(dialog);close.onclick=()=>dialog.close();let busy=false;
 const states={confirmed:'回声已确认',accepted:'官方已接受',unknown:'结果待核对',failed:'未发送成功',sending:'发送中'};
 const kinds={image:'图片',video:'视频',file:'PDF 文件',coupon:'优惠券卡片'};
 function report(value){
   const wrap=el('section',undefined,'official-card');body.append(wrap);
   if(value.note)wrap.append(el('p',value.note,'official-note'));
   const items=Array.isArray(value)?value:Array.isArray(value.items)?value.items:[value];
   if(!items.length)wrap.append(el('p','本次查询没有记录。'));
   for(const x of items){
     if(x.skus){
       wrap.append(el('h3',x.title),el('p','商品编号：'+x.id));
       wrap.append(el('p',x.properties.map(p=>p.name+'：'+p.values.join('、')).join('；')));
       for(const sku of x.skus)wrap.append(el('p',sku.specs.map(s=>s.name+'：'+s.value).join('；')+'\n展示价：'+(sku.display_price?sku.price_prefix+sku.display_price:'未返回')+'；库存：'+(sku.stock??'未返回')));
       wrap.append(el('p',(x.shipping_description||'')+'\n'+x.knowledge_note,'official-note'));
     }else if(x.kind&&x.status){
       wrap.append(el('h3',(kinds[x.kind]||x.kind)+' · '+(states[x.status]||x.status)),el('p',x.detail||''));
       if(x.created_at)wrap.append(el('p',new Date(x.created_at).toLocaleString(),'official-note'));
       if(x.message_id)wrap.append(el('p','消息编号：'+x.message_id,'official-note'));
     }else if(x.after_sale_id){
       wrap.append(el('h3','售后 '+x.after_sale_id),el('p',[x.after_sale_status_desc,x.status_desc,x.after_sale_type_desc,x.refund_amount_desc,x.reason].filter(Boolean).join('；')));
     }else if(x.precheck){
       wrap.append(el('h3','预检查结果'),el('p',Object.values(x.precheck).join('；')||'官方未返回可展示的提示。请到飞鸽核对。'));
     }else if(x.content||x.description){wrap.append(el('p',x.content||x.description))}
   }
   if(value.fetched_at)wrap.append(el('p','读取时间：'+new Date(value.fetched_at*1000).toLocaleString(),'official-note'));
   if(value.complete===false||value.has_more)wrap.append(el('p','本次结果不是全部记录，请在官方页面继续核对。','official-note'));
 }
 const button=(name,action)=>{const b=el('button',name);b.onclick=()=>run(b,action);return b};
 async function draw(){
   body.replaceChildren();const cid=selected,name=current?.name||'未选择客户';status.textContent='当前客户：'+name+'。发送素材会接管此会话；价格库存仅代表读取时状态。';
   const sync=button('读取商品详情并同步知识',async()=>{busy=true;try{const result=await api('/api/products/sync',{});status.textContent='已同步 '+result.saved+' 件商品。'+result.note;report(result)}finally{busy=false}});body.append(sync);
   if(!cid){body.append(el('p','选择客户后可发送素材、查询优惠券和售后。'));return}
   const input=el('input');input.type='file';input.accept='.png,.jpg,.jpeg,.gif,.mp4,.pdf';input.setAttribute('aria-label','选择发送素材');
   const send=button('发送所选素材到 '+name,async()=>{
     if(cid!==selected)throw Error('客户已切换，请重新打开面板');const file=input.files?.[0];if(!file)throw Error('请先选择素材');
     if(file.size>16*1024*1024)throw Error('素材最大16 MiB，图片最大10 MiB');
     busy=true;try{
       status.textContent='正在上传本地素材…';
       const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=reject;r.readAsDataURL(file)});
       const asset=await api('/api/assets/upload',{name:file.name,data});
       if(cid!==selected)throw Error('客户已切换，素材已保存但没有发送');
       const request_id=crypto.randomUUID().replaceAll('-','');input.value='';
       status.textContent='正在通过官方飞鸽发送到 '+name+'，等待回声确认…';
       const result=await api('/api/send_asset',{conversation_id:cid,kind:asset.kind,asset_id:asset.id,request_id});
       status.textContent=result.detail;report(result);await refresh();
     }finally{busy=false}
   });body.append(el('p','支持 PNG/JPG/GIF、MP4、PDF。官方不支持的格式不会伪装成已发送。'),input,send);
   body.append(button('查询现有优惠券',async()=>{
     if(cid!==selected)throw Error('客户已变化');const result=await api('/api/official',{section:'coupons',conversation_id:cid});
     if(cid!==selected)throw Error('客户已变化');if(!result.items.length)body.append(el('p','当前没有可查询到的券'));
     for(const coupon of result.items){const c=el('section',undefined,'official-card');c.append(el('h3',coupon.name),el('p',`满 ${coupon.threshold_fen/100} 元减 ${coupon.credit_fen/100} 元；${coupon.note}`));
       const b=button('发送此券卡片到 '+name,async()=>{if(cid!==selected)throw Error('客户已变化');busy=true;b.disabled=true;try{const r=await api('/api/send_asset',{conversation_id:cid,kind:'coupon',coupon_id:coupon.id,request_id:crypto.randomUUID().replaceAll('-','')});status.textContent=r.detail;report(r)}finally{busy=false}});b.disabled=!result.can_send;c.append(b);body.append(c)}
   }));
   body.append(button('读取客户售后',async()=>{
     if(cid!==selected)throw Error('客户已变化');
     const r=await api('/api/official',{section:'customer',action:'aftersales',conversation_id:cid});report(r);
     for(const item of r.items){
       const c=el('section',undefined,'official-card');c.append(el('h3','售后 '+item.after_sale_id));
       c.append(button('查询售后日志',async()=>{if(cid!==selected)throw Error('客户已变化');report(await api('/api/official',{section:'customer',action:'aftersale_logs',conversation_id:cid,after_sale_id:item.after_sale_id}))}));
       for(const a of item.precheck_actions||[])c.append(button('仅预检查：'+a.name,async()=>{if(cid!==selected)throw Error('客户已变化');report(await api('/api/official',{section:'customer',action:'refund_precheck',conversation_id:cid,after_sale_id:item.after_sale_id,action_key:a.key}))}));
       body.append(c);
     }
   }));
   body.append(button('查看素材发送记录',async()=>{const r=await fetch('/api/outbound_jobs?conversation_id='+encodeURIComponent(cid));const data=await r.json();if(!r.ok)throw Error(data.error||'读取失败');report(data)}));
 }
 open.onclick=()=>{dialog.showModal();if(!busy)draw()};
})();
