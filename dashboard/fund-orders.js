import { esc, accountRequest } from './common.js';
import { money, fundShares, dcaSourceLabel } from './dashboard-format.js';
import { custodyName } from './trading-accounts.js';

export function createFundOrders({currentAccount, fundDisplayName, openFundTrade, openFundVoid}) {
  const filters = {code:'', status:'active', sort:'date', ascending:false};
  let selectedId;
  const statuses = {active:'有效申购', deleted:'已删除', voided:'已撤销登记', all:'全部'};
  const day = order => order.row.nav_date || order.row.date;
  const state = order => order.deleted ? 'deleted' : order.voided ? 'voided' : 'active';

  function ordersFor(account) {
    const orders = new Map();
    function collect(events, deleted) {
      for (const row of events) {
        if (['fund_buy','fund_pending'].includes(row.side)) orders.set(row.id, {row, events:[row], deleted});
      }
      for (const event of events) {
        const id = event.side==='fund_confirm' ? event.pending_id : event.side==='fund_void' ? event.voided_pending_id : event.kind==='pending' ? event.order_id : null;
        const order = orders.get(id);
        if (!order) continue;
        order.events.push(event);
        if (event.side==='fund_confirm') order.confirmation=event;
        if (event.side==='fund_void') order.voided=true;
      }
    }
    collect(account.trades || []);
    for (const deletion of account.deleted_subscriptions || []) collect(deletion.events, deletion.date);
    return orders;
  }

  function render() {
    const account = currentAccount();
    const orders = ordersFor(account);
    if (filters.status==='active' && orders.get(selectedId)?.deleted) filters.status='all';
    const content = document.querySelector('#fund-orders-content');
    content.innerHTML = `<div class="allocation-actions">
      <label>基金代码 <input type="search" id="fund-orders-code" value="${esc(filters.code)}"></label>
      <label>记录范围 <select id="fund-orders-status">${Object.entries(statuses).map(([key,label])=>`<option value="${key}" ${filters.status===key ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
      </div><div id="fund-orders-results" class="table-wrap"></div><p class="warning" role="alert" hidden></p>`;
    const results = content.querySelector('#fund-orders-results');
    const error = content.querySelector('[role=alert]');
    function update() {
      const rows = [...orders.values()].filter(order => (filters.status==='all' || state(order)===filters.status) && order.row.code.includes(filters.code));
      const direction = filters.ascending ? 1 : -1;
      rows.sort((a,b) => direction * (filters.sort==='amount' ? a.row.amount-b.row.amount : day(a).localeCompare(day(b))));
      const heading = (key,label) => `<th ${filters.sort===key ? `aria-sort="${filters.ascending?'ascending':'descending'}"` : ''}><button type="button" class="text-button" data-order-sort="${key}">${label}${filters.sort===key ? filters.ascending?' ↑':' ↓' : ''}</button></th>`;
      results.innerHTML = rows.length ? `<table><thead><tr>${heading('date','申购日期')}<th>基金 / 平台</th>${heading('amount','扣款 / 元')}<th>确认份额</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows.map(order => {
        const {row, confirmation, deleted, events} = order;
        const active = state(order)==='active';
        const target = confirmation || account.fund_pending?.[row.id] || row;
        const status = active ? (confirmation || row.side==='fund_buy' ? '已确认' : '待确认份额') : statuses[state(order)];
        const source = dcaSourceLabel(account.dca?.sources?.[row.id]);
        const platform = account.trading_accounts ? custodyName(account,target.trading_account) : '';
        const detail = [...events].sort((a,b)=>a.date.localeCompare(b.date)).map(event => {
          const label = event.kind==='pending' ? '在途归属划转' : event.side==='fund_confirm' ? '确认份额' : event.side==='fund_void' ? '撤销登记' : '登记申购';
          return `<li>${esc(event.date)} · ${label}${event.nav == null ? '' : ` · 单位净值 ${esc(event.nav)}`}</li>`;
        }).join('');
        return `<tr><td>${esc(day(order))}</td><td>${esc(fundDisplayName(row.code) || row.code)}<small>${esc([row.code,platform].filter(Boolean).join(' · '))}</small></td><td class="num">${money(row.amount)}</td><td class="num">${fundShares(confirmation?.qty ?? row.qty)}</td><td>${status}</td><td>${active ? `<button type="button" class="text-button" data-order-edit="${esc(row.id)}">${confirmation?'更正份额':'修改申购'}</button> <button type="button" class="text-button fund-void-action" data-order-delete="${esc(row.id)}">删除申购</button>` : ''}<details ${selectedId===row.id?'open':''}><summary>流水详情</summary><ul>${source?`<li>${esc(source)}</li>`:''}${detail}${deleted?`<li>${esc(deleted)} · 删除申购</li>`:''}</ul></details></td></tr>`;
      }).join('')}</tbody></table>` : '<p class="empty">暂无符合条件的申购</p>';
    }
    content.querySelector('#fund-orders-code').oninput = event => {filters.code=event.target.value.trim();update();};
    content.querySelector('#fund-orders-status').onchange = event => {filters.status=event.target.value;update();};
    results.onclick = async event => {
      const sort=event.target.closest('[data-order-sort]');
      if (sort) {
        const key=sort.dataset.orderSort;
        filters.ascending=filters.sort===key && !filters.ascending;
        filters.sort=key;
        update();
        results.querySelector(`[data-order-sort="${key}"]`).focus();
        return;
      }
      const edit=event.target.closest('[data-order-edit]');
      const remove=event.target.closest('[data-order-delete]');
      const button=edit || remove;
      if (!button) return;
      selectedId=edit ? edit.dataset.orderEdit : remove.dataset.orderDelete;
      const order=orders.get(selectedId);
      const target=order.confirmation || order.row;
      button.disabled=true;
      error.hidden=true;
      try {
        if (edit) {
          const context=await accountRequest('/api/etf/event/context',{revision:account.revision,event_id:target.id});
          openFundTrade({...account,...context},context.event.side,context.event.code,context.event.strategy || 'core',context.event);
        } else openFundVoid(account,account,target,'delete-subscription');
      } catch (failure) {
        error.textContent=failure.message;
        error.hidden=false;
      } finally {button.disabled=false;}
    };
    update();
  }

  function open(orderId) {
    selectedId=orderId;
    Object.assign(filters,{code:'',status:orderId?'all':'active',sort:'date',ascending:false});
    const dialog=document.querySelector('#fund-orders-dialog');
    document.querySelector('#fund-orders-close').onclick=()=>dialog.close();
    render();
    if (!dialog.open) dialog.showModal();
    if (orderId) dialog.querySelector('details[open]')?.scrollIntoView({block:'nearest'});
  }

  function refresh() {
    if (!document.querySelector('#fund-orders-dialog').open) return;
    render();
  }

  return {open, refresh};
}
