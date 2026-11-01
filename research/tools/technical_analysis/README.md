# 技术指标

输入同一价格口径的 OHLCV，返回完整指标时序。保留官方示例的 56 个指标和默认参数；单项计算时才加载 AmazingData 运算符，不登录行情服务。

```python
from research.tools.technical_analysis import analyze_technical, TechnicalIndicators
from research.tools.technical_analysis.data import fetch_kline

result = analyze_technical(kline, indicator='MACD')
macd = result['MACD']  # DataFrame，保留 kline 的索引
atr = TechnicalIndicators.ATR(kline.close, kline.high, kline.low, n=20)

# 联网及复权是显式步骤，已有行情时不需要调用。
kline = fetch_kline('600001.SH', 20240101, 20261030, adjust='pre')
trends = analyze_technical(kline, category='trend')
```

`analyze_technical` 返回 `{指标名: DataFrame}`，`indicator` 和 `category` 二选一，均不指定时计算全部。类别为 `overbought_oversold/trend/energy/volume/ma/path/other`。输入需包含 `open/high/low/close/volume`，索引唯一、升序；缺少 `amount` 时沿用 `close * volume` 的示例估算，调用方需保证单位匹配。

计算不自动排序、不填补缺失价格、不截取末值、不生成买卖信号。保留 SDK 初始期计算规则，例如 MA 用当前可用窗口计算初值。异常直接抛出，不以空结果替代失败。

`data.fetch_kline` 支持显式 `raw/pre`；前复权按区间内末日因子归一化，仅调整 OHLC，不调整量额。缺失或非正因子会报错。市值计算和成交模拟应使用未复权价格。

`XS` 使用成交量加权 EMA，接口为 `XS(close, volume, n=13)`，修复了上游示例遗漏 `volume` 输入的问题。其余指标公式与原示例保持一致。

实现集中在 `indicators.py`，类别选择在 `analysis.py`，行情准备在 `data.py`。参见 [指标公式](references/technical_analysis_guide.md) 和 [上游声明](../references/upstream_notice.md)。
