import { finite } from './common.js';

const number = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:0}) : '—';
const price = value => finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:4}) : '—';
const money = value => finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
const signed = value => { const rounded=Number(value.toFixed(2)); return `${rounded>0?'+':''}${rounded.toFixed(2)}`; };
const percent = nav => finite(nav) && nav > 0 ? `${signed((nav - 1) * 100)}%` : '—';
const excess = (nav, base) => finite(nav) && finite(base) ? `${signed((nav - base) * 100)} 个百分点` : '—';
const ratio = (part, whole) => finite(part) && finite(whole) && whole > 0 ? `${(part / whole * 100).toFixed(1)}%` : '—';
const accountName = account => account?.name || '账户';
const reportDate = value => /^\d{8}$/.test(value) ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6)}` : value;
const timestamp = day => Date.parse(`${day}T00:00:00Z`);
const isoDay = value => /^\d{4}\/\d{1,2}\/\d{1,2}/.test(value) ? value.replace(/(\d{4})\/(\d{1,2})\/(\d{1,2}).*/, (_,y,m,d) => `${y}-${m.padStart(2,'0')}-${d.padStart(2,'0')}`) : value;
const SIDES = {buy:'买入', sell:'卖出', dividend:'分红', deposit:'入金', withdraw:'出金', assign:'持仓划转', cash_assign:'现金分配', cash_adjust:'现金校准', cash_transfer:'现金划转', fund_buy:'场外申购', fund_redeem:'场外赎回', fund_pending:'场外申购', fund_confirm:'场外申购确认', fund_void:'申购作废'};
SIDES.custody_transfer = '账户间划转';

const isAllocation = strategy => strategy.kind === 'allocation';
const acceptsFunds = strategy => strategy.accepts_funds;
const HOLDING_NAMES = {etf:'ETF', stock:'股票', other:'其他持仓', fund:'场外基金'};
const ASSET_NAMES = {equity:'权益', bond:'债券', commodity:'商品', cash:'现金类', mixed:'混合', unknown:'待分类'};
const isFundTrade = side => typeof side === 'string' && side.startsWith('fund_');
Object.assign(SIDES, {fund_redeem_pending:'场外赎回预登记', fund_redeem_void:'赎回作废'});
const fundShares = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:6}) : '—';

const shanghaiToday = () => new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const quotePrice = row => finite(row.close) ? price(row.close) : '<small class="price-pending">待估值</small>';

const valuationLabel = valuation => valuation?.date ? `收盘估值 · ${valuation.date}` : '估值日待确认';

export {
  number, price, money, signed, percent, excess, ratio, accountName, reportDate, timestamp,
  isoDay, SIDES, isAllocation, acceptsFunds, HOLDING_NAMES, ASSET_NAMES, isFundTrade, fundShares,
  shanghaiToday, quotePrice, valuationLabel,
};
