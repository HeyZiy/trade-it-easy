import { esc, finite, accountRequest } from './common.js';

import { money, SIDES, acceptsFunds, fundShares, shanghaiToday, dcaSourceLabel } from './dashboard-format.js';

import { custodyName, custodySelect } from './trading-accounts.js';

export function createFundDialogs({
  currentAccount, ownerViews, strategyOptions, strategyName, fundDisplayName, reloadAccount, showEntryReceipt, openFundOrders,
}) {
  let fundVoidReceipt;

  function pendingFundSection(account, recordedToday=account.dca?.recorded_today || []) {
    return pendingSubscriptionSection(account, recordedToday) + pendingRedemptionSection(account);
  }

  function fundVoidStatus(view, side) {
    const message = fundVoidReceipt?.view === view.id && fundVoidReceipt.side === side ? fundVoidReceipt.message : '';
    return message ? `<p id="fund-void-receipt" class="entry-success" role="status" tabindex="-1">${esc(message)}</p>` : '';
  }

  function pendingRedemptionSection(view) {
    const account = currentAccount();
    const orders = Object.values(account.fund_redeem_pending || {}).filter(order=>view.id==='etf' || order.strategy===view.id);
    const receipt = fundVoidStatus(view, 'fund_redeem_void');
    if (!orders.length && !receipt) return '';
    const rows = orders.map(order=>`<tr><td>${esc(fundDisplayName(order.code) || order.code)}<small>${esc([order.code, strategyName(order.strategy), account.trading_accounts && custodyName(account,order.trading_account)].filter(Boolean).join(' · '))}</small><small class="pending-fund-mobile">${esc(order.date)} · ${fundShares(order.qty)} 份</small></td><td class="num">${esc(order.date)}</td><td class="num">${fundShares(order.qty)}</td><td><div class="pending-fund-actions"><button type="button" class="text-button" data-redeem-receipt="${esc(order.id)}">确认到账</button><button type="button" class="text-button fund-void-action" data-redeem-void="${esc(order.id)}">撤销登记</button></div></td></tr>`).join('');
    return `<section class="section"><div class="section-heading"><h2>待到账赎回</h2></div>${receipt}${rows ? `<div class="table-wrap"><table class="pending-fund-table"><thead><tr><th>基金</th><th class="num">申请日</th><th class="num">申请份额</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : ''}</section>`;
  }

  function pendingSubscriptionSection(account, recordedToday) {
    const pending = Object.values(account.fund_pending || {});
    const autoNote = recordedToday.length
      ? `今日已自动记账 ${recordedToday.length} 笔（净值日 ${[...new Set(recordedToday)].join('、')}）`
      : '';
    if (!pending.length && !autoNote) return '';
    const rows = pending.map(order => {
      const name = fundDisplayName(order.code);
      return `<tr><td>${esc(name || order.code)}<small>${esc([name && order.code, strategyName(order.strategy), account.trading_accounts && custodyName(account,order.trading_account)].filter(Boolean).join(' · '))}</small><small class="pending-fund-mobile">${esc(order.nav_date)} · ${money(order.amount)} 元</small></td><td class="num">${esc(order.nav_date)}</td><td class="num">${money(order.amount)}</td><td><div class="pending-fund-actions"><button type="button" class="text-button" data-open-fund-order="${esc(order.id)}">查看申购</button></div></td></tr>`;
    }).join('');
    const note = autoNote ? `<p class="subtle">${esc(autoNote)}</p>` : '';
    return `<section id="fund-pending" class="section"><div class="section-heading"><h2>待确认申购</h2>${rows ? `<span class="subtle">在途 ${money(account.pending_fund_value)} 元</span>` : ''}</div>${note}${rows ? `<div class="table-wrap"><table class="pending-fund-table"><thead><tr><th>基金</th><th class="num">买入日</th><th class="num">实际扣款 / 元</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : ''}</section>`;
  }

  async function fillFundName(dialog, session, input, hint, state) {
    const code = input.value;
    if (code === state.code) return;
    state.code = code;
    hint.textContent = fundDisplayName(code) || '';
    hint.hidden = !hint.textContent;
    if (hint.textContent || !/^\d{6}$/.test(code)) return;
    try {
      const {names} = await accountRequest('/api/etf/fund/names', {codes:[code]});
      if (dialog.open && dialog.dataset.session === session && input.value === code) {
        hint.textContent = names[code] || '';
        hint.hidden = !hint.textContent;
      }
    } catch {
      // 简称查询失败不影响表单操作。
    }
  }

  function openFundVoid(account, view, order, side) {
    const dialog = document.querySelector('#fund-void-dialog');
    const form = document.querySelector('#fund-void-form');
    const error = document.querySelector('#fund-void-error');
    const save = document.querySelector('#fund-void-save');
    const cancel = document.querySelector('#fund-void-cancel');
    const deleting = side === 'delete-subscription';
    const name = fundDisplayName(order.code);
    const product = name ? `${name}（${order.code}）` : order.code;
    const strategy = strategyName(order.strategy);
    const dcaSource = deleting && account.dca?.sources?.[order.pending_id || order.id];
    const date = deleting
      ? `${dcaSource ? '扣款日' : order.nav_date ? '申购净值日' : '记账日'} ${order.nav_date || order.date}`
      : `申请日 ${order.date}`;
    const type = deleting ? '申购' : '赎回预登记';
    const amount = deleting ? `${money(order.amount)} 元` : `${fundShares(order.qty)} 份`;
    const location = account.trading_accounts && custodyName(account, order.trading_account);
    const impact = deleting
      ? '请确认这笔申购是误录。删除后会一并移除扣款登记及份额确认，并按剩余流水重算现金、份额和成本。'
      : `撤销登记后，将释放预登记占用的 ${amount}，策略现金不变。`;
    const action = deleting ? '删除' : '撤销';
    const shares = deleting && finite(order.qty) ? `${fundShares(order.qty)} 份` : '';
    const source = deleting ? dcaSourceLabel(dcaSource) || '手动申购' : '';
    document.querySelector('#fund-void-title').textContent = `${action}${type}`;
    document.querySelector('#fund-void-order').textContent = [product, strategy, location, source, date, amount, shares].filter(Boolean).join(' · ');
    document.querySelector('#fund-void-impact').textContent = impact;
    error.hidden = true;
    save.textContent = `确认${action}`;
    save.disabled = deleting;
    cancel.textContent = '取消';
    cancel.disabled = false;

    const payload = {request_id:crypto.randomUUID(), revision:account.revision};
    dialog.dataset.preview = payload.request_id;
    if (deleting) {
      accountRequest('/api/etf/event/preview-delete-subscription', {revision:account.revision, event_id:order.id}).then(() => {
        if (dialog.dataset.preview === payload.request_id) save.disabled = false;
      }).catch(failure => {
        if (dialog.dataset.preview !== payload.request_id) return;
        error.textContent = failure.message;
        error.hidden = false;
      });
    }
    if (deleting) payload.event_id = order.id;
    else Object.assign(payload, {side, voided_pending_id:order.id, date:shanghaiToday()});
    let saving = false, saved = false;
    cancel.onclick = () => dialog.close();
    dialog.oncancel = event => { if (saving) event.preventDefault(); };
    form.onsubmit = async event => {
      event.preventDefault();
      if (saving || save.disabled) return;
      saving = true;
      save.disabled = true;
      cancel.disabled = true;
      error.hidden = true;
      try {
        if (!saved) {
          await accountRequest(deleting ? '/api/etf/event/delete-subscription' : '/api/etf/event', payload);
          saved = true;
          if (!deleting) {
            fundVoidReceipt = {view:view.id, side, message:`${product} · ${type}已撤销。已释放 ${amount}。`};
          }
        }
        await reloadAccount();
        dialog.close();
        if (deleting) {
          document.querySelector('#status').textContent = `${product} · 申购已删除`;
          document.querySelector('#account-records')?.focus({preventScroll:true});
        } else document.querySelector('#fund-void-receipt')?.focus();
      } catch (failure) {
        error.textContent = saved ? `已${action}，账户刷新失败。请点击「刷新账户」核对。` : failure.message;
        error.hidden = false;
        if (saved) {
          save.textContent = '刷新账户';
          cancel.textContent = '关闭';
        }
      } finally {
        saving = false;
        save.disabled = false;
        cancel.disabled = false;
      }
    };
    dialog.showModal();
  }

  function openFundTrade(account, side, code, strategy, edit=null, receipt=null, installment=null) {
    const dialog = document.querySelector('#fund-dialog');
    const form = document.querySelector('#fund-form');
    const fields = document.querySelector('#fund-fields');
    const save = document.querySelector('#fund-save');
    const error = document.querySelector('#fund-error');
    const preview = document.querySelector('#fund-preview');
    const field = name => form.elements.namedItem(name);
    const buy = side !== 'fund_redeem';
    const registerRedemption = !buy && !edit && !receipt;
    const pending = side === 'fund_pending' || (side === 'fund_buy' && !edit);
    const confirming = side === 'fund_confirm';
    const accountFee = pending && !edit ? account.fund_subscription_fee : null;
    const options = document.querySelector('#fund-buy-tools');
    const summary = document.querySelector('#fund-quick-summary');
    const fundName = document.querySelector('#fund-name');
    const fetchNav = document.querySelector('#fund-fetch-nav');
    const recalcQty = document.querySelector('#fund-recalc-qty');
    let manualQty = !!edit, fetchedNavKey, feeCode;
    let saving = false, saved = false, requestId = crypto.randomUUID();
    const today = shanghaiToday();
    const session = crypto.randomUUID();
    dialog.dataset.session = session;
    form.reset();
    delete field('trading_account').dataset.context;
    const owner = field('strategy');
    const fundOwners = ownerViews().filter(acceptsFunds);
    const holders = fundOwners.filter(row=>account.strategy_fund_positions?.[row.id]?.[code]?.qty > 0);
    const choices = strategy ? fundOwners.filter(row=>row.id===strategy) : buy ? fundOwners : holders;
    owner.innerHTML = `<option value="">请选择策略</option>${strategyOptions(choices)}`;
    owner.value = strategy || (holders.length===1 ? holders[0].id : buy && !holders.length ? 'core' : '');
    owner.disabled = !!strategy;
    fields.disabled = false;
    save.disabled = false;
    fetchNav.disabled = false;
    const saveLabel = installment ? '确认补记' : edit ? '保存修改' : receipt ? '确认到账' : registerRedemption ? '保存预登记' : '保存记账';
    save.textContent = saveLabel;
    error.hidden = true;
    const installmentTitle = installment?.row.restoration_terms ? '重新补记定投' : '手动补记定投';
    document.querySelector('#fund-title').textContent = installment ? installmentTitle : edit ? `修改${SIDES[side]}` : receipt ? '确认赎回到账' : registerRedemption ? SIDES.fund_redeem_pending : SIDES[side];
    document.querySelector('#fund-help').textContent = installment
      ? (installment.row.possible_manual ? '账本已有同日同额的手动申购。请核对基金 App，确认这笔扣款尚未登记后再补记。' : '请先核对基金 App 的实际扣款。扣款计入在途资金，之后按公布净值确认份额。')
      : pending
      ? '扣款计入在途资金。每日更新或刷新估值时查到买入日净值，就会自动确认份额。'
      : confirming ? '按交易确认单更正份额。' : registerRedemption ? '登记申请份额，实际到账后再录入到账金额。' : '请按交易确认单和实际资金变动记账。';
    document.querySelector('#fund-amount-label').textContent = buy ? '实际扣款 / 元' : '实际到账 / 元';
    field('amount').parentElement.hidden = registerRedemption;
    field('amount').disabled = registerRedemption;
    document.querySelector('#fund-qty-label').textContent = buy ? '份额（自动计算，可修正）' : edit ? '确认赎回份额' : '申请赎回份额';
    options.hidden = !buy;
    options.open = buy && !!edit && !confirming;
    document.querySelector('#fund-buy-tools-label').textContent = accountFee ? '买入日' : '买入日与费用';
    document.querySelector('#fund-fee-kind-field').hidden = !!accountFee;
    summary.hidden = !pending && !confirming;
    document.querySelector('#fund-code-field').hidden = pending && !!code && !edit;
    document.querySelector('#fund-record-day-field').hidden = pending && !edit;
    document.querySelector('#fund-record-day-label').textContent = receipt ? '到账日期' : registerRedemption ? '申请日期' : '记账日期';
    document.querySelector('#fund-qty-field').hidden = pending;
    document.querySelector('#fund-nav-field').hidden = pending;
    document.querySelector('#fund-nav-day-label').textContent = installment ? '定投扣款日' : '申购净值日期';
    fetchNav.hidden = pending || confirming;
    field('qty').disabled = pending;
    field('fee_kind').disabled = !buy || confirming || !!accountFee;
    field('nav_date').disabled = !buy || confirming;
    field('nav').disabled = !buy || pending || confirming;
    field('code').value = code || '';
    const editingDca = pending && !!account.dca?.sources?.[edit?.id];
    field('code').readOnly = confirming || editingDca || (!!code && !edit);
    for (const name of ['amount','date']) field(name).readOnly = confirming;
    field('amount').value = edit?.amount ?? installment?.row.amount ?? '';
    field('qty').readOnly = !!receipt;
    field('qty').value = (receipt || edit)?.qty ?? '';
    field('date').min = account.min_date || account.trades[0]?.date || receipt?.date || account.start_date;
    field('date').max = account.max_date || today;
    field('date').value = edit?.date || today;
    field('nav').value = edit?.nav ?? '';
    field('nav_date').value = edit?.nav_date || installment?.row.due_date || (pending ? today : '');
    field('nav_date').readOnly = !!installment || editingDca;
    field('nav_date').max = field('date').value;
    if (edit?.subscription_fee) {
      field('fee_kind').value = edit.subscription_fee.kind;
      field('fee').value = edit.subscription_fee.value;
    }
    function loadFeeForCode() {
      const code = field('code').value;
      if (!pending || accountFee || feeCode === code) return;
      feeCode = code;
      const fee = account.fund_subscription_fees?.[code];
      field('fee_kind').value = fee?.kind || '';
      field('fee').value = fee?.value ?? '';
      options.open = !fee && /^\d{6}$/.test(code);
    }
    const nameState = {};
    if (edit) feeCode = code;
    loadFeeForCode();
    const showError = message => { error.textContent = message; error.hidden = false; };
    const navKey = () => `${field('code').value}:${field('nav_date').value}`;
    function selectedFee() {
      if (accountFee) return accountFee;
      const kind = field('fee_kind').value, value = Number(field('fee').value);
      if (kind === 'none') return {kind,value:0};
      if (kind && field('fee').value !== '' && finite(value) && value >= 0) return {kind,value};
      return null;
    }
    function estimateQty(amount, fee, nav) {
      if (!(amount > 0 && nav > 0) || !fee) return 0;
      const net = fee.kind === 'rate' ? amount / (1 + fee.value / 100) : fee.kind === 'fixed' ? amount - fee.value : amount;
      return net > 0 ? Number((Math.round(net * 100) / 100 / nav).toFixed(2)) : 0;
    }
    function update() {
      const code = field('code').value;
      const planAccount = installment?.plan.trading_account;
      const location=custodySelect(account,field('trading_account'),{side,kind:'fund',code,strategy:owner.value,
        edit:receipt || edit || (planAccount ? installment.plan : null)});
      if (receipt || planAccount) field('trading_account').disabled = true;
      const held = location ? location.qty : account.strategy_fund_positions?.[owner.value]?.[code]?.qty || 0;
      const reserved = !buy && !receipt ? Object.values(account.fund_redeem_pending || {})
        .filter(order=>order.code===code && order.strategy===owner.value && order.trading_account===(location?.id || 'unlocated'))
        .reduce((total,order)=>total+order.qty,0) : 0;
      const available = Math.max(0, Number((held-reserved).toFixed(6)));
      const cash = account.cash_by_strategy[owner.value];
      const amount = Number(field('amount').value);
      const kind = field('fee_kind').value;
      const usesFee = buy && !accountFee && (kind === 'rate' || kind === 'fixed');
      document.querySelector('#fund-fee-field').hidden = !usesFee;
      document.querySelector('#fund-fee-label').textContent = kind === 'rate' ? '实际费率 / %' : '申购费 / 元';
      field('fee').disabled = !usesFee || confirming;
      field('nav_date').max = field('date').value;
      field('qty').max = buy ? '1000000000' : String(available);
      if (buy && !pending && !confirming) {
        const estimate = estimateQty(amount, selectedFee(), Number(field('nav').value));
        if (!manualQty) field('qty').value = estimate || '';
        recalcQty.hidden = !manualQty || !estimate;
      } else {
        recalcQty.hidden = true;
      }
      const fee = selectedFee();
      const feeLabel = fee?.kind === 'none' ? '免申购费'
        : fee?.kind === 'rate' ? `费率 ${fee.value}%`
        : fee?.kind === 'fixed' ? `申购费 ${money(fee.value)} 元` : '待设置费用';
      summary.textContent = [code && `基金 ${code}`, `买入日 ${field('nav_date').value || '待选择'}`, accountFee ? `账户申购费：${feeLabel}` : feeLabel].filter(Boolean).join(' · ');
      preview.hidden = confirming;
      preview.textContent = owner.value
        ? `${strategyName(owner.value)}现金 ${money(cash)} 元 · ${registerRedemption ? '可申请赎回' : '持有'} ${fundShares(registerRedemption ? available : held)} 份${amount > 0 ? ` · 记账后现金 ${money(cash + (buy ? -amount : amount))} 元` : ''}`
        : '请选择所属策略。';
      if(location?.id) preview.textContent+=' · 账户现金 '+money(location.cash)+' 元';
      field('amount').setCustomValidity(buy && !confirming && amount > cash ? '所属策略现金不足，请先分配现金。' :
        buy && !confirming && location?.id && amount>location.cash ? '所选交易账户现金不足。' : '');
    }
    form.oninput = event => {
      if (event?.target === field('code')) {
        loadFeeForCode();
        fillFundName(dialog, session, field('code'), fundName, nameState);
      }
      if (event?.target === field('qty')) manualQty = !!field('qty').value;
      if (event?.target === field('nav')) fetchedNavKey = undefined;
      if (fetchedNavKey && fetchedNavKey !== navKey()) {
        field('nav').value = '';
        fetchedNavKey = undefined;
      }
      requestId = crypto.randomUUID();
      error.hidden = true;
      update();
    };
    update();

    fetchNav.onclick = async () => {
      const code = field('code').value, date = field('nav_date').value;
      if (!/^\d{6}$/.test(code) || !date) return showError('请先填写六位基金代码和申购净值日期。');
      error.hidden = true;
      fetchNav.disabled = true;
      try {
        const result = await accountRequest('/api/etf/fund/nav', {code,date});
        if (!saving && !saved && dialog.open && dialog.dataset.session === session && field('code').value === code && field('nav_date').value === date) {
          field('nav').value = result.nav;
          fetchedNavKey = navKey();
          form.oninput();
        }
      } catch (failure) {
        if (dialog.open && dialog.dataset.session === session) showError(failure.message);
      } finally {
        if (dialog.dataset.session === session) fetchNav.disabled = false;
      }
    };
    recalcQty.onclick = () => {
      manualQty = false;
      form.oninput();
    };
    document.querySelector('#fund-cancel').onclick = () => { if (!saving) dialog.close(); };
    dialog.oncancel = event => { if (saving) event.preventDefault(); };
    form.onsubmit = async event => {
      event.preventDefault();
      if (saving) return;
      if (!saved && !form.reportValidity()) return;
      if (!saved && pending && (!field('nav_date').value || !selectedFee())) {
        options.open = true;
        return showError('请先确认买入日和申购费用。');
      }
      if (!saved && buy && field('nav').value && !field('nav_date').value) return showError('请填写单位净值对应的日期。');
      saving = true;
      save.disabled = true;
      fields.disabled = true;
      error.hidden = true;
      try {
        if (!saved) {
          const payload = {request_id:requestId, revision:account.revision, side, strategy:owner.value, code:field('code').value,
            date:field('date').value, qty:Number(field('qty').value), amount:Number(field('amount').value)};
          if (account.trading_accounts) payload.trading_account=field('trading_account').value;
          if (edit) payload.event_id = edit.id;
          if (registerRedemption) {
            payload.side = 'fund_redeem_pending';
            delete payload.amount;
          }
          if (receipt) payload.pending_id = receipt.id;
          if (pending) {
            payload.side = 'fund_pending';
            payload.nav_date = field('nav_date').value;
            if (!accountFee) payload.subscription_fee = selectedFee();
            delete payload.qty;
          } else if (buy) {
            if (field('nav').value) {
              payload.nav = Number(field('nav').value);
              payload.nav_date = field('nav_date').value;
            }
            const fee = selectedFee();
            if (edit?.code === payload.code && fee) payload.subscription_fee = fee;
          }
          if (installment) {
            await accountRequest('/api/etf/dca/record', {...payload, id:installment.plan.id,
              due_date:installment.row.due_date, plan_amount:installment.plan.amount,
              plan_account:installment.plan.trading_account ?? null});
          } else {
            await accountRequest(edit ? '/api/etf/event/edit' : '/api/etf/event', payload);
          }
          saved = true;
        }
        await reloadAccount();
        dialog.close();
        if (installment) installment.onSaved();
        else showEntryReceipt(edit?.id || requestId, !!edit);
      } catch (failure) {
        showError(saved ? '本笔已保存，可点击刷新账户核对。' : failure.message);
      } finally {
        saving = false;
        fields.disabled = saved;
        save.disabled = false;
        save.textContent = saved ? '刷新账户' : saveLabel;
      }
    };
    dialog.showModal();
    fillFundName(dialog, session, field('code'), fundName, nameState);
  }

  function openDcaPlans(account, strategy='', code='', selectedPlanId='') {
    const dialog=document.querySelector('#dca-dialog');
    const form=document.querySelector('#dca-form');
    const fields=document.querySelector('#dca-fields');
    const save=document.querySelector('#dca-save');
    const error=document.querySelector('#dca-error');
    const plansBox=document.querySelector('#dca-plans');
    const installmentsBox=document.querySelector('#dca-installments');
    const formTitle=document.querySelector('#dca-form-title');
    const fundName=code && fundDisplayName(code);
    document.querySelector('#dca-title').textContent=code
      ? `${strategyName(strategy)} · ${fundName || code}定投计划`
      : '定投计划';
    const nameHint=document.querySelector('#dca-fund-name');
    const frequencySelect=form.elements.namedItem('frequency');
    const anchorSelect=form.elements.namedItem('anchor');
    const anchorLabel=document.querySelector('#dca-anchor-label');
    const codeInput=form.elements.namedItem('code');
    const planOwner=form.elements.namedItem('strategy');
    const plans=(account.dca?.plans || []).filter(plan=>
      !code || (plan.code===code && (plan.strategy || 'core')===strategy));
    let selectedPlan=plans.find(plan=>plan.id===selectedPlanId) || (plans.length===1 ? plans[0] : null);
    const FREQUENCY_ANCHORS={monthly:Array.from({length:31},(_,i)=>[i+1,`${i+1} 日`]),
      weekly:[[1,'周一'],[2,'周二'],[3,'周三'],[4,'周四'],[5,'周五']]};
    let editing=null, saving=false;
    const nameState={};
    const session=crypto.randomUUID();
    dialog.dataset.session=session;
    form.reset();error.hidden=true;fields.disabled=false;save.disabled=false;
    fields.hidden=save.hidden=Boolean(code && plans.length>1);
    codeInput.readOnly=Boolean(code);
    codeInput.value=code;
    planOwner.innerHTML=`<option value="">请选择所属组合</option>${strategyOptions(ownerViews().filter(acceptsFunds))}`;
    planOwner.value=strategy;
    planOwner.disabled=Boolean(code);
    nameHint.textContent=fundName || '';
    nameHint.hidden=!fundName;
    save.textContent='保存计划';formTitle.textContent='添加计划';
    const planAccount=form.elements.namedItem('trading_account');
    planAccount.parentElement.hidden=planAccount.disabled=!account.trading_accounts;
    planAccount.replaceChildren(new Option('跟随产品或渠道默认账户',''),...Object.entries(account.trading_accounts?.accounts || {})
      .filter(([,r])=>r.channel==='fund').map(([id,r])=>new Option(r.name,id)));
    function fillAnchors() {
      const current=Number(anchorSelect.value);
      const anchors=FREQUENCY_ANCHORS[frequencySelect.value] || [];
      anchorLabel.hidden=anchorSelect.disabled=anchors.length===0;
      anchorSelect.innerHTML=anchors.map(([value,label])=>`<option value="${value}">${label}</option>`).join('');
      if ([...anchorSelect.options].some(option=>Number(option.value)===current)) anchorSelect.value=current;
    }
    function editPlan(plan) {
      editing=plan;
      fields.hidden=save.hidden=false;
      planOwner.value=plan.strategy || 'core';
      planAccount.value=plan.trading_account || '';
      formTitle.textContent='修改计划';
      save.textContent='保存修改';
      codeInput.value=plan.code;
      form.elements.namedItem('amount').value=plan.amount;
      frequencySelect.value=plan.frequency;
      fillAnchors();
      if (!anchorSelect.disabled) anchorSelect.value=plan.anchor;
      form.elements.namedItem('note').value=plan.note || '';
      nameState.code=plan.code;
      nameHint.textContent=fundDisplayName(plan.code) || '';
      nameHint.hidden=!nameHint.textContent;
      error.hidden=true;
    }
    function renderPlans() {
      plansBox.innerHTML=plans.length ? `<table><thead><tr><th>基金</th><th>计划</th><th>状态</th><th>下次扣款日</th><th>操作</th></tr></thead><tbody>${plans.map(plan=>{
        const name=fundDisplayName(plan.code);
        const owner=strategyName(plan.strategy || 'core');
        let cadence='每个交易日';
        if (plan.frequency==='monthly') cadence=`每月 ${plan.anchor} 日`;
        if (plan.frequency==='weekly') cadence=`每周${FREQUENCY_ANCHORS.weekly[plan.anchor-1][1].slice(1)}`;
        const detail=[name && plan.code, !code && owner].filter(Boolean).join(' · ');
        return `<tr><td>${esc(name || plan.code)}${detail?`<small>${esc(detail)}</small>`:''}</td><td>${cadence} · ${money(plan.amount)} 元${account.trading_accounts?`<small>${plan.trading_account?esc(custodyName(account,plan.trading_account)):'跟随默认账户'}</small>`:''}${plan.note?`<small>${esc(plan.note)}</small>`:''}</td><td>${plan.enabled?'启用中':'已停用'}</td><td>${plan.next_due?esc(plan.next_due):'—'}</td><td><div class="pending-fund-actions"><button type="button" class="text-button" aria-controls="dca-installments" aria-expanded="${selectedPlan===plan}" data-dca-history="${esc(plan.id)}">每期记录</button><button type="button" class="text-button" data-dca-edit="${esc(plan.id)}">修改</button><button type="button" class="text-button" data-dca-toggle="${esc(plan.id)}">${plan.enabled?'停用':'启用'}</button><button type="button" class="text-button" data-dca-delete="${esc(plan.id)}">删除计划</button></div></td></tr>`;
      }).join('')}</tbody></table>` : '<div class="empty">暂无定投计划。请在基金 App 设定自动扣款后，把计划录到这里。</div>';
      plansBox.querySelectorAll('[data-dca-edit]').forEach(button=>button.onclick=()=>{
        const plan=plans.find(row=>row.id===button.dataset.dcaEdit);
        if (plan) editPlan(plan);
      });
      plansBox.querySelectorAll('[data-dca-toggle]').forEach(button=>button.onclick=()=>{
        const plan=plans.find(row=>row.id===button.dataset.dcaToggle);
        if (!plan) return;
        changePlan('/api/etf/dca/save', {...plan, enabled:!plan.enabled},
          plan.enabled?'定投计划已停用':'定投计划已启用', {keepOpen:true});
      });
      plansBox.querySelectorAll('[data-dca-delete]').forEach(button=>button.onclick=()=>{
        if (window.confirm('删除这个定投计划？删除后将无法从定投管理查看或补记该计划的期次。已有申购仍保留在流水里。')) {
          changePlan('/api/etf/dca/delete', {id:button.dataset.dcaDelete}, '定投计划已删除', {keepOpen:true});
        }
      });
      const historyButtons = [...plansBox.querySelectorAll('[data-dca-history]')];
      for (const button of historyButtons) {
        button.onclick = () => {
          const plan = plans.find(row=>row.id===button.dataset.dcaHistory);
          selectedPlan = selectedPlan === plan ? null : plan;
          for (const item of historyButtons) item.setAttribute('aria-expanded', String(item.dataset.dcaHistory===selectedPlan?.id));
          renderInstallments();
        };
      }
    }
    let installmentsAscending = false;
    function renderInstallments() {
      installmentsBox.hidden = !selectedPlan;
      if (!selectedPlan) return;
      const plan = selectedPlan;
      const statuses = {missing:'未记账', pending:'待确认份额', confirmed:'已确认', voided:'已撤销登记', deleted:'已删除'};
      const installments = plan.installments || [];
      const rows = (installmentsAscending ? [...installments].reverse() : installments).map(row => {
        const status = statuses[row.status];
        const action = row.restoration_terms ? '重新补记' : row.status==='missing' ? '手动补记' : '';
        const detail = [row.code!==plan.code ? row.code : '', dcaSourceLabel(row.source),
          row.date && row.date!==row.due_date ? `记账 ${row.date}` : '',
          row.possible_manual ? '有手动申购待核对' : '', row.reason].filter(Boolean).join(' · ');
        return `<tr><td>${esc(row.due_date)}</td><td class="num">${money(row.amount)}</td><td>${esc(status)}${detail ? `<small>${esc(detail)}</small>` : ''}</td><td>${row.order_id ? `<button type="button" class="text-button" data-dca-order="${esc(row.order_id)}">查看申购</button> ` : ''}${action ? `<button type="button" class="text-button" data-dca-due="${esc(row.due_date)}">${action}</button>` : ''}</td></tr>`;
      }).join('');
      installmentsBox.innerHTML = `<div class="section-heading"><h3 id="dca-installments-title">${esc(fundDisplayName(plan.code) || plan.code)} · 每期记录</h3><span class="subtle">最近 ${esc(account.dca.history_days)} 天</span></div>
        ${rows ? `<div class="table-wrap dca-history-table"><table><thead><tr><th aria-sort="${installmentsAscending ? 'ascending' : 'descending'}"><button type="button" data-dca-sort class="text-button">扣款日 ${installmentsAscending ? '↑' : '↓'}</button></th><th class="num">金额 / 元</th><th>记账状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="subtle">这段时间暂无到期记录。</p>'}`;
      const sort = installmentsBox.querySelector('[data-dca-sort]');
      if (sort) sort.onclick = () => {
        installmentsAscending = !installmentsAscending;
        renderInstallments();
        installmentsBox.querySelector('[data-dca-sort]').focus();
      };
      for (const button of installmentsBox.querySelectorAll('[data-dca-order]')) {
        button.onclick = () => {
          openFundOrders(button.dataset.dcaOrder);
          document.querySelector('#fund-orders-dialog').addEventListener('close', () => {
            if (dialog.open) openDcaPlans(currentAccount(), strategy, code, plan.id);
          }, {once:true});
        };
      }
      for (const button of installmentsBox.querySelectorAll('[data-dca-due]')) {
        button.onclick = () => {
          if (saving) return;
          const row = plan.installments.find(item=>item.due_date===button.dataset.dcaDue);
          const terms = {...plan, ...row.restoration_terms};
          openFundTrade(account, 'fund_pending', terms.code, terms.strategy || 'core', null, null, {
            plan:terms, row,
            onSaved: () => {
              openDcaPlans(currentAccount(), strategy, code, plan.id);
              document.querySelector('#status').textContent = '定投已补记';
            },
          });
        };
      }
    }
    frequencySelect.onchange=fillAnchors;
    form.elements.namedItem('code').oninput=event=>fillFundName(dialog, session, event.target, nameHint, nameState);
    document.querySelector('#dca-cancel').onclick=()=>{if(!saving)dialog.close();};
    dialog.oncancel=event=>{if(saving)event.preventDefault();};
    async function changePlan(path, payload, message, {keepOpen=false}={}) {
      if (saving) return;
      saving=true;save.disabled=true;fields.disabled=true;error.hidden=true;
      try {
        await accountRequest(path, payload);
      } catch (failure) {
        error.textContent=failure.message;error.hidden=false;
        saving=false;fields.disabled=false;save.disabled=false;
        return;
      }
      if (!keepOpen) dialog.close();
      try {
        await reloadAccount();
        if (keepOpen) openDcaPlans(currentAccount(), strategy, code, selectedPlan?.id);
        document.querySelector('#status').textContent=message;
      } catch {
        dialog.close();
        document.querySelector('#status').textContent=`${message}，请刷新页面核对。`;
      }
    }
    form.onsubmit=event=>{
      event.preventDefault();
      if (saving || save.hidden || !form.reportValidity()) return;
      const payload={code:codeInput.value.trim(),
        strategy:planOwner.value,
        amount:Number(form.elements.namedItem('amount').value),
        frequency:frequencySelect.value,
        note:form.elements.namedItem('note').value.trim(),
        enabled:editing?.enabled ?? true};
      if (!anchorSelect.disabled) payload.anchor=Number(anchorSelect.value);
      if (editing) payload.id=editing.id;
      if(account.trading_accounts) payload.trading_account=planAccount.value;
      changePlan('/api/etf/dca/save', payload, editing?'定投计划已保存':'定投计划已添加');
    };
    fillAnchors();
    renderPlans();
    renderInstallments();
    if (code && plans.length===1) editPlan(plans[0]);
    if (!dialog.open) dialog.showModal();
  }

  return { pendingFundSection, openFundVoid, openFundTrade, openDcaPlans };
}
