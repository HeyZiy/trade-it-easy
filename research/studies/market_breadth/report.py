"""Render the breadth replay as a self-contained HTML report."""

import math

import pandas as pd


def weekly_chart(weekly, window):
    width = 940
    left, right = 55, 915
    positions = [left + i * (right - left) / max(1, len(weekly) - 1) for i in range(len(weekly))]
    returns = weekly[["a500_return_pct", "industry_median_return_pct"]]
    scale = max(5, math.ceil(returns.abs().max().max() / 5) * 5)

    def breadth_y(value):
        return 136 - value * 2.8

    def return_y(value):
        return 244 - value * 61 / scale

    parts = [f'<svg viewBox="0 0 {width} 346" role="img" aria-label="{window} 行业广度和同期收益">']
    for value in (0, 15.5, 31):
        y = breadth_y(value)
        label = "半数" if value == 15.5 else f"{value:g}"
        parts.append(f'<path class="grid" d="M{left} {y} H{right}"/><text x="9" y="{y+4}">{label}</text>')
    for value in (-scale, 0, scale):
        y = return_y(value)
        parts.append(f'<path class="grid" d="M{left} {y} H{right}"/><text x="9" y="{y+4}">{value:g}%</text>')
    bar_width = min(12, 0.72 * (right - left) / max(len(weekly) - 1, 1))
    for x, (day, row) in zip(positions, weekly.iterrows()):
        y = breadth_y(row.industries_up)
        title = (
            f"{day:%Y-%m-%d} ｜ {row.industries_up:.0f}/31 行业上涨 ｜ "
            f"行业中位数 {row.industry_median_return_pct:+.2f}% ｜ A500 {row.a500_return_pct:+.2f}%"
        )
        if row.narrow_rally:
            parts.append(f'<path class="warning" d="M{x:.1f} 44 V309"/>')
        bar_class = "narrow" if row.narrow_rally else "breadth"
        parts.append(
            f'<rect class="{bar_class}" x="{x-bar_width/2:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{max(1, 136-y):.1f}"><title>{title}</title></rect>'
        )
    for column, line_class in (("a500_return_pct", "index"), ("industry_median_return_pct", "median")):
        points = " ".join(f"{x:.1f},{return_y(v):.1f}" for x, v in zip(positions, weekly[column]))
        parts.append(f'<polyline class="{line_class}" points="{points}"/>')
    for i in sorted({round(j * (len(weekly) - 1) / 5) for j in range(6)}):
        parts.append(f'<text x="{positions[i]:.1f}" y="333" text-anchor="middle">{weekly.index[i]:%y-%m-%d}</text>')
    parts.append("</svg>")
    return "".join(parts)


def risk_comparison(weekly):
    weekly = weekly.dropna(subset=["industries_up"])
    eligible = weekly.loc[weekly.a500_return_pct.gt(0) & weekly.a500_next_4w_return_pct.notna()]
    narrow = eligible.loc[eligible.narrow_rally]
    wide = eligible.loc[~eligible.narrow_rally]
    down = eligible.industries_up_change.lt(0)
    flat_or_up = eligible.industries_up_change.ge(0)

    def row(label, sample):
        if sample.empty:
            return f"<tr><th>{label}</th><td>0</td><td colspan='3'>—</td></tr>"
        forward = sample.a500_next_4w_return_pct
        return (
            f"<tr><th>{label}</th><td>{len(sample)}</td><td>{forward.lt(0).sum()}/{len(sample)}</td>"
            f"<td>{forward.mean():+.2f}%</td><td>{sample.a500_next_4w_worst_close_pct.mean():+.2f}%</td></tr>"
        )

    if narrow.empty or wide.empty:
        takeaway = "当前区间缺少可对照的历史样本。行业广度仍可用于描述上涨的参与面。"
    else:
        difference = wide.a500_next_4w_worst_close_pct.mean() - narrow.a500_next_4w_worst_close_pct.mean()
        if difference > 0:
            takeaway = (
                f"样本中，多数行业未涨时，A500 此后四周的平均最差周收盘表现低 {difference:.2f} 个百分点。"
                "我会把这种分化列为风险提示：计划加仓时，先复核上涨是否扩散、原有仓位是否已集中在领涨方向。"
                "上涨行业数较上周减少只记为观察项。"
            )
        else:
            takeaway = "当前样本中，多数行业未涨时，A500 的后续平均下探未加深。将分化保留为观察项。"

    hits = weekly.narrow_rally & weekly.a500_next_4w_return_pct.notna()
    hit_positions = [i for i, hit in enumerate(hits) if hit]
    episodes = sum(
        i == 0 or position - hit_positions[i - 1] >= 4
        for i, position in enumerate(hit_positions)
    )
    episode_note = f"{len(narrow)} 个已观察到完整后续的提示周，按未来四周收益区间是否重叠合并为 {episodes} 段。"
    details = "".join(row(f"{day:%Y-%m-%d}", narrow.loc[[day]]) for day in narrow.index)
    return f"""
    <section>
      <h2>广度收窄后的风险</h2>
      <p class="callout">{takeaway}</p>
      <p>下表只比较过去四周 A500 上涨的观察周。后续表现从观察周末起算，区间最差表现取未来四个周收盘相对起点的最低收益，正值按 0 计。</p>
      <div class="table"><table>
        <thead><tr><th>观察周的状态</th><th>周数</th><th>四周后下跌</th><th>四周后平均收益</th><th>区间最差表现均值</th></tr></thead>
        <tbody>{row("多数行业未涨", narrow)}{row("多数行业上涨", wide)}
        {row("上涨行业数较上周减少", eligible.loc[down])}
        {row("上涨行业数较上周持平或增加", eligible.loc[flat_or_up])}</tbody>
      </table></div>
      <p class="note">{episode_note} 规则源自对历史行情的观察，提示周集中在少数行情，适合用于人工复核加仓风险。</p>
      <details><summary>查看每个提示周及后续表现</summary><div class="table"><table>
        <thead><tr><th>周末</th><th>周数</th><th>四周后下跌</th><th>四周后收益</th><th>区间最差表现</th></tr></thead>
        <tbody>{details or '<tr><td colspan="5">当前区间暂无完整样本</td></tr>'}</tbody>
      </table></div></details>
    </section>"""


def write_report(result, target):
    sections = []
    for window, label in ((4, "滚动四周"), (1, "单周")):
        weekly = result[window].dropna(subset=["industries_up"])
        if weekly.empty:
            continue
        latest = weekly.iloc[-1]
        if latest.narrow_rally:
            state = "A500 上涨，多数行业未涨"
        elif latest.broad_advance_despite_index_decline:
            state = "A500 下跌，多数行业上涨"
        elif latest.a500_return_pct < 0:
            state = "A500 与多数行业同步走弱"
        elif latest.a500_return_pct > 0:
            state = "A500 与多数行业同步上涨"
        else:
            state = "A500 收平"
        change = latest.industries_up_change
        change_text = f"较上周 {change:+d}" if pd.notna(change) else "较上周 —"
        sections.append(f"""
        <section>
          <h2>{label}<span>{state}</span></h2>
          <div class="metrics">
            <div>上涨行业<strong>{latest.industries_up:.0f} / 31</strong><small>{change_text}</small></div>
            <div>行业收益中位数<strong>{latest.industry_median_return_pct:+.2f}%</strong></div>
            <div>A500 收益<strong>{latest.a500_return_pct:+.2f}%</strong>
            <small>比行业中位数 {latest.a500_minus_industry_median_pp:+.2f} 个百分点</small></div>
          </div>
          <div class="legend"><span>上涨行业数</span><span class="warm">指数上涨、行业参与偏窄</span>
            <span class="blue">A500 收益</span><span class="purple">行业收益中位数</span></div>
          {weekly_chart(weekly, label)}
        </section>""")
    first, last = result.index[[0, -1]]
    content = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>A 股市场观察 · 行业广度</title>
<style>
  * {{ box-sizing:border-box }}
  body {{ margin:0; background:#edf2f1; color:#172c31; font:16px/1.6 "Microsoft YaHei",system-ui,sans-serif }}
  main {{ max-width:1080px; margin:auto; padding:38px 18px }}
  header {{ margin-bottom:24px }} h1 {{ margin:0; font-size:clamp(27px,5vw,42px) }}
  h2 {{ margin:0 0 12px; font-size:24px }} h2 span {{ margin-left:12px; font-size:15px; font-weight:500 }}
  p {{ margin:8px 0 }}
  section {{ background:#fff; margin:0 0 16px; border:1px solid #d8e2e0; border-radius:14px; padding:22px }}
  .metrics {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:12px 0 18px }}
  .metrics div {{ background:#eff5f5; padding:10px 13px; border-radius:8px }}
  strong {{ display:block; font-size:clamp(20px,3.6vw,32px); font-variant-numeric:tabular-nums }}
  .legend {{ display:flex; flex-wrap:wrap; gap:6px 20px; font-size:14px; color:#306869 }}
  .legend span::before {{ content:""; background:currentColor; display:inline-block; width:12px; height:4px; margin:0 6px 3px 0 }}
  .warm {{ color:#a94d16 }} .blue {{ color:#225395 }} .purple {{ color:#73409a }}
  svg {{ display:block; width:100%; margin:4px 0 0 }}
  svg text {{ font:14px system-ui,sans-serif; fill:#435b60 }} .grid {{ stroke:#d0dcd9 }}
  .breadth {{ fill:#4e8b86 }} .narrow {{ fill:#bb5a23 }} .warning {{ stroke:#eec1a6 }}
  polyline {{ fill:none; stroke-width:2; pointer-events:none }} .index {{ stroke:#225395 }} .median {{ stroke:#73409a }}
  .callout {{ background:#fff3e8; border-left:4px solid #bb5a23; padding:12px 15px; font-weight:600 }}
  .table {{ overflow-x:auto; margin:10px 0 }} table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums }}
  th,td {{ text-align:right; padding:8px; border-bottom:1px solid #d8e2e0; white-space:nowrap }}
  th:first-child {{ text-align:left }} thead {{ background:#eff5f5 }} summary {{ cursor:pointer }}
  .note,header p,small {{ color:#425b61 }} small {{ display:block }}
  @media(max-width:580px) {{ main {{ padding:20px 10px }} section {{ padding:12px }} .metrics {{ grid-template-columns:1fr }} }}
</style>
<main>
  <header><h1>A 股市场观察 · 行业广度</h1>
  <p>{first:%Y-%m-%d} 至 {last:%Y-%m-%d} · {len(result)} 个观察周 · 31 个申万一级行业</p></header>
  {sections[0]}
  {risk_comparison(result[4])}
  {"".join(sections[1:])}
  <p class="note">周末以周五标记，采用当周最后一个交易日的收盘。滚动四周按有交易的周计算。图中橙色标记对应各自周期的分化条件，历史风险对比采用滚动四周口径。</p>
</main></html>"""
    target.write_text(content, encoding="utf-8")
