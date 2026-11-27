import { esc, finite, accountRequest } from './common.js';

import { number, money, isFundTrade, ASSET_NAMES, shanghaiToday, valuationLabel } from './dashboard-format.js';

export function createAccountActions({
  currentAccount, currentStrategies, fundDisplayName, reloadAccount, showEntryReceipt,
  openEntry, openCashAdjust, openCustodyMove, openFundTrade, openFundVoid, openDcaPlans,
  bindRecords, bindReconciliation,
}) {
  function closeActionMenus() {
    for (const list of document.querySelectorAll('.action-menu-list:not([hidden])')) {
      list.hidden = true;
      list.previousElementSibling.setAttribute('aria-expanded', 'false');
    }
  }

  document.addEventListener('click', event => {
    if (!event.target.closest('.action-menu')) closeActionMenus();
  });

  function bindActionMenus() {
    for (const toggle of document.querySelectorAll('.action-menu-toggle')) {
      const list = toggle.nextElementSibling;
      toggle.onclick = () => {
        const open = list.hidden;
        closeActionMenus();
        if (open) {
          list.hidden = false;
          toggle.setAttribute('aria-expanded', 'true');
        }
      };
      list.onclick = event => {
        if (event.target.closest('.action-menu-item')) closeActionMenus();
      };
    }
  }

  async function withGuard(button, request) {
    button.disabled = true;
    try {
      await request();
      await reloadAccount();
    } catch (error) {
      document.querySelector('#status').textContent = error.message;
      button.disabled = false;
    }
  }

  function bindAccountActions(view) {
    const account = currentAccount();
    if (!account?.can_record || (view.id!=='etf' && view.account_id!=='etf')) return;
    if (view.id === 'etf') bindReconciliation(account);
    bindActionMenus();
    const strategy = view.account_id==='etf' ? view.id : 'holding_area';
    document.querySelector('#manual-entry').onclick = () => openEntry(account, null, strategy);
    document.querySelector('#position-transfer').onclick = () => openEntry(account, {side:'assign'}, strategy);
    document.querySelector('#cash-transfer').onclick = () => openEntry(account, {side:'cash_transfer'}, strategy);
    document.querySelectorAll('[data-custody-account]').forEach(button=>button.onclick=()=>{
      openCustodyMove(account,button.dataset.custodyAccount,account.custody_positions[Number(button.dataset.custodyRow)]);
    });
    document.querySelectorAll('[data-cash-adjust]').forEach(button=>button.onclick=()=>{
      openCashAdjust(account,button.dataset.cashAdjust);
    });
    async function editRecordEvent(button) {
      button.disabled=true;
      try {
        const context=await accountRequest('/api/etf/event/context',{revision:account.revision,event_id:button.dataset.editEvent});
        const before={...account,...context};
        if(context.event.side==='cash_assign') openCashAllocation(before,{edit:context.event});
        else if(context.event.side==='cash_adjust') openCashAdjust(before,context.event.trading_account || 'unlocated',context.event);
        else if(context.event.side==='custody_transfer') {
          const e=context.event;
          const row=before.custody_positions.find(r=>r.account===e.trading_account && r.kind===e.kind && r.strategy===e.strategy && r.code===e.code);
          openCustodyMove(before,e.trading_account,row,e);
        } else if(isFundTrade(context.event.side)) openFundTrade(before,context.event.side,context.event.code,context.event.strategy || 'core',context.event);
        else openEntry(before,context.event,context.event.strategy,{edit:context.event});
      } catch(error) {document.querySelector('#status').textContent=error.message;}
      finally {button.disabled=false;}
    }
    document.querySelectorAll('[data-edit-event]').forEach(button=>button.onclick=()=>editRecordEvent(button));
    bindRecords(view, editRecordEvent, order=>openFundVoid(account, view, order, 'delete-subscription'));
    document.querySelectorAll('[data-position-side]').forEach(button => button.onclick = () => openEntry(account, {side:button.dataset.positionSide,code:button.dataset.code}, button.dataset.strategy));
    for (const button of document.querySelectorAll('[data-holding-transfer]')) {
      const {code, kind, strategy} = button.dataset;
      button.onclick = () => openEntry(account, {side:'assign', code, kind}, strategy);
    }
    for (const button of document.querySelectorAll('[data-classify-asset]')) {
      button.onclick = () => openAssetClassification(account, button.dataset);
    }
    for (const button of document.querySelectorAll('[data-strategy-tab]')) {
      button.onclick = () => document.getElementById(`tab-${button.dataset.strategyTab}`).click();
    }
    const dcaButton = document.querySelector('#dca-settings');
    if (dcaButton) dcaButton.onclick = () => openDcaPlans(account);
    document.querySelectorAll('[data-fund-dca]').forEach(button=>button.onclick=()=>
      openDcaPlans(account, button.dataset.strategy, button.dataset.fundDca));
    document.querySelectorAll('[data-void-event]').forEach(button=>button.onclick=()=>
      openFundVoid(account, view, view.fund_pending[button.dataset.voidEvent], 'fund_void'));
    document.querySelectorAll('[data-redeem-receipt]').forEach(button=>button.onclick=()=> {
      const order = account.fund_redeem_pending[button.dataset.redeemReceipt];
      openFundTrade(account, 'fund_redeem', order.code, order.strategy, null, order);
    });
    document.querySelectorAll('[data-redeem-void]').forEach(button=>button.onclick=()=>
      openFundVoid(account, view, account.fund_redeem_pending[button.dataset.redeemVoid], 'fund_redeem_void'));
    for (const button of document.querySelectorAll('[data-fund-side]')) {
      button.onclick = () => openFundTrade(account, button.dataset.fundSide, button.dataset.code, button.dataset.strategy);
    }
    document.querySelectorAll('[data-open-cash]').forEach(button=>button.onclick=()=>openCashAllocation(account));
    document.querySelectorAll('[data-cash-rebalance]').forEach(button=>button.onclick=()=>openCashAllocation(account,{rebalance:true}));
    document.querySelectorAll('[data-fill]').forEach(button => button.onclick = () => openEntry(account, view.suggestions[Number(button.dataset.fill)], strategy));
    document.querySelectorAll('[data-dismiss]').forEach(button => button.onclick = () => {
      const target = view.suggestions[Number(button.dataset.dismiss)];
      if (button.dataset.armed) {
        delete button.dataset.armed;
        withGuard(button, () => accountRequest('/api/etf/dismiss', {revision:account.revision, id:target.id}));
      } else {
        // 两步确认：第一次点击进入待确认态，5 秒不点即自动复原。
        button.dataset.armed = '1';
        button.textContent = '确认忽略？';
        button.setAttribute('aria-label', `忽略${target.name || target.code}的${target.side==='sell' ? '减持' : '增持'}建议，再次点击确认`);
        clearTimeout(button._armedTimer);
        button._armedTimer = setTimeout(() => {
          if (!button.isConnected) return;
          delete button.dataset.armed;
          button.textContent = '本轮忽略';
          button.removeAttribute('aria-label');
        }, 5000);
      }
    });
  }

  function openAssetClassification(account, {code, kind, category}) {
    const dialog = document.querySelector('#asset-class-dialog');
    const form = document.querySelector('#asset-class-form');
    const field = form.elements.namedItem('asset_class');
    const save = document.querySelector('#asset-class-save');
    const error = document.querySelector('#asset-class-error');
    const name = kind === 'fund' ? fundDisplayName(code) : account.positions.find(row => row.code === code)?.name;
    document.querySelector('#asset-class-product').textContent = [name, code].filter(Boolean).join(' · ');
    field.innerHTML = '<option value="">请选择</option>' + Object.entries(ASSET_NAMES)
      .filter(([value]) => value !== 'unknown').map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
    field.value = category === 'unknown' ? '' : category;
    field.disabled = false;
    save.disabled = false;
    save.textContent = '保存分类';
    error.hidden = true;
    let saving = false, saved = false;
    document.querySelector('#asset-class-cancel').onclick = () => { if (!saving) dialog.close(); };
    dialog.oncancel = event => { if (saving) event.preventDefault(); };
    form.onsubmit = async event => {
      event.preventDefault();
      if (saving || (!saved && !form.reportValidity())) return;
      saving = true; save.disabled = true; field.disabled = true; error.hidden = true;
      try {
        if (!saved) {
          await accountRequest('/api/etf/asset/classify', {revision:account.config_revision, kind, code, asset_class:field.value});
          saved = true;
        }
        await reloadAccount();
        dialog.close();
        document.querySelector('#status').textContent = `${code} 已归入${ASSET_NAMES[field.value]}`;
      } catch (failure) {
        error.textContent = saved ? '分类已保存，页面更新失败。请点击刷新账户。' : failure.message;
        error.hidden = false;
      } finally {
        saving = false; save.disabled = false; field.disabled = saved;
        save.textContent = saved ? '刷新账户' : '保存分类';
      }
    };
    dialog.showModal();
    field.focus();
  }

  function openCashAllocation(account, options={}) {
    const dialog=document.querySelector('#cash-dialog'),form=document.querySelector('#cash-form');
    const error=document.querySelector('#cash-error');
    const fields=document.querySelector('#cash-fields');
    const button=form.querySelector('[type=submit]');
    let saving=false, saved=false, planning=false;
    let requestId=crypto.randomUUID();
    form.reset();error.hidden=true;fields.disabled=false;button.disabled=!!options.rebalance;button.textContent='确认分配';
    document.querySelector('#cash-title').textContent=options.edit?'修改历史现金分配':options.rebalance?'现金再平衡':'分配现金';
    const preview=document.querySelector('#cash-rebalance-preview');
    preview.hidden=true;
    const dialogRequest=crypto.randomUUID();dialog.dataset.request=dialogRequest;
    document.querySelector('#cash-date-label').hidden=!options.edit;
    const dateField=form.elements.namedItem('date');
    dateField.value=options.edit?.date || '';
    dateField.min=account.min_date || account.start_date;
    dateField.max=account.max_date || '';
    dateField.required=!!options.edit;
    document.querySelector('#cash-total').textContent=money(account.cash);
    const cashFields=document.querySelector('#cash-strategies');
    cashFields.hidden=!!options.rebalance;
    cashFields.innerHTML=currentStrategies().map(({id,name}) =>
      `<label>${esc(name)} / 元<input name="${esc(id)}" type="number" min="0" step="0.01" required></label>`).join('');
    const inputs = currentStrategies().map(({id}) => form.elements.namedItem(id));
    for (const input of inputs) input.value=((options.edit?.cash_allocations || account.cash_by_strategy)[input.name] || 0).toFixed(2);
    function update() {
      requestId=crypto.randomUUID();
      preview.hidden=true;
      const allocated=inputs.reduce((sum,input)=>sum+Number(input.value),0);
      const remaining=Math.round((account.cash-allocated)*100)/100;
      const message=remaining<0 ? `超出账户现金 ${money(-remaining)} 元` : `待定现金 ${money(remaining)} 元`;
      const remainder=document.querySelector('#cash-remaining');
      remainder.textContent=message;
      remainder.hidden=!!options.rebalance && remaining===0;
      remainder.classList.toggle('invalid',remaining<0);
      inputs[0].setCustomValidity(remaining<0 ? message : '');
    }
    form.oninput=update;
    document.querySelector('#cash-cancel').onclick=()=>{if(!saving)dialog.close();};
    dialog.oncancel=event=>{if(saving)event.preventDefault();};
    const rebalanceButton=document.querySelector('#cash-fill-target');
    rebalanceButton.hidden=!!options.edit;
    rebalanceButton.disabled=!finite(account.equity);
    rebalanceButton.onclick=async()=>{
      if(planning || saved)return;
      planning=true;rebalanceButton.disabled=true;button.disabled=true;error.hidden=true;
      try {
        const plan=await accountRequest('/api/etf/cash/rebalance',{revision:account.revision});
        if(!dialog.open || dialog.dataset.request!==dialogRequest)return;
        for(const input of inputs) input.value=plan.cash_allocations[input.name].toFixed(2);
        update();
        const area=plan.rows.find(row=>row.id==='holding_area');
        const rows=plan.rows.filter(row=>row.deviation!==null || row===area).map(row=>{
          const gap=row===area ? Math.round((row.invested+row.after_cash-row.residual_budget)*100)/100 : row.deviation;
          const label=row===area ? (gap>0?'高于余量':gap<0?'低于余量':'符合余量') : (gap>0?'超配':gap<0?'低配':'已对齐');
          return `<tr><td>${esc(row.name)}</td><td>${row.change>0?'+':''}${money(row.change)}</td><td>${money(row.after_cash)}</td><td>${label}${gap?` ${money(Math.abs(gap))}`:''}</td></tr>`;
        }).join('');
        const overweight=plan.rows.filter(row=>row.holding_overweight).map(row=>row.name);
        const note=plan.shortfall>0?`按当前持仓，达到策略预算共需现金 ${money(plan.required_cash)} 元。可调配 ${money(plan.available_cash)} 元，按各策略所需现金的比例分配后，仍低于预算 ${money(plan.shortfall)} 元。`:overweight.length?'已补足低配策略的现金。':'本次调整后，各策略达到已设预算。';
        const retained=plan.rows.filter(row=>row.deviation===null && row!==area && row.after_cash>0)
          .map(row=>`${esc(row.name)}保留现金 ${money(row.after_cash)} 元。`).join('');
        const basis = valuationLabel(plan.valuation) + (Object.keys(account.fund_positions || {}).length ? ' · 场外按各自净值日' : '');
        preview.innerHTML=`<p class="entry-preview">${note}${overweight.length?` ${esc(overweight.join('、'))}的持仓市值已超过预算。`:''}${retained}</p><p class="subtle">${esc(basis)}</p><div class="table-wrap"><table class="cash-plan-table"><thead><tr><th>策略</th><th>现金调整 / 元</th><th>调整后现金 / 元</th><th>调整后偏离预算 / 元</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        preview.hidden=false;
      } catch(failure) {
        if(dialog.dataset.request===dialogRequest) {preview.hidden=true;error.textContent=failure.message;error.hidden=false;}
      } finally {
        planning=false;
        if(dialog.dataset.request===dialogRequest) {rebalanceButton.disabled=!finite(account.equity);button.disabled=!!options.rebalance && preview.hidden;}
      }
    };
    form.onsubmit=async event=>{
      event.preventDefault();if(saving || planning || (options.rebalance && preview.hidden))return;
      if(!form.reportValidity())return;
      saving=true;button.disabled=true;error.hidden=true;fields.disabled=true;
      const day=shanghaiToday();
      const cash_allocations=Object.fromEntries(inputs.map(input=>[input.name,Number(input.value)]));
      const payload={request_id:requestId,revision:account.revision,date:day,side:'cash_assign',cash_allocations};
      if(options.edit) {payload.date=dateField.value;payload.event_id=options.edit.id;}
      try {
        if(!saved) {await accountRequest(options.edit?'/api/etf/event/edit':'/api/etf/event',payload);saved=true;}
        await reloadAccount();dialog.close();
        showEntryReceipt(options.edit?.id || requestId, !!options.edit);
      } catch (failure) {
        error.textContent=saved ? '现金分配已保存，页面更新失败。请刷新账户查看。' : failure.message;
        error.hidden=false;
      } finally {saving=false;fields.disabled=saved;button.disabled=false;button.textContent=saved?'刷新账户':'确认分配';}
    };
    update();dialog.showModal();
    if(options.rebalance) {
      if(finite(account.equity)) rebalanceButton.click();
      else {error.textContent='部分持仓待估值，请先补齐估值数据再进行现金再平衡。';error.hidden=false;}
    }
  }

  return { bindAccountActions };
}
