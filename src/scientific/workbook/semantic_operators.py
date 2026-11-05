from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_UP
import calendar
from math import isfinite
import re


OPERATORS = {'is_number', 'is_text', 'ltrim', 'after_colon', 'strip_iso_time', 'first_last_name',
             'to_text', 'date_text', 'coalesce_unique', 'count_matches', 'sum_matches', 'max_product'}
OPERATORS |= {'sum_month', 'length', 'left', 'right', 'mid', 'find', 'search', 'roundup', 'year', 'month', 'day', 'date', 'eomonth'}
OPERATORS |= {'matrix_sum', 'join_dates'}


def _error(reason):
    from .semantic_compiler import SemanticCompilationError
    raise SemanticCompilationError(reason)


def equal(left, right):
    if left in (None, '') or right in (None, ''):
        return left in (None, '') and right in (None, '')
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left.casefold() == right.casefold()
    return type(left) is type(right) and left == right or type(left) in (int, float) and type(right) in (int, float) and left == right


def qualified_criteria(array,key):
    if isinstance(key,datetime) and any(type(v) in (int,float) for v in array) or type(key) in (int,float) and any(isinstance(v,datetime) for v in array):
        _error('TYPED_CRITERIA_DATE_MISMATCH')
    if type(key) is bool and any(type(v) in (int,float) for v in array) or type(key) in (int,float) and any(type(v) is bool for v in array):
        _error('TYPED_CRITERIA_BOOLEAN_MISMATCH')


def criteria_formula(array,key):
    return 'IF(' + key + '="",--(' + array + '=""),--(' + array + '=' + key + ')*--NOT(ISBLANK(' + array + ')))'


def date_text(value, source, target):
    if not isinstance(value, str):
        _error('TYPED_DATE_TEXT_SOURCE_REQUIRED')
    if source == 'yyyy.mm.dd':
        match = re.fullmatch(r'(\d{4})\.(\d{2})\.(\d{2})(?: .*)?', value)
        if not match:
            _error('INVALID_DATE_TEXT')
        parts = [int(x) for x in match.groups()]
    elif source == 'yyyymmdd':
        if not re.fullmatch(r'\d{8}', value):
            _error('INVALID_DATE_TEXT')
        parts = [int(value[:4]), int(value[4:6]), int(value[6:])]
    elif source == 'Day, dd Month yyyy':
        match = re.fullmatch(r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (\d{1,2}) ([A-Za-z]+) (\d{4})', value, re.I)
        months = {name.lower(): i for i, name in enumerate(('January','February','March','April','May','June','July','August','September','October','November','December'), 1)}
        if not match or match[3].lower() not in months:
            _error('DATE_LOCALE_NOT_QUALIFIED')
        parts = [int(match[4]), months[match[3].lower()], int(match[2])]
    else:
        _error('UNSUPPORTED_DATE_TEXT_FORMAT')
    try:
        parsed = datetime(*parts)
    except ValueError:
        _error('INVALID_CALENDAR_DATE')
    if source == 'Day, dd Month yyyy' and match[1].lower() != ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')[parsed.weekday()]:
        _error('INCONSISTENT_WEEKDAY_DATE')
    formats = {'dd/mm/yyyy': f'{parsed.day:02d}/{parsed.month:02d}/{parsed.year:04d}',
               'dd-mm-yyyy': f'{parsed.day:02d}-{parsed.month:02d}-{parsed.year:04d}',
               'yyyy mm dd': f'{parsed.year:04d} {parsed.month:02d} {parsed.day:02d}'}
    if target not in formats:
        _error('UNSUPPORTED_DATE_TEXT_FORMAT')
    return formats[target]


def evaluate(operator, values):
    if operator == 'is_number':
        return type(values[0]) in (int, float) and isfinite(values[0])
    if operator == 'is_text':
        return isinstance(values[0], str)
    if operator == 'to_text':
        value = values[0]
        if isinstance(value, str):
            return value
        if type(value) not in (int, float) or not isfinite(value):
            _error('TEXT_CONVERSION_SOURCE_NOT_QUALIFIED')
        text = format(Decimal(str(value)), 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.')
        if len(text.replace('.', '').replace('-', '').lstrip('0')) > 15:
            _error('TEXT_CONVERSION_PRECISION_NOT_QUALIFIED')
        return text
    if operator == 'coalesce_unique':
        candidates = [v for v in values[:-1] if v not in (None, '')]
        unique = []
        for value in candidates:
            if not any(type(value) is type(old) and value == old for old in unique):
                unique.append(value)
        if len(unique) > 1:
            _error('AMBIGUOUS_NONBLANK_ALTERNATIVES')
        return unique[0] if unique else values[-1]
    if operator == 'sum_month':
        amounts, dates, month = values
        if not isinstance(month, datetime) or not isinstance(amounts, list) or not isinstance(dates, list) or len(amounts) != len(dates):
            _error('TYPED_MONTH_SUM_DOMAIN_REQUIRED')
        if any(v is not None and not isinstance(v, datetime) for v in dates):
            _error('TYPED_MONTH_SUM_DATE_REQUIRED')
        return sum(a or 0 for a, d in zip(amounts, dates) if d is not None and (d.year, d.month) == (month.year, month.month))
    if operator == 'matrix_sum':
        amounts, first, key1, second, key2, headers, key3 = values
        if not all(isinstance(v, list) for v in (amounts, first, second, headers)) or len(first) != len(second) or len(amounts) != len(first) * len(headers):
            _error('MATRIX_CRITERIA_NOT_ALIGNED')
        for array,key in ((first,key1),(second,key2),(headers,key3)):
            qualified_criteria(array,key)
        return sum(amounts[r * len(headers) + c] or 0 for r in range(len(first)) for c in range(len(headers)) if equal(first[r], key1) and equal(second[r], key2) and equal(headers[c], key3))
    if operator == 'join_dates':
        key, keys, dates, delimiter, fmt = values
        if not isinstance(keys, list) or not isinstance(dates, list) or len(keys) != len(dates):
            _error('DATE_LOOKUP_RANGES_NOT_ALIGNED')
        selected = []
        for candidate, value in zip(keys, dates):
            if equal(candidate, key) and value not in (None, ''):
                if not isinstance(value, datetime) or value.time().isoformat() != '00:00:00':
                    _error('DATE_JOIN_GRANULARITY_NOT_QUALIFIED')
                if value not in selected:
                    selected.append(value)
        def formatted(d):
            return {'mm-dd-yy':f'{d.month:02d}-{d.day:02d}-{d.year%100:02d}',
                    'dd/mm/yyyy':f'{d.day:02d}/{d.month:02d}/{d.year:04d}',
                    'dd-mm-yyyy':f'{d.day:02d}-{d.month:02d}-{d.year:04d}',
                    'yyyy-mm-dd':f'{d.year:04d}-{d.month:02d}-{d.day:02d}',
                    'm/d/yyyy':f'{d.month}/{d.day}/{d.year:04d}',
                    'm/d/yy':f'{d.month}/{d.day}/{d.year%100:02d}'}[fmt]
        if fmt not in {'mm-dd-yy','dd/mm/yyyy','dd-mm-yyyy','yyyy-mm-dd','m/d/yyyy','m/d/yy'}:
            _error('UNSUPPORTED_DATE_TEXT_FORMAT')
        return delimiter.join(formatted(d) for d in selected)
    if operator in {'count_matches', 'sum_matches'}:
        conditions = values[1:] if operator == 'sum_matches' else values
        arrays = conditions[::2]
        if any(not isinstance(v, list) for v in arrays) or len({len(v) for v in arrays}) != 1:
            _error('CRITERIA_RANGES_NOT_ALIGNED')
        if operator == 'sum_matches' and (not isinstance(values[0], list) or len(values[0]) != len(arrays[0])):
            _error('SUM_RANGE_NOT_ALIGNED')
        for array,key in zip(arrays,conditions[1::2]):
            qualified_criteria(array,key)
        selected = [i for i in range(len(arrays[0])) if all(equal(array[i], key) for array, key in zip(arrays, conditions[1::2]))]
        return sum(values[0][i] or 0 for i in selected) if operator == 'sum_matches' else len(selected)
    if operator == 'max_product':
        if not all(isinstance(v, list) for v in values) or len(values[0]) != len(values[1]) or not values[0]:
            _error('PRODUCT_RANGES_NOT_ALIGNED')
        if any(v is None for a in values for v in a):
            _error('UNSPECIFIED_BLANK_PRODUCT')
        return max(a * b for a, b in zip(*values))
    if any(v is None for v in values):
        _error('UNSPECIFIED_BLANK_SOURCE')
    if operator in {'year', 'month', 'day', 'eomonth'}:
        if not isinstance(values[0], datetime):
            _error('TYPED_CALENDAR_SOURCE_REQUIRED')
        if operator != 'eomonth':
            return getattr(values[0], operator)
        offset = values[1]
        if type(offset) not in (int, float) or int(offset) != offset:
            _error('INTEGER_MONTH_OFFSET_REQUIRED')
        index = values[0].year * 12 + values[0].month - 1 + int(offset)
        year, month = index // 12, index % 12 + 1
        if not 1 <= year <= 9999:
            _error('CALENDAR_RANGE_EXCEEDED')
        return datetime(year, month, calendar.monthrange(year, month)[1])
    if operator == 'date':
        if any(type(v) not in (int, float) or int(v) != v for v in values):
            _error('INTEGER_DATE_COMPONENTS_REQUIRED')
        try:
            return datetime(*(int(v) for v in values))
        except ValueError:
            _error('INVALID_CALENDAR_DATE')
    if operator == 'roundup':
        if type(values[1]) is not int or not -15 <= values[1] <= 15:
            _error('INVALID_ROUND_PRECISION')
        try:
            return float(Decimal(str(values[0])).quantize(Decimal(1).scaleb(-values[1]), rounding=ROUND_UP))
        except ArithmeticError:
            _error('UNQUALIFIED_ROUND_DOMAIN')
    if operator == 'length':
        return len(values[0])
    if operator in {'left', 'right', 'mid'}:
        if any(type(v) not in (int, float) or int(v) != v for v in values[1:]) or any(v < 0 for v in values[1:]) or operator == 'mid' and values[1] == 0:
            _error('INVALID_SUBSTRING_ARGUMENT')
        if operator == 'left':
            return values[0][:int(values[1])]
        if operator == 'right':
            return values[0][-int(values[1]):] if values[1] else ''
        return values[0][int(values[1]) - 1:int(values[1]) - 1 + int(values[2])]
    if operator in {'find', 'search'}:
        start = values[2] if len(values) == 3 else 1
        if type(start) not in (int, float) or int(start) != start or not 1 <= start <= len(values[1]) + 1:
            _error('INVALID_SEARCH_START')
        if operator == 'search' and any(c in values[0] for c in '*?~'):
            _error('SEARCH_PATTERN_NOT_QUALIFIED')
        needle, source = values[0], values[1]
        if operator == 'search':
            needle, source = needle.lower(), source.lower()
        index = source.find(needle, int(start) - 1)
        if index < 0:
            _error('DELIMITER_NOT_FOUND')
        return index + 1
    if operator == 'ltrim':
        return values[0].lstrip(' ')
    if operator == 'after_colon':
        if ':' not in values[0]:
            _error('DELIMITER_NOT_FOUND')
        return values[0].split(':', 1)[1]
    if operator == 'strip_iso_time':
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', values[0]):
            try:
                datetime.fromisoformat(values[0])
            except ValueError:
                _error('INVALID_ISO_TIMESTAMP')
            return values[0]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', values[0]):
            _error('INVALID_ISO_TIMESTAMP')
        try:
            datetime.fromisoformat(values[0].replace('Z', '+00:00'))
        except ValueError:
            _error('INVALID_ISO_TIMESTAMP')
        return values[0][:10]
    if operator == 'date_text':
        return date_text(*values)
    if operator == 'first_last_name':
        parts = values[0].split()
        if len(parts) != 3 or ' '.join(parts) != values[0] or not re.fullmatch(r'[^@\s]+@[^@\s]+', parts[2]):
            _error('NAME_EMAIL_DOMAIN_NOT_QUALIFIED')
        return ' '.join(parts[:2])
    _error('UNSUPPORTED_TYPED_OPERATOR')


def formula(operator, args):
    if operator in {'left','right','mid','find','search','year','month','day','date','eomonth','roundup'}:
        return operator.upper() + '(' + ','.join(args) + ')'
    if operator == 'length':
        return 'LEN(' + args[0] + ')'
    if operator == 'sum_month':
        return 'SUMIFS(' + args[0] + ',' + args[1] + ',">="&DATE(YEAR(' + args[2] + '),MONTH(' + args[2] + '),1),' + args[1] + ',"<"&EOMONTH(' + args[2] + ',0)+1)'
    if operator == 'matrix_sum':
        return 'SUMPRODUCT(' + '*'.join(criteria_formula(args[i],args[i+1]) for i in (1,3,5)) + '*' + args[0] + ')'
    if operator == 'join_dates':
        return 'TEXTJOIN(' + args[3] + ',TRUE,UNIQUE(IF(' + args[1] + '=' + args[0] + ',TEXT(' + args[2] + ',' + args[4] + '),"")))'
    if operator in {'is_number', 'is_text'}:
        return ('ISNUMBER' if operator == 'is_number' else 'ISTEXT') + '(' + args[0] + ')'
    if operator == 'after_colon':
        return 'MID(' + args[0] + ',FIND(":",' + args[0] + ')+1,LEN(' + args[0] + '))'
    if operator == 'strip_iso_time':
        return 'LEFT(' + args[0] + ',10)'
    if operator == 'first_last_name':
        return 'LEFT(' + args[0] + ',FIND(" ",' + args[0] + ',FIND(" ",' + args[0] + ')+1)-1)'
    if operator == 'to_text':
        return '(' + args[0] + '&"")'
    if operator == 'ltrim':
        return 'IF(TRIM(' + args[0] + ')="","",REPLACE(' + args[0] + ',1,FIND(LEFT(TRIM(' + args[0] + '),1),' + args[0] + ')-1,""))'
    if operator == 'coalesce_unique':
        result = args[-1]
        for value in reversed(args[:-1]):
            result = 'IF(' + value + '<>"",' + value + ',' + result + ')'
        return result
    if operator in {'count_matches', 'sum_matches'}:
        conditions = args[1:] if operator == 'sum_matches' else args
        parts = [criteria_formula(a,k) for a,k in zip(conditions[::2],conditions[1::2])]
        return 'SUMPRODUCT(' + ','.join(parts + ([args[0]] if operator == 'sum_matches' else [])) + ')'
    if operator == 'max_product':
        return 'MAX(INDEX(' + args[0] + '*' + args[1] + ',0))'
    _error('EXECUTOR_FORMULA_LOWERING_UNAVAILABLE:' + operator)
