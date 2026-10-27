"""Portable file boundaries and the end-to-end holding-attribution pipeline."""
import csv

import pytest

from research.studies.etf_rotation.analyze_positions import analyze
from research.studies.etf_rotation.study_common import read_csv, read_json, write_csv
from research.studies.industry_momentum.study_entry_volatility import reconstruct_mixed, snapshots
from research.studies.etf_rotation.study_etf_fit import run


def test_csv_round_trip_preserves_unicode_quotes_newlines_and_missing_values(tmp_path):
    file = tmp_path / 'rows.csv'
    write_csv(file, [dict(code='黄金,"ETF"\n下一行', value=None, closed=True),
                     dict(code='B', value=1.5, closed=False)])
    assert read_csv(file) == [dict(code='黄金,"ETF"\n下一行', value='', closed='true'),
                              dict(code='B', value='1.5', closed='false')]
    file.write_text('code,value\nA,1,2\n', encoding='utf-8')
    with pytest.raises(ValueError, match='width mismatch'):
        read_csv(file)
    file.write_text('code,value\nA,"unterminated\n', encoding='utf-8')
    with pytest.raises(csv.Error):
        read_csv(file)


@pytest.fixture
def history_file(tmp_path):
    file = tmp_path / 'positions.csv'
    header = ['日期', '', '', '', '', '', '', '', '', '', '', '', '当日盈亏', '', '', '']
    rows = [header]
    for day, code, value, pnl, cash, nav in [
            ('2020-01-01', '510300.XSHG', 103000, 3000, -20, 102980),
            ('2020-01-02', '159915.XSHE', 99000, -1000, 4939, 103939)]:
        position = [''] * 17
        position[0], position[2], position[4] = day, f'标的({code})', '10000股'
        for index, number in [(7, value), (9, 10), (12, pnl), (15, nav)]:
            position[index] = str(number)
        cash_row = [''] * 16
        cash_row[0], cash_row[2], cash_row[7] = day, 'Cash', str(cash)
        rows.extend([position, cash_row])
    with file.open('w', encoding='gb18030', newline='') as stream:
        csv.writer(stream).writerows(rows)
    return file


def test_attribution_and_fit_pipeline_reconcile_costs_and_exclude_open_segments(history_file, tmp_path):
    result = analyze(history_file, include_episodes=True)
    assert result['netProfit'] == 3939
    assert result['closedTrades'] == 1 and result['winners'] == 1
    assert result['episodes'][0]['netPnl'] == 4959
    assert result['episodes'][1]['netPnl'] == -1020
    assert result['checks']['netAttributionError'] == 0
    assert result['checks']['episodeCashflowErrorMax'] == 0
    mixed = reconstruct_mixed(snapshots(history_file))
    assert [row['net_pnl'] for row in mixed['rows']] == [4959, -1020]
    output = tmp_path / 'fit'
    fit = run(history_file, output)
    assert fit['closedEpisodes'] == 1 and fit['openEpisodes'] == 1
    assert read_json(output / 'fit_method.json')['reconciliation'] == result['checks']
    assert len(read_csv(output / 'fit_episodes.csv')) == 2
    summaries = {row['code']: row for row in read_csv(output / 'fit_summary.csv')}
    assert summaries['510300.XSHG']['closedEpisodes'] == '1'
    assert summaries['159915.XSHE']['closedEpisodes'] == '0'
    assert summaries['159915.XSHE']['meanReturn'] == ''
