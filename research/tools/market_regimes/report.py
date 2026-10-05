"""市场状态诊断的独立HTML报告，不依赖绘图库。"""

from __future__ import annotations

import html
import json
from pathlib import Path
import pandas as pd

from .analysis import STATES

def table_html(frame: pd.DataFrame, columns: dict[str, str], percent: set[str]) -> str:
    body = []
    for _, row in frame.iterrows():
        cells = []
        for col in columns:
            value = row[col]
            if pd.isna(value):
                value = "—"
            elif col in percent:
                value = f"{value * 100:.2f}%"
            elif isinstance(value, pd.Timestamp):
                value = value.strftime("%Y-%m-%d")
            elif col in {"days", "episodes", "year", "episode"}:
                value = str(int(value))
            cells.append(f"<td>{html.escape(str(value))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return ("<table><thead><tr>" + "".join(f"<th>{html.escape(v)}</th>" for v in columns.values()) +
            "</tr></thead><tbody>" + "".join(body) + "</tbody></table>")


def chart_payload(daily: pd.DataFrame) -> dict:
    cols = ["close", "month_ma", "week_ma", "strategy_nav", "benchmark_nav", "index_nav", "strategy_drawdown"]
    return {"dates": daily["date"].dt.strftime("%Y-%m-%d").tolist(), "states": daily["state"].tolist(),
            **{c: daily[c].round(6).tolist() for c in cols if c in daily}}


def write_report(analyses: list[dict], out: Path, metadata: dict) -> None:
    index_name, benchmark_name = metadata["index_name"], metadata["benchmark_name"]
    excluded_years = ",".join(map(str, metadata["exclude_years"]))
    sections, payloads = [], {}
    for i, item in enumerate(analyses):
        daily, summary, episodes = item["daily"], item["summary"], item["episodes"]
        payloads[str(i)] = chart_payload(daily)
        percent = {"strategy_annualized", "index_annualized", "relative_index_annualized",
                   "episode_median_return", "episode_win_rate", "excluded_years_annualized", "episode_worst_drawdown"}
        summary_columns = {"state_label": "状态", "days": "交易日", "episodes": "连续段",
            "strategy_annualized": "策略折算年化", "index_annualized": f"{index_name}折算年化",
            "relative_index_annualized": f"相对{index_name}年化", "episode_median_return": "每段收益中位数",
            "episode_win_rate": "盈利段比例"}
        if excluded_years:
            summary_columns["excluded_years_annualized"] = f"剔除{excluded_years}折算年化"
        stats = table_html(summary, summary_columns, percent)
        interval_html = table_html(episodes, {"episode": "段", "state_label": "状态", "start": "开始", "end": "结束",
            "days": "交易日", "strategy_return": "策略收益", "index_return": f"{index_name}收益",
            "relative_index_return": f"相对{index_name}收益", "strategy_max_drawdown": "本段最大回撤"},
            {"strategy_return", "index_return", "relative_index_return", "strategy_max_drawdown"})
        totals = (f"{daily['date'].iloc[0]:%Y-%m-%d} 至 {daily['date'].iloc[-1]:%Y-%m-%d} · "
                  f"{len(daily)}交易日 · 策略累计 {daily['strategy_nav'].iloc[-1] - 1:.2%} · "
                  + (f"{benchmark_name}累计 {daily['benchmark_nav'].iloc[-1] - 1:.2%} · "
                   if "benchmark_nav" in daily else "") +
                  f"{index_name}累计 {daily['index_nav'].iloc[-1] - 1:.2%}")
        sections.append(f'<section id="report-{i}" {"hidden" if i else ""}><h2>{html.escape(item["name"])}</h2>'
            f'<p>{html.escape(totals)}</p><div class="table-wrap">{stats}</div>'
            f'<p class="note">折算年化按该状态内的交易日计算（{metadata["trading_days_per_year"]}日），用于比较收益强弱，'
            '不是择时策略年化。连续段长短不同，中位数只作辅助；相对收益用净值比。'
            '首尾段受样本边界截断。可选的年份剔除用于检查收益集中现象。</p>'
            f'<details><summary>查看全部 {len(episodes)} 个连续区间</summary><div class="table-wrap">{interval_html}</div></details></section>')
    options = "".join(f'<option value="{i}">{html.escape(x["name"])}</option>' for i, x in enumerate(analyses))
    legend = "".join(f'<span><i style="background:{color}"></i>{label}</span>' for label, color in STATES.values())
    content = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>市场状态收益诊断</title>
<style>
body{margin:24px auto;padding:0 20px;max-width:1320px;font-family:system-ui,"Microsoft YaHei",sans-serif;color:#17314b;background:#f5f7fa}
h1{font-size:26px}h2{font-size:20px}p{line-height:1.65}.note{color:#596879;font-size:13px}
select,button{padding:8px 12px;border:1px solid #cbd5e1;background:white;border-radius:5px;color:#17314b}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin:14px 0}.legend i{display:inline-block;width:13px;height:13px;margin-right:5px;border-radius:2px}
canvas{width:100%;height:720px;background:#fff;border:1px solid #dbe3eb;border-radius:8px;touch-action:pan-y}
#tooltip{min-height:40px;font-size:13px;line-height:1.7;color:#334155}.table-wrap{overflow:auto;background:#fff;border:1px solid #dbe3eb;border-radius:6px;margin:12px 0}
table{border-collapse:collapse;white-space:nowrap;width:100%;font-size:13px}th,td{padding:10px 12px;border-bottom:1px solid #e2e8f0;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#edf2f7}details{margin:18px 0}summary{cursor:pointer}
.controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap}label{font-size:13px}
</style>
<h1>市场状态收益诊断</h1>
<p>__INDEX_NAME__ <b>__MONTH__月均线 + __WEEK__周均线</b>。月强：上一已结束月的收盘≥月均线；周强：上一已结束周的收盘≥周均线。周五/月末当天仍使用旧状态，下一交易日更新。</p>
<div class="controls"><select id="strategy">__OPTIONS__</select><label>起始 <input id="from" type="date"></label><label>截止 <input id="to" type="date"></label><button id="reset">全部期间</button></div>
<div class="legend">__LEGEND__</div>
<canvas id="chart" aria-label="市场指数、均线、策略净值和回撤，底色代表当日已知市场状态"></canvas><div id="tooltip">移动鼠标查看日期、市场状态和净值；修改日期可放大某段行情。</div>
__SECTIONS__
<p class="note">仅对原回测曲线作收益归因，未执行状态过滤或切换策略，也未搜索均线参数。状态变化前持有的仓位、成本都保留在原曲线中。周线和月线描述同一市场，不能当成两份独立证据。收益差异尚需重复区间和样本外验证。</p>
<p class="note">数据：收益为本地CSV；__SOURCE_DESCRIPTION__。__INPUTS__</p>
<script>
const series=__DATA__, colors=__COLORS__, labels=__LABELS__;
const indexName=__INDEX_JSON__, benchmarkName=__BENCHMARK_JSON__;
const canvas=document.getElementById('chart'), ctx=canvas.getContext('2d'), choose=document.getElementById('strategy');
const from=document.getElementById('from'), to=document.getElementById('to');
let visible=[], geom=null;
function draw(cursor=-1){
 const data=series[choose.value], width=canvas.clientWidth, height=720, ratio=window.devicePixelRatio||1;
 canvas.width=width*ratio;canvas.height=height*ratio;ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);
 visible=data.dates.map((d,i)=>i).filter(i=>(!from.value||data.dates[i]>=from.value)&&(!to.value||data.dates[i]<=to.value));
 if(!visible.length){geom=null;ctx.fillText('所选日期没有数据',50,50);return}
 const left=70,right=width-25,n=visible.length,x=j=>left+(right-left)*(n===1?0.5:j/(n-1));
 const hasBenchmark=Object.hasOwn(data,'benchmark_nav');
 const panels=[{top:36,bottom:235,cols:['close','month_ma','week_ma'],names:[indexName,'月均线','周均线'],colors:['#1e293b','#a855f7','#0891b2'],title:indexName+'收盘 / 已完成周期均线'},
 {top:290,bottom:490,cols:hasBenchmark?['strategy_nav','benchmark_nav','index_nav']:['strategy_nav','index_nav'],names:hasBenchmark?['策略',benchmarkName,indexName]:['策略',indexName],colors:hasBenchmark?['#7c3aed','#64748b','#0891b2']:['#7c3aed','#0891b2'],title:'累计净值（初始资金=1，放大后保留原净值）'},
 {top:545,bottom:675,cols:['strategy_drawdown'],names:['策略回撤'],colors:['#b91c1c'],title:'策略自历史高点回撤'}];
 for(const p of panels){
  let values=p.cols.flatMap(c=>visible.map(i=>data[c][i])),lo=Math.min(...values),hi=Math.max(...values);
  if(p.cols[0]==='strategy_drawdown')hi=0;
  let span=hi-lo||1;lo-=span*.06;if(p.cols[0]!=='strategy_drawdown')hi+=span*.06;const y=v=>p.bottom-(v-lo)/(hi-lo)*(p.bottom-p.top);
  for(let j=0;j<n;){let k=j+1;while(k<n&&data.states[visible[k]]===data.states[visible[j]])k++;
   const a=j===0?left:(x(j-1)+x(j))/2,b=k===n?right:(x(k-1)+x(k))/2;
   ctx.globalAlpha=.10;ctx.fillStyle=colors[data.states[visible[j]]];ctx.fillRect(a,p.top,b-a,p.bottom-p.top);ctx.globalAlpha=1;j=k;}
  ctx.font='12px system-ui';ctx.fillStyle='#334155';ctx.fillText(p.title,left,p.top-15);
  for(let tick=0;tick<5;tick++){const value=lo+(hi-lo)*tick/4,yy=y(value);ctx.strokeStyle='#e2e8f0';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.textAlign='right';ctx.fillText(p.cols[0]==='strategy_drawdown'?(value*100).toFixed(1)+'%':value.toFixed(p.cols[0]==='close'?0:2),left-8,yy+4);ctx.textAlign='left';}
  p.cols.forEach((c,k)=>{ctx.strokeStyle=p.colors[k];ctx.lineWidth=1.6;ctx.beginPath();visible.forEach((i,j)=>{if(j===0)ctx.moveTo(x(j),y(data[c][i]));else ctx.lineTo(x(j),y(data[c][i]));});ctx.stroke();});
  let lx=Math.max(left+330,right-260);p.names.forEach((name,k)=>{ctx.fillStyle=p.colors[k];ctx.fillText('━ '+name,lx,p.top-15);lx+=85;});
  if(cursor>=0){ctx.setLineDash([3,4]);ctx.strokeStyle='#64748b';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x(cursor),p.top);ctx.lineTo(x(cursor),p.bottom);ctx.stroke();ctx.setLineDash([]);}
 }
 ctx.fillStyle='#64748b';ctx.font='11px system-ui';for(let tick=0;tick<6;tick++){const j=Math.round((n-1)*tick/5);ctx.textAlign=tick===0?'left':tick===5?'right':'center';ctx.fillText(data.dates[visible[j]],x(j),701);}ctx.textAlign='left';
 geom={left,right,n};
}
function reset(){const d=series[choose.value];from.value=d.dates[0];to.value=d.dates.at(-1);draw();}
choose.addEventListener('change',()=>{document.querySelectorAll('section').forEach((s,i)=>s.hidden=String(i)!==choose.value);reset();});
from.addEventListener('change',()=>draw());to.addEventListener('change',()=>draw());document.getElementById('reset').addEventListener('click',reset);
canvas.addEventListener('mousemove',e=>{if(!geom)return;const px=e.clientX-canvas.getBoundingClientRect().left;
 const j=Math.max(0,Math.min(geom.n-1,Math.round((px-geom.left)/(geom.right-geom.left)*(geom.n-1)))),i=visible[j],d=series[choose.value];
 document.getElementById('tooltip').textContent=`${d.dates[i]} · ${labels[d.states[i]]} · ${indexName} ${d.close[i].toFixed(2)} · 月均线 ${d.month_ma[i].toFixed(2)} · 周均线 ${d.week_ma[i].toFixed(2)} · 策略净值 ${d.strategy_nav[i].toFixed(4)} · 回撤 ${(d.strategy_drawdown[i]*100).toFixed(2)}%`;draw(j);});
window.addEventListener('resize',()=>draw());reset();
</script></html>'''
    source_description = html.escape(f"{index_name}日线：{metadata['source']}")
    if metadata.get("source_url"):
        source_description += f'，<a href="{html.escape(metadata["source_url"], quote=True)}">行情来源</a>'
    safe_json = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    substitutions = {"__MONTH__": str(metadata["month_window"]), "__WEEK__": str(metadata["week_window"]),
                     "__OPTIONS__": options, "__LEGEND__": legend, "__SECTIONS__": "".join(sections),
                     "__INDEX_NAME__": html.escape(index_name), "__SOURCE_DESCRIPTION__": source_description,
                     "__INDEX_JSON__": safe_json(index_name), "__BENCHMARK_JSON__": safe_json(benchmark_name),
                     "__INPUTS__": html.escape("输入：" + ", ".join(x["name"] for x in analyses)),
                     "__DATA__": safe_json(payloads),
                     "__COLORS__": json.dumps({k: v[1] for k, v in STATES.items()}),
                     "__LABELS__": json.dumps({k: v[0] for k, v in STATES.items()}, ensure_ascii=False)}
    for key, value in substitutions.items():
        content = content.replace(key, value)
    (out / "report.html").write_text(content, encoding="utf-8")


