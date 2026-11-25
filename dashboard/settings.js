const key=location.pathname.split('/')[2] || 'budgets';
const activeLink=document.querySelector(`[data-settings="${key}"]`);
const name=activeLink.textContent;
activeLink.setAttribute('aria-current','page');
document.title=`${name} · 账户设置`;
document.querySelector('#config-title').textContent=name;
const form=document.querySelector('#config-form');
const fields=document.querySelector('#config-fields');
const error=document.querySelector('#config-error');
const statusLine=document.querySelector('#config-status');
const save=document.querySelector('#config-save');
const retry=document.querySelector('#config-retry');
const allocation=key==='core';
const panel=allocation ? 'allocation' : key;
const saveLabel=key==='history' ? '恢复所选版本' : '保存'+name;
save.textContent=saveLabel;
for (const section of fields.querySelectorAll('[data-config-panel]')) {
  section.hidden=section.disabled=section.dataset.configPanel!==panel;
}

async function loadSettings(message='') {
  fields.disabled=true;
  save.disabled=true;
  retry.hidden=true;
  error.hidden=true;
  statusLine.textContent=message || '正在读取配置…';
  try {
    const response=await fetch('/api/etf/config',{cache:'no-store'});
    const result=await response.json();
    if (!response.ok) throw Error(result.error || '配置读取失败');
    editSection(result);
    fields.hidden=fields.disabled=false;
    statusLine.textContent=message;
  } catch (failure) {
    error.textContent=failure instanceof TypeError ? '无法读取配置，请重试。' : failure.message;
    error.hidden=false;
    statusLine.textContent=message;
    retry.hidden=false;
    retry.onclick=()=>loadSettings(message);
  }
}

function editTradingAccounts(settings, existing) {
  const list=document.querySelector('#trading-account-list');
  const products=document.querySelector('#trading-product-list');
  list.replaceChildren();
  products.replaceChildren();
  function accountData() {
    return Object.fromEntries([...list.children].map(row=>[row.dataset.id,{
      name:row.querySelector('input').value.trim(), channel:row.querySelector('select').value,
    }]));
  }
  function refreshChoices() {
    const accounts=accountData();
    for (const select of [document.querySelector('#default-listed'),document.querySelector('#default-fund'),...products.querySelectorAll('[data-account]')]) {
      const channel=select.id ? select.id.slice(8) : select.parentElement.parentElement.querySelector('[data-channel]').value;
      const value=select.value;
      select.replaceChildren(...Object.entries(accounts).filter(([,r])=>r.channel===channel).map(([id,r])=>new Option(r.name || '新账户',id)));
      if ([...select.options].some(o=>o.value===value)) select.value=value;
    }
  }
  function addAccount(id, account) {
    const row=document.createElement('div');
    row.className='entry-grid';
    row.dataset.id=id;
    row.innerHTML='<label>账户名称<input required maxlength="40"></label><label>交易渠道<select><option value="listed">场内证券</option><option value="fund">场外基金</option></select></label>';
    row.querySelector('input').value=account.name;
    row.querySelector('select').value=account.channel;
    row.querySelector('select').disabled=!!existing?.accounts[id];
    if (!existing?.accounts[id]) {
      const remove=document.createElement('button');
      remove.type='button';
      remove.className='text-button';
      remove.textContent='移除';
      remove.onclick=()=>{row.remove();refreshChoices();};
      row.append(remove);
    }
    row.oninput=refreshChoices;
    list.append(row);
    refreshChoices();
  }
  function addProduct(product='', id='') {
    const [channel,code]=product.split(':');
    const row=document.createElement('div');
    row.className='entry-grid';
    row.innerHTML='<label>交易渠道<select data-channel><option value="fund">场外基金</option><option value="listed">场内证券</option></select></label><label>产品代码<input required pattern="[0-9]{6}" maxlength="6" inputmode="numeric"></label><label>默认账户<select data-account required></select></label><button class="text-button" type="button">移除</button>';
    row.querySelector('input').value=code || '';
    row.querySelector('[data-channel]').value=channel || 'fund';
    row.querySelector('[data-channel]').onchange=refreshChoices;
    row.querySelector('button').onclick=()=>row.remove();
    products.append(row);
    refreshChoices();
    if (id) row.querySelector('[data-account]').value=id;
  }
  Object.entries(settings.accounts).forEach(([id,row])=>addAccount(id,row));
  Object.entries(settings.products).forEach(([product,id])=>addProduct(product,id));
  for (const [kind,id] of Object.entries(settings.defaults)) document.querySelector('#default-'+kind).value=id;
  document.querySelector('#add-trading-account').onclick=()=>addAccount(crypto.randomUUID(),{name:'',channel:'fund'});
  document.querySelector('#add-trading-product').onclick=()=>addProduct();
  return ()=>{
    const overrides={};
    for (const row of products.children) {
      const product=row.querySelector('[data-channel]').value+':'+row.querySelector('input').value;
      if (Object.hasOwn(overrides,product)) throw Error('同一产品只能设置一个默认账户');
      overrides[product]=row.querySelector('[data-account]').value;
    }
    return {accounts:accountData(),defaults:{listed:document.querySelector('#default-listed').value,
      fund:document.querySelector('#default-fund').value},products:overrides};
  };
}

function editSection(result) {
  const budgetFields=document.querySelector('#config-budgets');
  const strategies=Object.entries(result.strategy_names);
  budgetFields.innerHTML=key==='budgets' ? strategies.map(([id,label]) =>
    `<label>${esc(label)}目标占比 / %<input name="${esc(id)}" type="number" min="0" max="100" step="any" placeholder="暂不设置"></label>`).join('') : '';
  const budgetInputs=[...budgetFields.querySelectorAll('input')];
  const feeKind=form.elements.namedItem('fund_fee_kind');
  const feeValue=form.elements.namedItem('fund_fee_value');
  const stampDuty=form.elements.namedItem('stock_sell_stamp_duty');
  const commissions=Object.fromEntries(['stock','etf'].map(kind=>[kind,{
    rate:form.elements.namedItem(`${kind}_commission`), minimum:form.elements.namedItem(`${kind}_min_commission`),
  }]));
  document.querySelector('#config-assets').replaceChildren();
  document.querySelector('#config-pool').replaceChildren();
  const percentInput=value=>+(value*100).toFixed(8);
  const assetsEditor=document.querySelector('#config-assets');
  const memberCodes=(row,kind)=>Array.from(row.querySelectorAll(`[data-member="${kind}"]`),input=>input.value.trim());
  const draft=result.config;
  const readAccounts=key==='accounts' && editTradingAccounts(draft.trading_accounts || result.trading_accounts_template,draft.trading_accounts);
  const requestedNames=new Set();
  form.onchange=async event=>{
    const input=event.target;
    const kind=input.dataset.member || (input.matches('#config-pool [data-field=code]') ? 'listed' : '');
    const code=input.value.trim();
    if (!kind || !/^[0-9]{6}$/.test(code)) return;
    const names=kind==='fund' ? result.fund_names : result.names;
    const product=`${kind}:${code}`;
    if (names[code] || requestedNames.has(product)) return;
    requestedNames.add(product);
    try {
      const path=kind==='fund' ? '/api/etf/fund/names' : '/api/etf/security/names';
      const found=await accountRequest(path,{codes:[code]});
      Object.assign(names,found.names);
      if (input.isConnected) {
        if (allocation) validateMembers();
        else validatePool();
      }
    } catch {
      requestedNames.delete(product);
    }
  };
  function updateFeeField() {
    const usesValue=feeKind.value==='rate' || feeKind.value==='fixed';
    document.querySelector('#config-fund-fee-field').hidden=!usesValue;
    document.querySelector('#config-fund-fee-label').textContent=feeKind.value==='rate'?'实际费率 / %':'申购费 / 元';
    feeValue.disabled=!usesValue;
    feeValue.required=usesValue;
    for (const {rate,minimum} of Object.values(commissions)) {
      rate.required=minimum.value!=='' || (rate===commissions.stock.rate && stampDuty.value!=='');
      minimum.required=rate.value!=='' || rate.required;
    }
  }
  function assetWeight(input) {
    const original = input.closest('.config-asset').original.weight;
    const value = Number(input.value);
    return value === percentInput(original) ? original : value / 100;
  }
  function total() {
    const weights=[...document.querySelectorAll('#config-assets [data-field=weight]')];
    const sum=weights.reduce((n,input)=>n+assetWeight(input),0);
    const weightIssue=sum>1+1e-8;
    document.querySelector('#config-add').disabled=weights.length>=49;
    weights.forEach(input=>input.setCustomValidity(''));
    weights.at(-1)?.setCustomValidity(weightIssue ? `${name}标的权重合计不能超过100%。` : '');
    const cash=weightIssue ? '—' : `${+(Math.max(0,1-sum)*100).toFixed(4)}%`;
    document.querySelector('#config-total').textContent=`标的权重 ${+(sum*100).toFixed(4)}% · 目标现金 ${cash}（自动）${weightIssue?' · 标的权重不能超过100%':''}`;
    document.querySelector('#config-total').classList.toggle('invalid',weightIssue);
  }
  function budgetTotal() {
    const sum=budgetInputs.reduce((sum,input)=>sum+Number(input.value),0);
    const issue=sum>100+1e-8 ? '策略目标占比合计不能超过100%。' : '';
    budgetInputs[0]?.setCustomValidity(issue);
    const summary=document.querySelector('#config-budget-total');
    summary.textContent=`正式策略 ${+sum.toFixed(4)}% · ${issue || `待定占比 ${+(100-sum).toFixed(4)}%`}`;
    summary.classList.toggle('invalid',!!issue);
  }
  function validateMembers() {
    const seen=new Set();
    for (const input of assetsEditor.querySelectorAll('[data-member]')) {
      const code=input.value.trim();
      const kind=input.dataset.member;
      const id=`${kind}:${code}`;
      const valid=/^[0-9]{6}$/.test(code);
      const issue=!code ? '请输入产品代码' : !valid ? '代码须为六位数字' : seen.has(id) ? '此代码已在本组合中使用' : '';
      input.setCustomValidity(issue);
      input.setAttribute('aria-invalid',String(!!code && !!issue));
      const caption=input.parentElement.querySelector('small');
      const productName=kind==='listed' ? result.names[code] : result.fund_names[code];
      caption.textContent=code ? issue || productName || '暂无名称' : '';
      if(valid) seen.add(id);
    }
  }
  function addRow(asset) {
    const row=document.createElement('section');
    row.className='config-asset';
    row.original=structuredClone(asset);
    const options=(values,current)=>values.map(([value,label])=>`<option value="${value}" ${value===current?'selected':''}>${label}</option>`).join('');
    const listed = !asset.code || asset.code.startsWith('FUND:') ? [] : [asset.code, ...(asset.etf_codes || [])];
    row.innerHTML = `
      <div class="entry-grid config-asset-head">
        <label>配置名称<input data-field="label" value="${esc(asset.label || '')}" maxlength="40" placeholder="如创业板"></label>
        <label>目标权重 / %<input data-field="weight" type="number" value="${percentInput(asset.weight)}" min="0" max="100" step="any" required></label>
        <button type="button" class="text-button" data-remove>删除配置项</button>
      </div>
      <div class="entry-grid config-member-groups"></div>
        <details>
          <summary>其他设置</summary>
          <div class="entry-grid config-advanced">
            <label>资产类别<select data-field="asset_type">${options([['equity','权益'],['bond','债券'],['gold','黄金']],asset.asset_type)}</select></label>
            <label>估值来源<select data-field="valuation_kind">${options([['none','不展示估值'],['csindex','中证指数'],['overseas','海外']],asset.valuation_kind)}</select></label>
            <label>跟踪指数<input data-field="tracked_index" value="${esc(asset.tracked_index || '')}" pattern="[0-9]{6}" maxlength="6"></label>
            <label>减仓顺序<input data-field="volatility_rank" type="number" min="0" max="100" step="1" value="${asset.volatility_rank}" required></label>
            <label>股息估值<select data-field="dividend_style"><option value="false" ${!asset.dividend_style?'selected':''}>否</option><option value="true" ${asset.dividend_style?'selected':''}>是</option></select></label>
          </div>
        </details>
    `;
    const field=name=>row.querySelector(`[data-field=${name}]`);
    function update() {
      const hasListed=!!row.querySelector('[data-member="listed"]');
      field('label').setCustomValidity(row.querySelector('[data-member]') ? '' : '请添加至少一个场内产品或场外基金。');
      const index=field('tracked_index'),indexed=hasListed && field('valuation_kind').value==='csindex';
      index.disabled=!indexed;index.required=indexed;
      field('valuation_kind').disabled=field('dividend_style').disabled=!hasListed;
    }
    function addGroup(kind,title,codes,limit) {
      const group=document.createElement('fieldset');
      group.className='config-member-group';
      group.innerHTML=`<legend>${title}</legend><div data-members></div><button type="button" class="text-button">＋ 添加${title}</button>`;
      const list=group.querySelector('[data-members]');
      const add=group.querySelector('button');
      function append(code) {
        const member=document.createElement('div');
        member.className='config-member';
        member.innerHTML=`<input type="text" inputmode="numeric" required placeholder="六位代码" aria-label="${title}代码" data-member="${kind}"><small></small><button type="button" class="text-button" aria-label="移除${title}">移除</button>`;
        const input=member.querySelector('input');
        input.value=code;
        member.querySelector('button').onclick=()=>{
          const next=member.nextElementSibling || member.previousElementSibling;
          member.remove();
          add.disabled=false;
          update();
          validateMembers();
          (next?.querySelector('input') || add).focus();
        };
        list.append(member);
        add.disabled=list.childElementCount>=limit;
        return input;
      }
      codes.forEach(append);
      add.onclick=()=>{append('').focus();update();};
      row.querySelector('.config-member-groups').append(group);
    }
    addGroup('listed','场内产品',listed,21);
    addGroup('fund','场外基金',asset.fund_codes || [],20);
    row.oninput=event=>{
      if(event.target.matches('[data-member]')) validateMembers();
      if(event.target.matches('[data-field=valuation_kind]')) update();
    };
    row.querySelector('[data-remove]').onclick=()=>{row.remove();validateMembers();total();};
    assetsEditor.append(row);
    update();
    return row;
  }
  function validatePool() {
    const rows=[...document.querySelectorAll('#config-pool tr')];
    const counts=new Map();
    rows.forEach(row=>{
      const code=row.querySelector('[data-field=code]').value.trim();
      if(code) counts.set(code,(counts.get(code)||0)+1);
    });
    rows.forEach(row=>{
      const codeField=row.querySelector('[data-field=code]'), groupField=row.querySelector('[data-field=group]');
      const code=codeField.value.trim();
      codeField.setCustomValidity(!/^[0-9]{6}$/.test(code) ? '代码须为六位数字。' : counts.get(code)>1 ? '池内代码不能重复。' : '');
      const group=groupField.value.trim();
      groupField.setCustomValidity(!group ? '请填写资产类别。' : '');
      row.querySelector('[data-name]').textContent=result.names[code] || '';
    });
    document.querySelector('#config-pool-add').disabled=rows.length>=50;
  }
  function addPoolRow(member={code:'',group:''}) {
    const row=document.createElement('tr');
    row.innerHTML=`
      <td><input data-field="code" value="${esc(member.code)}" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required aria-label="标的代码"></td>
      <td><input data-field="group" value="${esc(member.group)}" list="pool-groups" maxlength="8" required placeholder="如 国内权益" aria-label="资产类别"></td>
      <td><small data-name></small></td>
      <td><button type="button" class="text-button" data-remove>删除</button></td>`;
    row.oninput=validatePool;
    row.querySelector('[data-remove]').onclick=()=>{row.remove();validatePool();};
    document.querySelector('#config-pool').append(row);
    validatePool();
    return row;
  }
  const history=document.querySelector('#config-history');
  form.oninput=null;
  if (key==='budgets') {
    for (const input of budgetInputs) input.value=finite(draft.budgets[input.name]) ? percentInput(draft.budgets[input.name]) : '';
    budgetTotal();
    form.oninput=budgetTotal;
  } else if (key==='fees') {
    feeKind.value=draft.fund_subscription_fee?.kind ?? '';
    feeValue.value=draft.fund_subscription_fee?.value ?? '';
    for (const [kind,{rate,minimum}] of Object.entries(commissions)) {
      rate.value=draft.trade_fees?.[kind]?.commission_per_10000 ?? '';
      minimum.value=draft.trade_fees?.[kind]?.min_commission ?? '';
    }
    stampDuty.value=draft.trade_fees?.stock_sell_stamp_duty_per_10000 ?? '';
    updateFeeField();
    form.oninput=updateFeeField;
  } else if (allocation) {
    (draft[key] || []).filter(row=>row.code!=='CASH').forEach(addRow);
    validateMembers();
    total();
    form.oninput=total;
    document.querySelector('#config-add').onclick=()=>{
      const row=addRow({code:'',weight:0,asset_type:'equity',valuation_kind:'none',
        tracked_index:null,dividend_style:false,volatility_rank:0});
      total();
      row.querySelector('[data-field=label]').focus();
    };
  } else if (key==='rotation') {
    (draft.rotation?.pool ?? []).forEach(addPoolRow);
    validatePool();
    document.querySelector('#config-pool-add').onclick=()=>addPoolRow().querySelector('input').focus();
  } else if (key==='history') {
    const available=result.history.some(version=>version.revision!==result.revision);
    history.innerHTML=`<option value="">${available?'请选择要恢复的版本':'暂无可恢复的历史版本'}</option>`+result.history.map((version,index)=>`<option value="${index}" ${version.revision===result.revision?'disabled':''}>${esc(quoteTime(version.saved_at))}${version.revision===result.revision?' · 当前配置':''}</option>`).join('');
    history.onchange=()=>{save.disabled=history.value==='';};
  }
  function readAllocation() {
    const assets=[...assetsEditor.children].map(row=>{
      const get=key=>row.querySelector(`[data-field=${key}]`).value;
      const [firstListed, ...etfs] = memberCodes(row,'listed');
      const label = get('label').trim();
      const funds = memberCodes(row,'fund');
      const valuation = firstListed ? get('valuation_kind') : 'none';
      const asset = {
        code: firstListed || `FUND:${funds[0]}`,
        weight: assetWeight(row.querySelector('[data-field=weight]')),
        asset_type: get('asset_type'),
        valuation_kind: valuation,
        tracked_index: valuation === 'csindex' ? get('tracked_index') : null,
        dividend_style: !!firstListed && get('dividend_style') === 'true',
        volatility_rank: Number(get('volatility_rank')),
      };
      if (label) asset.label = label;
      if (etfs.length) asset.etf_codes = etfs;
      if (funds.length) asset.fund_codes = funds;
      return asset;
    });
    const cashIndex=draft[key]?.findIndex(row=>row.code==='CASH') ?? -1;
    if (cashIndex>=0) assets.splice(cashIndex,0,draft[key][cashIndex]);
    return assets;
  }
  function applyFees(config) {
    delete config.fund_subscription_fee;
    delete config.trade_fees;
    if(feeKind.value) config.fund_subscription_fee={kind:feeKind.value,value:feeKind.value==='none'?0:Number(feeValue.value)};
    const tradeFees={};
    for (const [kind,{rate,minimum}] of Object.entries(commissions)) {
      if (rate.value!=='') tradeFees[kind]={commission_per_10000:Number(rate.value),min_commission:Number(minimum.value)};
    }
    if (stampDuty.value!=='') tradeFees.stock_sell_stamp_duty_per_10000=Number(stampDuty.value);
    if (Object.keys(tradeFees).length) config.trade_fees=tradeFees;
  }
  function editedConfig() {
    if (key==='history') {
      const version=structuredClone(result.history[Number(history.value)].config);
      if (draft.trading_accounts) {
        version.trading_accounts ??= structuredClone(draft.trading_accounts);
        version.trading_accounts.accounts={...draft.trading_accounts.accounts,...version.trading_accounts.accounts};
      }
      return version;
    }
    const config=structuredClone(draft);
    if (readAccounts) config.trading_accounts=readAccounts();
    if (allocation) config[key]=readAllocation();
    if (key==='fees') applyFees(config);
    if (key==='budgets') {
      for (const {name,value} of budgetInputs) {
        if (value==='') delete config.budgets[name];
        else if (Number(value)!==percentInput(draft.budgets[name])) config.budgets[name]=Number(value)/100;
      }
    }
    if (key==='rotation') {
      const pool=Array.from(document.querySelectorAll('#config-pool tr'),row=>({
        code:row.querySelector('[data-field=code]').value.trim(),
        group:row.querySelector('[data-field=group]').value.trim(),
      }));
      if(pool.length) config.rotation={pool};
      else delete config.rotation;
    }
    return config;
  }
  form.onsubmit=async event=>{
    event.preventDefault();
    if (save.disabled) return;
    fields.disabled=save.disabled=true;
    statusLine.textContent='正在保存…';
    error.hidden=true;
    try {
      await accountRequest('/api/etf/config',{revision:result.revision,config:editedConfig()});
    } catch (failure) {
      error.textContent=failure.message;
      error.hidden=false;
      statusLine.textContent='';
      fields.disabled=save.disabled=false;
      return;
    }
    await loadSettings(key==='history' ? '已恢复所选版本。' : name+'已保存。');
  };
  save.disabled=key==='history';
}

loadSettings();
