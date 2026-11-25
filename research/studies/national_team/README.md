# national_team · 国家队 ETF 申赎环境监控

跟踪国家队重仓宽基 ETF（按指数组合并）的日度份额变动，输出申赎环境状态与 core 持仓覆盖视图。
只回答「托底买方在不在场」，不回答「国家队今天买了多少」——单日份额变动是全市场申赎合计，
无法精确归因国家队。

## 口径

- 份额：上交所逐日整表（`fund_etf_scale_sse`，T+1 披露）+ 深交所区间批量（`fund_scale_daily_szse`）；
  净值：天天基金 `fund_open_fund_info_em`。净申赎金额 ≈ 当日份额变动 × 单位净值。
- 组级 5 日份额变动阈值（quantdata 描述性口径）：≥ +7.5% `LARGE_IN` / ≥ +2% `ELEVATED` /
  ≤ -2% `REDEEM` / 其余 `FLAT`。
- `stall_streak`（连续停止净申购天数）是核心警报：国家队开始买入不是底部（2026-11-20 单日
  900 亿纪录后指数继续新低），但连续多日零申赎 = 托底缺席、两融负反馈失去缓冲。

## 组成

- `HS300`：510300/510310/510330/159919（汇金系重仓组，2025 年报口径持仓超万亿）
- `SSE50`：510050
- `A500`：563360/159338/159352/159361/563220/159351/563800/512050（A500 家族份额前 8，2026-10 救市新增）
- `CSI1000`：512100/159845/560010/159629
- `CSI500`：510500/512500
- `STAR50`：588000/588080
- `CHINEXT`：159915/159977/159957

## 用法

```bash
# 仓库根目录，项目 conda 解释器
python -m research.tools.national_team.national_team            # 增量更新 + 报告
python -m research.tools.national_team.national_team --start 20260901 --end 20261121
python -m research.tools.national_team.national_team --no-account   # 不关联 core 持仓
python -m research.tools.national_team.national_team --no-fetch     # 只用缓存
```

缓存：`research/data/national_team/{shares,navs}.csv`（长表，增量合并，可删可重建）。

## 边界（读数前必读）

1. 输出是环境状态，不是国家队买卖金额；归因需等基金半年报/年报披露（2026-05 起规则已取消
   ETF 半年报前十大持有人列表，官方持仓披露更滞后）。
2. ETF 分红日份额调整会被误读为申赎；监控池以无分红宽基为主，出现异常尖峰先查分红。
3. 阈值是描述性标签不是信号；本工具的角色是**风险提示**：申购在场时几乎没有决策价值
   （托底在场≠止跌），缺席/净赎回时提示左尾变肥（两融负反馈失去缓冲）。消费方是人，
   不接 rebalancer/执行层——状态转变时人工决定是否调整既定计划的执行节奏。
