"""Shared file and numeric helpers for the offline ETF studies."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent


def number(value):
    if value is None or value == '':
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def positive(value):
    parsed = number(value)
    return parsed is not None and parsed > 0


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def quantile(values, probability):
    values = sorted(x for x in values if isinstance(x, (int, float)) and math.isfinite(x))
    if not values:
        return None
    at = (len(values) - 1) * probability
    lo, hi = math.floor(at), math.ceil(at)
    return values[lo] + (values[hi] - values[lo]) * (at - lo)


def groups(rows, keys):
    result = defaultdict(list)
    for row in rows:
        result[tuple(row.get(key) for key in keys)].append(row)
    return list(result.values())


def counts_by(rows, keys):
    return {'|'.join(str(items[0].get(key, '')) for key in keys): len(items)
            for items in groups(rows, keys)}


def csv_fields(file, encoding='utf-8-sig'):
    with Path(file).open(encoding=encoding, newline='') as stream:
        return [row for row in csv.reader(stream, strict=True) if any(value != '' for value in row)]


def read_csv(file):
    rows = csv_fields(file)
    if not rows:
        raise ValueError(f'Empty CSV: {file}')
    header, *values = rows
    if any(len(row) != len(header) for row in values):
        raise ValueError(f'CSV width mismatch: {file}')
    return [dict(zip(header, row)) for row in values]


def write_csv(file, rows, *, union=False):
    file = Path(file)
    if not rows:
        file.write_text('', encoding='utf-8')
        return
    keys = list(dict.fromkeys(key for row in rows for key in row)) if union else list(rows[0])
    with file.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({key: (str(value).lower() if isinstance(value, bool) else value)
                             for key, value in row.items()})


def json_text(value):
    # JavaScript's JSON writer emitted non-finite ratios as null. Preserve that
    # output contract for empty/flat histories, without writing invalid JSON.
    def clean(item):
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, dict):
            return {key: clean(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(value) for value in item]
        return item
    return json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def read_json(file):
    return json.loads(Path(file).read_text(encoding='utf-8'))


def write_json(file, value):
    Path(file).write_text(json_text(value), encoding='utf-8')


def source_hashes(files, *, base=HERE):
    return [{'file': Path(os.path.relpath(file, base)).as_posix(),
             'sha256': hashlib.sha256(Path(file).read_bytes()).hexdigest()} for file in files]


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def fee(notional, commission=.0002):
    return math.floor(max(5, notional * commission) * 100 + .5) / 100
