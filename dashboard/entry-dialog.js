function createEntryDialog({
  ownerViews, strategyOptions, strategyName, acceptsFunds,
  number, money, fundShares, fundDisplayName, shanghaiToday,
  currentAccount, reloadAccount, showEntryReceipt,
}) {
  return function openEntry(account, suggestion, strategy='holding_area', options={}) {
    const dialog = document.querySelector('#event-dialog');
    const form = document.querySelector('#event-form');
    const error = document.querySelector('#event-error');
    const save = document.querySelector('#event-save');
    const next = document.querySelector('#event-continue');
    const fields = document.querySelector('#event-fields');
    const refresh = document.querySelector('#event-refresh');
    const success = document.querySelector('#event-success');
    const reference = document.querySelector('#entry-reference');
    const armedNote = document.querySelector('#entry-armed-note');
    let confirmationReason = '';
    let confirmedSubmitter = null;
    const referenceQty = options.edit ? null : suggestion?.qty;
    // 重开表单前恢复各类型，防止 reset 跳到上次类别中唯一可用的选项。
    for (const option of form.elements.namedItem('side').options) option.disabled = false;
    form.reset(); error.hidden = true; save.disabled = false; next.disabled = false;
    next.hidden = !!options.edit;
    save.textContent = options.edit ? '保存修改' : '保存记账';
    fields.disabled = false; refresh.hidden = true;
    success.hidden = !options.success; success.textContent = options.success || '';
    let requestId = crypto.randomUUID();
    let saving = false;
    const field = name => form.elements.namedItem(name);
    delete field('trading_account').dataset.context;
    field('side').value = 'buy';
    const owners = strategyOptions();
    field('strategy').innerHTML = `<option value="">请选择策略</option>${owners}`;
    field('from_strategy').innerHTML = owners;
    const today = shanghaiToday();
    field('date').value = options.edit?.date || options.date || today;
    field('date').min = options.edit ? account.min_date : account.trades?.[0]?.date || account.start_date;
    field('date').max = options.edit ? account.max_date : today;
    field('strategy').value = strategy;
    if (suggestion) {
      for (const key of ['side','code']) if (suggestion[key] !== undefined) field(key).value = suggestion[key];
    }
    if (field('side').value==='cash_transfer') {
      field('strategy').value=strategy==='holding_area'?'core':strategy;
      field('from_strategy').value=account.cash_by_strategy.holding_area>0 ? 'holding_area' :
        Object.keys(account.cash_by_strategy).find(key=>key!==field('strategy').value && account.cash_by_strategy[key]>0) || 'holding_area';
    }
    if (field('side').value==='assign') {
      field('from_strategy').value = options.fromStrategy || strategy || 'holding_area';
      field('strategy').value = options.fromStrategy ? strategy : strategy==='core' ? 'holding_area' : 'core';
      if (suggestion?.kind === 'fund') {
        field('strategy').value = ownerViews().find(row => row.id !== strategy && acceptsFunds(row))?.id || '';
      }
    }
    const securities = new Map(account.positions.map(row=>[row.code,row.name]));
    if(options.edit) for(const key of ['side','date','code','qty','price','fee','strategy','from_strategy','amount']) if(options.edit[key]!==undefined) field(key).value=options.edit[key];
    const kind = field('kind');
    kind.value = options.edit ? options.edit.kind || 'listed' : suggestion?.kind || '';
    const fundOwnerIds = new Set(ownerViews().filter(acceptsFunds).map(row=>row.id));
    for (const item of ownerViews()) {
      for (const code of Object.keys(item.target_weights || {})) if (code!=='CASH' && !securities.has(code)) securities.set(code, code);
    }
    let suggestionOwner;
    function entryPosition() {
      const assign = field('side').value === 'assign';
      const owner = field(assign ? 'from_strategy' : 'strategy').value;
      const code = field('code').value;
      const listed = account.strategy_positions.find(row=>row.strategy===owner && row.code===code);
      const fund = assign ? account.strategy_fund_positions?.[owner]?.[code] : null;
      const ambiguous = Boolean(listed && fund);
      const selectedFund = fund && (!listed || kind.value === 'fund') ? fund : null;
      const held = ambiguous && !kind.value ? 0 : selectedFund ? selectedFund.qty : listed?.count || 0;
      return {listed, fund:selectedFund, ambiguous, held};
    }
    // 建议价格只用于估算，实际成交均价必须由用户填写。
    function update() {
      const side = field('side').value;
      const trade = ['buy','sell'].includes(side);
      const assign = side === 'assign';
      const cashTransfer = side === 'cash_transfer';
      const internal = assign || cashTransfer;
      const mode = trade ? 'trade' : assign ? 'assign' : cashTransfer ? 'cash_transfer' : 'cash';
      field('side').parentElement.hidden = internal;
      document.querySelector('#event-title').textContent = options.edit ? '修改历史记账' : assign ? '持仓划转' : cashTransfer ? '现金划转' : trade ? '记录买卖成交' : '记录出入金 / 分红';
      document.querySelectorAll('[data-entry-mode]').forEach(button=>{
        button.classList.toggle('active',button.dataset.entryMode===mode);
        button.setAttribute('aria-pressed',button.dataset.entryMode===mode);
      });
      for (const option of field('side').options) {
        const group = ['buy','sell'].includes(option.value) ? 'trade' : ['assign','cash_transfer'].includes(option.value) ? option.value : 'cash';
        option.hidden = group!==mode; option.disabled = group!==mode;
      }
      document.querySelectorAll('[data-trade-field]').forEach(label => {
        const input = label.querySelector('input,select');
        const shown = trade || (assign && !['price','fee'].includes(input.name));
        label.hidden = !shown; input.disabled = !shown;
      });
      field('strategy').parentElement.hidden = false;
      field('strategy').disabled = false;
      const source = field('from_strategy');
      source.parentElement.hidden = !internal; source.disabled = !internal;
      field('strategy').parentElement.firstChild.textContent = internal ? '转入策略' : '策略';
      document.querySelector('#entry-qty-label').textContent = assign ? '划转份额' : '成交份额';
      const {listed, fund, ambiguous, held:totalHeld} = entryPosition();
      const location=custodySelect(account,field('trading_account'),{side,kind:fund?'fund':'listed',code:field('code').value,
        strategy:field(assign?'from_strategy':'strategy').value,edit:options.edit});
      const held=location ? location.qty : totalHeld;
      kind.disabled = !ambiguous;
      document.querySelector('#entry-kind-field').hidden = !ambiguous;
      const shareStep = fund ? '0.000001' : side==='buy' ? '100' : '1';
      field('qty').min = shareStep;
      field('qty').step = shareStep;
      document.querySelector('[data-cash-field]').hidden = trade || assign;
      field('amount').disabled = trade || assign;
      field('amount').parentElement.firstChild.textContent = cashTransfer ? '划转金额 / 元' : side==='withdraw' ? '转出金额 / 元' : '到账金额 / 元';
      const owner = internal ? source.value : field('strategy').value;
      const cash = account.cash_by_strategy[owner];
      const ownerName = strategyName(owner);
      const code = field('code').value;
      reference.hidden = !(trade && referenceQty > 0 && side===suggestion?.side && code===suggestion.code && owner===strategy);
      if (!reference.hidden) document.querySelector('#entry-reference-qty').textContent = `建议${side==='sell' ? '减持' : '增持'}约 ${number(referenceQty)} 份`;
      const nextSuggestionOwner = assign ? owner : null;
      if (nextSuggestionOwner !== suggestionOwner) {
        suggestionOwner = nextSuggestionOwner;
        const choices = assign ? new Map(account.strategy_positions.filter(row=>row.strategy===owner)
          .map(row=>[row.code,row.name])) : securities;
        if (assign) for (const fundCode of Object.keys(account.strategy_fund_positions?.[owner] || {})) {
          choices.set(fundCode, choices.has(fundCode) ? '场内证券 / 场外基金' : fundDisplayName(fundCode) || '场外基金');
        }
        document.querySelector('#entry-securities').innerHTML = [...choices]
          .map(([value,name])=>`<option value="${esc(value)}">${esc(name)}</option>`).join('');
      }
      const chooseKind = ambiguous && !kind.value;
      const knownName = fund ? fundDisplayName(code) || '场外基金' : assign ? listed?.name : securities.get(code);
      document.querySelector('#entry-code-label').textContent = fund ? '基金代码' : assign ? '标的代码' : '证券代码';
      document.querySelector('#entry-security').textContent = chooseKind ? '同一代码有两类持仓，请选择标的类型'
        : knownName && knownName!==code ? knownName
        : assign ? '可输入代码，或从转出组合的持仓中选择'
        : code.length===6 ? '请核对证券代码' : '可输入代码，或从已有持仓中选择';
      for (const option of field('strategy').options) {
        option.disabled = Boolean(fund && option.value && !fundOwnerIds.has(option.value));
        if (option.value) option.textContent = internal ? strategyName(option.value) : `${strategyName(option.value)} · ${money(account.cash_by_strategy[option.value])} 元现金`;
      }
      const all = document.querySelector('#entry-all');
      all.hidden = !['sell','assign'].includes(side);
      all.textContent = assign ? '全部划转' : '全部卖出';
      all.disabled = held<=0;
      const context = document.querySelector('#entry-context');
      const shares = (fund ? fundShares : number)(held);
      context.textContent = !ownerName ? '请选择本笔记账所属的策略。' : assign ? code && !chooseKind ? `${ownerName}可划转 ${shares} 份` : '选择持仓后查看可划转份额。' : `${ownerName}现金 ${money(cash)} 元${trade && code ? ` · 持有 ${shares} 份` : ''}`;
      if (location?.id && !assign) context.textContent+=' · 账户现金 '+money(location.cash)+' 元';
      if (assign && held > 0) {
        context.textContent += '。划转前的盈亏归转出策略，之后归转入策略。';
      }
      const amount = trade ? Number(field('qty').value) * Number(field('price').value) : Number(field('amount').value);
      const feeInput = field('fee');
      const feeKind = Object.keys(account.trade_fee_code_prefixes || {}).find(key =>
        /^\d{6}$/.test(code) && account.trade_fee_code_prefixes[key].some(prefix => code.startsWith(prefix)));
      const rule = trade && !options.edit ? account.trade_fees?.[feeKind] : null;
      const roundMoney = value => Math.round(Number((value * 100).toPrecision(15))) / 100;
      const automaticFee = rule && feeInput.value === '';
      const commission = automaticFee && amount > 0 ? roundMoney(Math.max(rule.min_commission, amount * rule.commission_per_10000 / 10000)) : 0;
      const stampDuty = automaticFee && feeKind === 'stock' && side === 'sell'
        ? roundMoney(amount * (account.trade_fees.stock_sell_stamp_duty_per_10000 ?? 0) / 10000) : 0;
      const fee = automaticFee ? roundMoney(commission + stampDuty) : trade ? Number(feeInput.value) : 0;
      document.querySelector('#entry-fee-label').textContent = rule ? '手续费 / 元（可按交割单修正）' : '手续费 / 元';
      feeInput.placeholder = automaticFee && amount > 0 ? `${money(fee)} 元（自动）` : rule ? '按账户费率自动计算' : '0';
      const delta = Math.round((['buy','withdraw','cash_transfer'].includes(side) ? -amount-fee : amount-fee)*100)/100;
      const preview = document.querySelector('#event-preview');
      preview.hidden = assign || !(amount>0);
      const feeDetails = automaticFee ? ` · 佣金 ${money(commission)} 元${kind === 'stock' && side === 'sell' ? ` · 印花税 ${money(stampDuty)} 元` : ''}` : '';
      if (preview.hidden) preview.textContent = '';
      else if (cashTransfer) preview.textContent = `${ownerName}划出 ${money(amount)} 元后剩余 ${money(cash-amount)} 元；${strategyName(field('strategy').value)}收到后余额 ${money(account.cash_by_strategy[field('strategy').value]+amount)} 元。账户总现金不变。`;
      else preview.textContent = `${trade ? `成交金额 ${money(amount)} 元 · 手续费 ${money(fee)} 元${feeDetails}。` : ''}现金${delta>=0?'增加':'减少'} ${money(Math.abs(delta))} 元；${ownerName || '所选策略'}记账后余额 ${finite(cash) ? money(cash+delta) : '—'} 元。`;
      // 成交价护栏：与参考价偏离或金额偏大时，提交按钮升级为带金额的两段式确认。
      const saveDefaultLabel = options.edit ? '保存修改' : '保存记账';
      const referencePrice = trade && !options.edit && code===suggestion?.code && side===suggestion?.side ? suggestion?.price : null;
      const deviation = trade && finite(referencePrice) && Number(referencePrice)>0 && field('price').value !== ''
        ? Math.abs(Number(field('price').value) - referencePrice) / referencePrice : null;
      confirmationReason = deviation !== null && deviation > 0.05
        ? `成交价偏离参考价 ${money(referencePrice)} 元 ${(deviation*100).toFixed(1)}%`
        : amount >= 50000 && trade ? '成交金额较大' : '';
      save.textContent = saveDefaultLabel;
      next.textContent = '保存并继续';
      const problem = {message:'', field:null};
      if (assign && code.length===6 && !held && !chooseKind) {
        problem.message = `${ownerName || '转出组合'}暂无此代码的已确认持仓。`; problem.field='code';
      } else if (fund && !finite(fund.nav)) {
        problem.message = '场外基金缺少单位净值，请先更新估值。'; problem.field='code';
      } else if (fund && field('strategy').value && !fundOwnerIds.has(field('strategy').value)) {
        problem.message = `场外基金只能在${ownerViews().filter(acceptsFunds).map(row=>row.name).join('和')}之间划转。`; problem.field='strategy';
      } else if (['sell','assign'].includes(side) && Number(field('qty').value)>held) {
        problem.message = `份额超过${ownerName || '所选策略'}持仓（${shares} 份）。`; problem.field='qty';
      } else if (side==='buy' && field('qty').value && Number(field('qty').value)%100!==0) {
        problem.message = '买入份额须为100份的整数倍。'; problem.field='qty';
      } else if (internal && source.value===field('strategy').value) {
        problem.message = '请选择不同的划转来源和目标。'; problem.field='strategy';
      } else if (!assign && location?.id && Math.round((location.cash+delta)*100)<0) {
        problem.message='所选交易账户现金不足。'; problem.field=trade?'price':'amount';
      } else if (!assign && finite(cash) && Math.round((cash+delta)*100)<0) {
        problem.message = `${ownerName}现金不足${side==='buy' ? '，可先通过“划转现金”补充' : ''}。`;
        problem.field=trade?'price':'amount';
      }
      // 金额或份额突变时提示单位级 fat-finger，不影响提交。
      const magnitudeHint = trade && amount>0 && !problem.message && Number(field('qty').value)>0
        && (Number(field('qty').value)/Number(held||1)>=9 || amount>=50000)
        ? '这笔金额不小，已按两位小数核对。' : '';
      for (const name of ['code','qty','amount','price','strategy']) field(name).setCustomValidity('');
      if (problem.field) field(problem.field).setCustomValidity(problem.message);
      const validation = document.querySelector('#entry-validation');
      validation.hidden = !problem.message; validation.textContent = problem.message;
      armedNote.textContent = confirmationReason ? `${confirmationReason}。提交时需再次确认。` : magnitudeHint;
      armedNote.hidden = Boolean(problem.message) || !armedNote.textContent;
    }
    form.oninput = event => {
      if (event?.target === field('code') || event?.target === field('from_strategy')) kind.value = '';
      requestId=crypto.randomUUID(); error.hidden=true;
      confirmedSubmitter = null;
      update();
    };
    form.onchange = form.oninput;
    document.querySelectorAll('[data-entry-mode]').forEach(button=>button.onclick=()=>{
      const mode = button.dataset.entryMode;
      const side = mode==='trade' ? 'buy' : mode==='cash' ? 'deposit' : mode;
      // 先恢复选项，避免浏览器拒绝选中上一类别中被禁用的选项。
      for (const option of field('side').options) option.disabled=false;
      field('side').value = side;
      kind.value = '';
      if (['assign','cash_transfer'].includes(mode) && field('from_strategy').value===field('strategy').value) field('strategy').value = field('from_strategy').value==='core' ? 'holding_area' : 'core';
      form.oninput();
    });
    document.querySelector('#entry-all').onclick = () => {
      const owner=field(field('side').value==='assign'?'from_strategy':'strategy').value;
      field('qty').value = account.trading_accounts ? account.custody_positions.find(r=>r.account===field('trading_account').value && r.code===field('code').value && r.strategy===owner && r.kind===(entryPosition().fund?'fund':'listed'))?.qty || '' : entryPosition().held || '';
      form.oninput();
    };
    document.querySelector('#entry-use-reference').onclick = () => {
      field('qty').value = referenceQty;
      form.oninput();
      field('qty').focus();
    };
    document.querySelector('#event-cancel').onclick = () => { if (!saving) dialog.close(); };
    dialog.oncancel = event => { if (saving) event.preventDefault(); };
    form.onsubmit = async event => {
      event.preventDefault();
      if (saving || fields.disabled) return;
      if (!form.reportValidity()) return;
      const submitter = event.submitter === next ? next : save;
      const keepOpen = submitter === next;
      if (confirmationReason && confirmedSubmitter !== submitter) {
        update();
        confirmedSubmitter = submitter;
        const amount = Number(field('qty').value) * Number(field('price').value);
        submitter.textContent = `确认${keepOpen ? '并继续' : '记入'} ${money(amount)} 元`;
        armedNote.textContent = `${confirmationReason}。检查无误后，再点一次「${submitter.textContent}」。`;
        armedNote.hidden = false;
        return;
      }
      saving = true; save.disabled = true; next.disabled = true; fields.disabled = true; error.hidden = true;
      const side = field('side').value;
      const payload = {request_id:requestId, revision:account.revision, date:field('date').value, side};
      if(options.edit) payload.event_id=options.edit.id;
      if(account.trading_accounts && side!=='cash_transfer') payload.trading_account=field('trading_account').value;
      if (side==='assign') {
        Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), strategy:field('strategy').value, from_strategy:field('from_strategy').value});
        if (entryPosition().fund) payload.kind = 'fund';
      } else if (['buy','sell'].includes(side)) {
        Object.assign(payload, {code:field('code').value, qty:Number(field('qty').value), price:Number(field('price').value), strategy:field('strategy').value});
        if (options.edit || field('fee').value !== '') payload.fee = Number(field('fee').value);
      } else {
        payload.amount=Number(field('amount').value);
        payload.strategy=field('strategy').value;
        if(side==='cash_transfer') payload.from_strategy=field('from_strategy').value;
      }
      async function finish() {
        saving = true;
        refresh.disabled = true;
        try {
          await reloadAccount();
        } catch (failure) {
          saving = false; refresh.disabled = false; refresh.hidden = false;
          error.textContent = '本笔已保存，账户刷新失败。请点击“刷新账户”核对后继续。'; error.hidden=false;
          return;
        }
        saving = false;
        if (!keepOpen) dialog.close();
        const receipt = showEntryReceipt(options.edit?.id || payload.request_id, !!options.edit);
        if (keepOpen) {
          openEntry(currentAccount(), {side}, payload.strategy, {date:payload.date,fromStrategy:payload.from_strategy,success:`${payload.date} · ${receipt}。可继续记下一笔。`});
        }
      }
      refresh.onclick = finish;
      try {
        await accountRequest(options.edit ? '/api/etf/event/edit' : '/api/etf/event', payload);
      } catch (failure) {
        saving = false; fields.disabled = false;
        error.textContent = failure.message; error.hidden=false; save.disabled=false; next.disabled=false;
        return;
      }
      await finish();
    };
    update(); if (!dialog.open) dialog.showModal();
    field(['buy','sell','assign'].includes(field('side').value) ? suggestion?.code && strategy ? 'qty' : 'code' : 'amount').focus();
  };
}
