const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = value => typeof value === 'number' && Number.isFinite(value);
const quoteTime = value => new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false});

async function accountRequest(path, payload) {
  let response, result;
  try {
    response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Trade-Request':'dashboard'}, body:JSON.stringify(payload)});
    result = await response.json();
  } catch { throw Error('连接中断，请重试。'); }
  if (!response.ok) throw Error(result.error || (path==='/api/etf/refresh' ? '估值暂时无法更新，请稍后重试。' : '保存失败，请重试。'));
  return result;
}

export { esc, finite, quoteTime, accountRequest };
