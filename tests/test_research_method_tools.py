"""公共研究方法的数值、时间和显式副作用契约。"""
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from research.tools.fundamental_analysis import analyze_fundamentals, get_ttm, get_single_quarter
from research.tools.technical_analysis import analyze_technical
from research.tools.technical_analysis.data import forward_adjust
from research.tools.announcement_analysis import filter_announcements


def test_importing_methods_does_not_import_sdk_or_change_process_settings():
    subprocess.run([sys.executable, '-c', '''
import sys, warnings
paths, filters = list(sys.path), list(warnings.filters)
import pandas, numpy
filters = list(warnings.filters)
from research.tools import fundamental_analysis, technical_analysis, announcement_analysis
assert 'AmazingData' not in sys.modules
assert paths == sys.path
assert filters == warnings.filters
'''], check=True, timeout=45)


def test_cumulative_ttm_and_single_quarter_keep_missing_periods():
    frame = pd.DataFrame({'REPORTING_PERIOD': ['20230331', '20231231', '20240331', '20240930'],
                          'profit': [20., 100., 30., 90.]})
    result = get_ttm(frame, 'profit')
    assert np.isnan(result.iloc[0])
    assert result.iloc[1:3].tolist() == [100., 110.]
    assert np.isnan(result.iloc[3])
    quarters = get_single_quarter(frame, 'profit')
    assert quarters.iloc[2] == 30.
    assert np.isnan(quarters.iloc[3])


def test_size_uses_raw_price_and_share_change_date_without_backfill():
    dates = pd.to_datetime(['2026-01-01', '2026-01-02', '2026-01-05'])
    kline = pd.DataFrame({'kline_time': dates, 'close': [10., 11., 12.]})
    equity = pd.DataFrame({'MARKET_CODE': ['600001.SH'], 'CHANGE_DATE': ['20260102'],
                           'TOT_SHARE': [100.], 'FLOAT_A_SHARE': [60.]})
    result = analyze_fundamentals('600001.SH', None, None, None, kline=kline,
                                 equity_structure=equity, categories='size')['size']
    assert np.isnan(result['总市值'].iloc[0])
    assert result['总市值'].iloc[1:].tolist() == [11000000., 12000000.]
    assert result['流通市值比总市值'].iloc[-1] == .6
    assert kline['close'].tolist() == [10., 11., 12.]


@pytest.fixture
def kline():
    dates = pd.date_range('2025-01-01', periods=100, freq='B')
    price = np.arange(100) * .1 + 10
    return pd.DataFrame({'open': price-.1, 'high': price+1, 'low': price-1,
                         'close': price, 'volume': np.full(100, 1000.)}, index=dates)


def test_technical_returns_full_series_and_preserves_sdk_initial_values(kline):
    result = analyze_technical(kline, indicator='MA')['MA']
    pd.testing.assert_index_equal(result.index, kline.index)
    # SDK MA uses the available initial window, unlike a full-window-only MA.
    assert result['MA5'].iloc[0] == 10.
    assert result['MA5'].iloc[4] == pytest.approx(10.2)
    with pytest.raises(ValueError, match='唯一且升序'):
        analyze_technical(kline.iloc[::-1], indicator='MA')
    with pytest.raises(ValueError, match='只能指定一个'):
        analyze_technical(kline, indicator='MA', category='ma')


def test_all_technical_indicators_include_volume_weighted_xs(kline):
    kline['volume'] = np.arange(len(kline)) * 50. + 1000.
    result = analyze_technical(kline)
    assert len(result) == 56
    xs = result['XS']
    assert xs.notna().all().all()
    np.testing.assert_allclose(xs['SUP'] / xs['SDN'], 1.06/.94)
    def ema(series, span):
        return series.ewm(span=span, adjust=False).mean()
    weighted = sum(ema(kline.close*kline.volume, span) / ema(kline.volume, span)
                   for span in (3, 6, 12, 24)) / 4
    np.testing.assert_allclose(xs['SUP'], ema(weighted, 13)*1.06)


def test_forward_adjust_does_not_use_future_factors_or_adjust_volume(kline):
    factors = pd.DataFrame({'600001.SH': [1., 2., 8.]},
                           index=[kline.index[0], kline.index[50], kline.index[-1]+pd.Timedelta(days=2)])
    adjusted = forward_adjust(kline, factors, '600001.SH')
    assert adjusted['close'].iloc[0] == kline['close'].iloc[0] / 2
    assert adjusted['close'].iloc[-1] == kline['close'].iloc[-1]
    pd.testing.assert_series_equal(adjusted['volume'], kline['volume'])
    with pytest.raises(ValueError, match='缺失或非正'):
        forward_adjust(kline, factors.iloc[1:], '600001.SH')


def test_announcement_prefix_is_literal_and_dates_are_inclusive():
    frame = pd.DataFrame({'TAG_ID': ['999|103010', '110301', '103.1', '103x1', None],
                          'PUBLISH_TIME': ['2026-01-02']*5})
    original = frame.copy(deep=True)
    assert filter_announcements(frame, 20260102, 20260102, tag_ids=['10301']).index.tolist() == [0]
    assert filter_announcements(frame, 20260102, 20260102, tag_ids=['103.']).TAG_ID.tolist() == ['103.1']
    assert filter_announcements(frame, 20260103, 20260104).empty
    with pytest.raises(ValueError, match='TAG_ID'):
        filter_announcements(frame.drop(columns='TAG_ID'), 20260101, 20260103, tag_ids=['103'])
    pd.testing.assert_frame_equal(frame, original)


def test_pdf_conversion_returns_readable_markdown_and_reports_failed_files(tmp_path):
    fitz = pytest.importorskip('fitz')
    from research.tools.announcement_analysis import PdfConverter

    pdf = tmp_path / 'announcement.pdf'
    document = fitz.open()
    page = document.new_page()
    text = 'Annual operating revenue grew during the reporting period. Cash flow remained positive.'
    page.insert_text((72, 72), text)
    document.save(str(pdf))
    document.close()
    output = tmp_path / 'markdown'
    converter = PdfConverter(preferred_engine='fitz', output_dir=str(output))
    converted = converter.convert_batch({'annual': str(pdf)}, max_workers=1)
    assert set(converted) == {'annual'}
    content = (output / 'annual.md').read_text(encoding='utf-8')
    assert 'Annual operating revenue' in content
    assert 'source_id: annual' in content
    assert converter.convert_one(str(tmp_path / 'missing.pdf'), 'missing') is None
    assert str(tmp_path / 'missing.pdf') in converter.get_failed_files()
