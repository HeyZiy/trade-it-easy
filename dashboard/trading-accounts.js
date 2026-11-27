import { esc, finite, accountRequest } from './common.js';

import { money, fundShares, shanghaiToday } from './dashboard-format.js';

function custodyName(account,id) {
  return account.trading_accounts?.accounts[id]?.name || '待归属';
}

function custodySelect(account, select, {side,kind,code,strategy,edit}) {
  const settings=account.trading_accounts;
  const visible=!!settings && !!side && side!=='cash_transfer';
  select.parentElement.hidden=!visible;
  select.disabled=!visible;
  if (!visible) return null;
  const holdings=account.custody_positions.filter(row=>row.kind===kind && row.code===code && row.strategy===strategy);
  const outgoing=['sell','fund_redeem','assign'].includes(side);
  const cashOperation=['deposit','withdraw','dividend'].includes(side);
  const signature=JSON.stringify([side,kind,code,strategy]);
  if (select.dataset.context!==signature) {
    const old=edit && (edit.trading_account || 'unlocated');
    const ids=outgoing ? holdings.map(row=>row.account) : Object.keys(settings.accounts)
      .filter(id=>cashOperation || settings.accounts[id].channel===kind);
    if (old==='unlocated' && !ids.includes(old)) ids.push(old);
    const opts=ids.map(id=>new Option(custodyName(account,id),id));
    select.replaceChildren(...opts);
    const preferred=old || (!outgoing && !cashOperation && (settings.products[`${kind}:${code}`] || settings.defaults[kind]));
    if (preferred && ids.includes(preferred)) select.value=preferred;
    else if (ids.length!==1) {
      select.prepend(new Option('请选择交易账户',''));
      select.value='';
    }
    select.dataset.context=signature;
  }
  select.disabled=side==='fund_confirm';
  return {id:select.value,qty:holdings.find(row=>row.account===select.value)?.qty || 0,
    cash:account.custody_cash[select.value] || 0};
}

function custodyProduct(account,kind,code) {
  if (!account.trading_accounts) return '';
  const quantities=new Map();
  for(const row of account.custody_positions) {
    if(row.kind===kind && row.code===code) quantities.set(row.account,(quantities.get(row.account) || 0)+row.qty);
  }
  return [...quantities].map(([id,qty])=>`<small>${esc(custodyName(account,id))} · ${fundShares(qty)} 份</small>`).join('');
}

function custodySection(account, {strategyName, fundDisplayName}) {
  if (!account.trading_accounts) return '';
  const pending=Object.values(account.fund_pending || {});
  const ids=Object.keys(account.trading_accounts.accounts);
  if (account.custody_cash.unlocated || account.custody_positions.some(r=>r.account==='unlocated') ||
      pending.some(r=>!r.trading_account || r.trading_account==='unlocated')) ids.unshift('unlocated');
  const options=account.trading_accounts.accounts;
  const groups=ids.map(id=>{
    const positions=account.custody_positions.filter(r=>r.account===id);
    const cash=account.custody_cash[id] || 0;
    const orders=pending.filter(r=>(r.trading_account || 'unlocated')===id);
    const inTransit=orders.reduce((sum,r)=>sum+r.amount,0);
    const valued=positions.every(r=>finite(r.market_value));
    const assets=valued ? money(cash+inTransit+positions.reduce((sum,r)=>sum+r.market_value,0))+' 元' : '待估值';
    const action=(kind,index)=>Object.entries(options).some(([other,row])=>other!==id && (kind==='cash' || row.channel===kind))
      ? `<button type="button" class="text-button" data-custody-account="${esc(id)}" data-custody-row="${index}">${id==='unlocated'?'分配':'划转'}</button>` : '';
    const adjust=id!=='unlocated' || cash ? `<button type="button" class="text-button" data-cash-adjust="${esc(id)}">校准</button>` : '';
    const rows=positions.map(row=>{
      const name=row.kind==='fund' ? fundDisplayName(row.code) : account.positions.find(p=>p.code===row.code)?.name;
      return `<tr><td>${esc(name || row.code)}<small>${esc(row.code)} · ${esc(strategyName(row.strategy))}</small></td><td>${fundShares(row.qty)}</td><td>${money(row.market_value)}</td><td>${action(row.kind,account.custody_positions.indexOf(row))}</td></tr>`;
    }).join('');
    return `<details class="custody-group"${id==='unlocated'?' open':''}><summary>${esc(custodyName(account,id))} · ${assets}</summary>
      <p>现金 ${money(cash)} 元 ${cash>0?action('cash',-1):''} ${adjust}${orders.length?` · 在途申购 ${money(inTransit)} 元`:''}</p>
      ${rows?`<div class="table-wrap"><table><thead><tr><th>持仓</th><th>份额</th><th>市值 / 元</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`:''}</details>`;
  }).join('');
  return `<section class="section"><div class="section-heading"><h2>按交易账户查看</h2><a href="/settings/accounts">账户设置</a></div>${groups}</section>`;
}

export { custodyName, custodySelect, custodyProduct, custodySection };

export function createCustodyDialogs({strategyName, reloadAccount, showEntryReceipt}) {
  function openCashAdjust(account,id,edit=null) {
    const dialog=document.querySelector('#adjust-dialog');
    const form=dialog.querySelector('form');
    const field=name=>form.elements.namedItem(name);
    const error=form.querySelector('[role=alert]');
    const preview=document.querySelector('#adjust-difference');
    const recorded=account.custody_cash[id] || 0;
    form.reset();
    error.hidden=true;
    if(edit) field('actual').value=(recorded+edit.amount).toFixed(2);
    document.querySelector('#adjust-source').textContent=`${custodyName(account,id)} · 账面现金 ${money(recorded)} 元`;
    Object.assign(field('date'),{min:account.min_date || account.trades[0]?.date || account.start_date,
      max:account.max_date || shanghaiToday(),value:edit?.date || shanghaiToday()});
    const update=()=>{
      const actual=Number(field('actual').value);
      const difference=Math.round((actual-recorded)*100)/100;
      preview.hidden=!(field('actual').value && finite(actual));
      preview.textContent=!finite(actual) ? '' : difference===0 ? '与账面一致，无需校准。'
        : `账面将${difference>0?'增加':'减少'} ${money(Math.abs(difference))} 元，按各策略现金占比分摊，计入收益而非出入金。`;
      preview.classList.toggle('invalid',finite(actual) && difference<0 && Math.round((recorded+difference)*100)<0);
      field('amount').value=finite(actual) ? String(difference) : '';
      field('actual').setCustomValidity(!field('actual').value || !finite(actual) ? '' : difference===0 ? '与账面一致，无需校准。'
        : Math.round((recorded+difference)*100)<0 ? '差额超过所选交易账户现金。' : '');
    };
    const submit=form.querySelector('[type=submit]');
    const fields=form.querySelector('fieldset');
    fields.disabled=submit.disabled=false;
    let requestId=crypto.randomUUID(), saved=false;
    form.oninput=()=>{requestId=crypto.randomUUID();error.hidden=true;update();};
    field('cancel').onclick=()=>{if(!submit.disabled)dialog.close();};
    dialog.oncancel=e=>{if(submit.disabled)e.preventDefault();};
    form.onsubmit=async e=>{
      e.preventDefault();
      if(submit.disabled) return;
      const payload={side:'cash_adjust',trading_account:id,amount:Number(field('amount').value),
        date:field('date').value,request_id:requestId,revision:account.revision};
      if(edit) payload.event_id=edit.id;
      fields.disabled=submit.disabled=true;
      try {
        if(!saved) {await accountRequest(edit?'/api/etf/event/edit':'/api/etf/event',payload);saved=true;}
        await reloadAccount();
        dialog.close();
        showEntryReceipt(edit?.id || requestId, !!edit);
      } catch(failure) {error.textContent=saved?'校准已保存，请刷新账户。':failure.message;error.hidden=false;}
      finally {fields.disabled=saved;submit.disabled=false;}
    };
    update();
    dialog.showModal();
  }

  function openCustodyMove(account,id,row,edit=null) {
    const dialog=document.querySelector('#custody-dialog');
    const form=dialog.querySelector('form');
    const field=name=>form.elements.namedItem(name);
    const error=form.querySelector('[role=alert]');
    const cash=!row;
    const max=cash ? account.custody_cash[id] : row.qty;
    const step=cash ? '0.01' : row.kind==='fund' ? '0.000001' : '1';
    const target=field('target');
    form.reset();
    error.hidden=true;
    target.replaceChildren(...Object.entries(account.trading_accounts.accounts)
      .filter(([key,r])=>key!==id && (cash || r.channel===row.kind)).map(([key,r])=>new Option(r.name,key)));
    if(edit) target.value=edit.to_account;
    document.querySelector('#custody-source').textContent=custodyName(account,id)+' · '+(cash ? '可划转现金 '+money(max)+' 元' : row.code+' · '+strategyName(row.strategy)+' · 可划转 '+fundShares(max)+' 份');
    document.querySelector('#custody-unit').textContent=cash ? '划转金额 / 元' : '划转份额';
    Object.assign(field('quantity'),{min:step,step,max:String(max),value:String(edit?.qty ?? edit?.amount ?? max)});
    Object.assign(field('date'),{min:account.min_date || account.trades[0]?.date || account.start_date,
      max:account.max_date || shanghaiToday(),value:edit?.date || shanghaiToday()});
    const submit=form.querySelector('[type=submit]');
    const fields=form.querySelector('fieldset');
    fields.disabled=submit.disabled=false;
    let requestId=crypto.randomUUID(), saved=false;
    form.oninput=()=>{requestId=crypto.randomUUID();error.hidden=true;};
    field('cancel').onclick=()=>{if(!submit.disabled)dialog.close();};
    dialog.oncancel=e=>{if(submit.disabled)e.preventDefault();};
    form.onsubmit=async e=>{
      e.preventDefault();
      if(submit.disabled) return;
      const payload={side:'custody_transfer',kind:row?.kind || 'cash',trading_account:id,to_account:target.value,
        date:field('date').value,request_id:requestId,revision:account.revision};
      if(row) Object.assign(payload,{strategy:row.strategy,code:row.code,qty:Number(field('quantity').value)});
      else payload.amount=Number(field('quantity').value);
      if(edit) payload.event_id=edit.id;
      fields.disabled=submit.disabled=true;
      try {
        if(!saved) {await accountRequest(edit?'/api/etf/event/edit':'/api/etf/event',payload);saved=true;}
        await reloadAccount();
        dialog.close();
      } catch(failure) {error.textContent=saved?'划转已保存，请刷新账户。':failure.message;error.hidden=false;}
      finally {fields.disabled=saved;submit.disabled=false;}
    };
    dialog.showModal();
  }

  return { openCashAdjust, openCustodyMove };
}
