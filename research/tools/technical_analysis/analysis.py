"""对已准备好的同口径 OHLCV 计算指标面板，不获取或调整价格。"""
import pandas as pd
from .indicators import TechnicalIndicators


def _registry(df):
    close, open_, high, low, volume = (df[c] for c in ("close", "open", "high", "low", "volume"))
    amount = df["amount"] if "amount" in df else close * volume
    TI = TechnicalIndicators
    overbought_indicators = [
        ("KDJ", lambda: TI.KDJ(close, high, low)),
        ("RSI", lambda: TI.RSI(close)),
        ("WR", lambda: TI.WR(close, high, low)),
        ("CCI", lambda: TI.CCI(close, high, low)),
        ("ROC", lambda: TI.ROC(close)),
        ("MTM", lambda: TI.MTM(close)),
        ("BIAS", lambda: TI.BIAS(close)),
        ("SKDJ", lambda: TI.SKDJ(close, high, low)),
        ("MFI", lambda: TI.MFI(close, high, low, volume)),
        ("OSC", lambda: TI.OSC(close)),
        ("UDL", lambda: TI.UDL(close)),
        ("ACCER", lambda: TI.ACCER(close)),
        ("RCCD", lambda: TI.RCCD(close)),
        ("MARSI", lambda: TI.MARSI(close)),
    ]

    trend_indicators = [
        ("MACD", lambda: TI.MACD(close)),
        ("DMI", lambda: TI.DMI(close, high, low)),
        ("DMA", lambda: TI.DMA(close)),
        ("TRIX", lambda: TI.TRIX(close)),
        ("ARBR", lambda: TI.ARBR(close, open_, high, low)),
        ("EMV", lambda: TI.EMV(close, high, low, volume)),
        ("DPO", lambda: TI.DPO(close)),
        ("VHF", lambda: TI.VHF(close)),
        ("CHO", lambda: TI.CHO(close, high, low, volume)),
        ("DBCD", lambda: TI.DBCD(close)),
        ("DDI", lambda: TI.DDI(close, high, low)),
        ("JS", lambda: TI.JS(close)),
        ("QACD", lambda: TI.QACD(close)),
        ("UOS", lambda: TI.UOS(close, high, low)),
    ]

    energy_indicators = [
        ("CR", lambda: TI.CR(close, high, low)),
        ("PSY", lambda: TI.PSY(close)),
        ("MASS", lambda: TI.MASS(high, low)),
        ("PCNT", lambda: TI.PCNT(close)),
        ("WAD", lambda: TI.WAD(close, high, low)),
    ]

    volume_indicators = [
        ("OBV", lambda: TI.OBV(close, volume)),
        ("VR", lambda: TI.VR(close, volume)),
        ("VOLMA", lambda: TI.VOLMA(volume)),
        ("WVAD", lambda: TI.WVAD(close, open_, high, low, volume)),
        ("VOSC", lambda: TI.VOSC(volume)),
        ("VRSI", lambda: TI.VRSI(volume)),
        ("VSTD", lambda: TI.VSTD(volume)),
        ("AMO", lambda: TI.AMO(amount)),
        ("TAPI", lambda: TI.TAPI(close, amount)),
    ]

    ma_indicators = [
        ("MA", lambda: TI.MA(close)),
        ("EXPMA", lambda: TI.EXPMA(close)),
        ("BBI", lambda: TI.BBI(close)),
        ("AMV", lambda: TI.AMV(volume, amount)),
    ]

    path_indicators = [
        ("BOLL", lambda: TI.BOLL(close)),
        ("ENE", lambda: TI.ENE(close)),
        ("MIKE", lambda: TI.MIKE(close, high, low)),
        ("PBX", lambda: TI.PBX(close)),
        ("XS", lambda: TI.XS(close, volume)),
        ("BBIBOLL", lambda: TI.BBIBOLL(close)),
    ]

    other_indicators = [
        ("ASI", lambda: TI.ASI(close, open_, high, low)),
        ("ATR", lambda: TI.ATR(close, high, low)),
        ("SAR", lambda: TI.SAR(close, high, low)),
        ("CDP", lambda: TI.CDP(close, high, low)),
    ]

    all_categories = {
        "overbought_oversold": ("一、超买超卖型", overbought_indicators),
        "trend": ("二、趋势型", trend_indicators),
        "energy": ("三、能量型", energy_indicators),
        "volume": ("四、成交量型", volume_indicators),
        "ma": ("五、均线型", ma_indicators),
        "path": ("六、路径型", path_indicators),
        "other": ("七、其他型", other_indicators),
    }

    return {key: {name: calculate for name, calculate in indicators}
            for key, (_, indicators) in all_categories.items()}


def analyze_technical(kline, *, indicator=None, category=None):
    """返回 {指标名: DataFrame}，保留完整输入索引及 SDK 初始期计算规则。

    indicator 和 category 二选一；不传时计算全部。计算失败直接抛出异常。
    可通过 TechnicalIndicators 的单项方法显式设置窗口参数。
    """
    if not isinstance(kline, pd.DataFrame) or kline.empty:
        raise ValueError("kline 必须是非空 DataFrame")
    if not kline.index.is_unique or not kline.index.is_monotonic_increasing:
        raise ValueError("kline 索引必须唯一且升序")
    if indicator is not None and category is not None:
        raise ValueError("indicator 和 category 只能指定一个")
    registry = _registry(kline)
    if category is not None:
        if category not in registry:
            raise ValueError(f"未知技术指标类别: {category}")
        selected = registry[category]
    else:
        selected = {name: calculate for group in registry.values() for name, calculate in group.items()}
    if indicator is not None:
        name = indicator.upper()
        if name not in selected:
            raise ValueError(f"未知技术指标: {indicator}")
        selected = {name: selected[name]}
    return {name: pd.DataFrame(calculate(), index=kline.index) for name, calculate in selected.items()}
