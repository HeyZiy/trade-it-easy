"""可选的 AmazingData 分析函数；调用时加载 SDK，保留 SDK 自己的数据口径。

这些扩展不构成默认研究流程，不覆盖 core 的 IC、分组和统计结果。
"""
from importlib import import_module


def _sdk():
    try:
        return import_module('AmazingData.factor_analysis')
    except ImportError as exc:
        raise RuntimeError('此扩展需要安装 AmazingData；公共 IC/分组分析不需要该 SDK') from exc


def preprocess(factor, **parameters):
    """参数直接对应官方 run_pipeline，调用方明确选择截断、标准化和补缺规则。"""
    return _sdk().FactorPreProcessing(factor).run_pipeline(**parameters)


def collinearity(factors):
    return _sdk().CollinearityAnalysis(factors).cal_collinearity().summary()


def orthogonalize(factors, method='symmetric'):
    return _sdk().FactorOrthogonalization(factors).cal_orthogonalization(method=method)


def weight_factors(factors, *, method='equal', factor_ic=None, factor_return=None,
                   window=20, half_life=20, weight_normalization=True):
    """收益/IC 驱动权重的训练数据和可见时间由调用方负责，不自动生成。"""
    if method != 'equal' and factor_ic is None and factor_return is None:
        raise ValueError('非等权方法需要调用方提供已核对时点的 IC 或因子收益')
    return _sdk().FactorWeighting(factors).weighting(
        method, factor_ic=factor_ic, factor_return=factor_return,
        window=window, half_life=half_life, weight_normalization=weight_normalization)


def score_stocks(factors, weights):
    return _sdk().StockScorer(factors).score(weights)


def regression(factor, factor_name, close_price, benchmark=None, *, nlags=10):
    result = _sdk().RegressionAnalysis(factor, factor_name, close_price, benchmark)
    result.cal_factor_return()
    result.cal_t_value_statistics()
    result.cal_net_analysis()
    result.cal_acf(nlags=nlags)
    return result


def stratification(factor, close_price, *, groups=5, ascending=True,
                   benchmark=None, market_cap=None, decay_periods=10):
    """独立 SDK 扩展，使用 SDK 的价格/持有期约定，不冒充显式端点收益诊断。"""
    return _sdk().StratificationAnalysis(factor, close_price, groups, ascending, benchmark).run(
        market_cap=market_cap, decay_periods=decay_periods)


def crowding(factor, close_price, market_cap=None, *, groups=5, ascending=False, window=60):
    result = _sdk().FactorCrowdingAnalysis(factor, close_price, market_cap, groups, ascending)
    series = result.calc_all(window)
    return series, result.crowding_summary()
