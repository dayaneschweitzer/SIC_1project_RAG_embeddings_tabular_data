from __future__ import annotations

import re

from .semantic_ir import Goal, IRValidationError, Reference, ValueType as T, WriteOperation as W, call, literal, reference


Q = r'''(?:"[^"\n]*"|'[^'\n]*')'''
COL = r'[A-Z]{1,3}'
CELL = rf'{COL}[1-9]\d*'
NUMBER = r'-?\d+(?:\.\d+)?'


def operation_clauses(text):
    cuts, quote, depth = [], None, 0
    for match in re.finditer(r"""["'()]|\s+(?:and then|then|and)\s+(?=(?:write|put|set|delete|remove|insert|copy|duplicate|unmerge|sort|convert|change)\b)""", text, re.I):
        value = match[0]
        if value in "\"'":
            if quote == value:
                quote = None
            elif quote is None and (match.start() == 0 or not text[match.start() - 1].isalnum()):
                quote = value
        elif quote is None and value == '(':
            depth += 1
        elif quote is None and value == ')':
            depth -= 1
        elif quote is None and depth == 0:
            cuts.append(match)
    if not cuts:
        return [(0, len(text), text)]
    starts = [0] + [m.end() for m in cuts]
    ends = [m.start() for m in cuts] + [len(text)]
    return [(a, b, text[a:b]) for a, b in zip(starts, ends)]


def condition(text, start=None):
    text = text.strip().strip('.')
    quoted = [(m.start(), m.end()) for m in re.finditer(Q, text)]
    depths, depth = [], 0
    for index, char in enumerate(text):
        depths.append(depth)
        if not any(a <= index < b for a, b in quoted):
            depth += (char == '(') - (char == ')')
            if depth < 0:
                raise IRValidationError('UNBALANCED_CONDITION')
    if depth:
        raise IRValidationError('UNBALANCED_CONDITION')
    if text.startswith('(') and text.endswith(')') and all(v > 0 for v in depths[1:]):
        return condition(text[1:-1], start)
    for separator, op in ((r'\s+or\s+', 'or'), (r'\s+and\s+', 'and')):
        cuts = [m for m in re.finditer(separator, text, re.I)
                if not any(a <= m.start() < b for a, b in quoted)
                and depths[m.start()] == 0
                and not text[:m.start()].lower().endswith('column')
                and not (op == 'or' and text[:m.start()].lower().endswith(('less than', 'greater than')) and text[m.end():].lower().startswith('equal to '))]
        if cuts:
            bounds = [(0, cuts[0].start())] + [(a.end(), b.start()) for a, b in zip(cuts, cuts[1:])] + [(cuts[-1].end(), len(text))]
            return call(op, *(condition(text[a:b], start) for a, b in bounds))
    match = re.fullmatch(rf'(?:the )?(?:cell in )?column (?P<col>{COL}) (?P<test>is blank|is not blank|is numeric|contains a number|is text|is not numeric)', text, re.I)
    if match:
        ref = reference(match['col'].upper(), T.SCALAR, start_row=start)
        test = match['test'].lower()
        expr = call('is_blank' if 'blank' in test else 'is_text' if test.endswith('text') else 'is_number', ref)
        return call('not', expr) if 'not' in test else expr
    match = re.fullmatch(rf'(?:the )?(?:cell in )?column (?P<col>{COL}) (?P<test>equals|is|=|<>|!=|>=|<=|>|<|is less than or equal to|is greater than or equal to|is less than|is greater than) (?P<value>{Q}|{NUMBER})', text, re.I)
    if match:
        names = {'equals':'eq','is':'eq','=':'eq','<>':'ne','!=':'ne','>=':'ge','<=':'le','>':'gt','<':'lt',
                 'is less than or equal to':'le','is greater than or equal to':'ge','is less than':'lt','is greater than':'gt'}
        raw = match['value']
        op = names[match['test'].lower()]
        value = raw[1:-1] if raw[:1] in "\"'" else float(raw)
        return call(op, reference(match['col'].upper(), T.SCALAR if op in {'eq','ne'} else T.NUMBER, start_row=start), literal(value))
    match = re.fullmatch(rf'column (?P<col>{COL}) (?P<negative>does not contain|contains) (?P<value>{Q})', text, re.I)
    if match:
        col = match['col'].upper()
        found = call('contains', reference(col, T.TEXT, start_row=start), literal(match['value'][1:-1]))
        expr = call('if', call('is_text', reference(col, T.SCALAR, start_row=start)), found, literal(False))
        return call('not', expr) if match['negative'].lower().startswith('does') else expr
    raise IRValidationError('UNRESOLVED_COMPOSITIONAL_CONDITION')


def parse_frontend(body, identifier, span, sheet):
    def goal(expression, target, operation=W.FORMULA, start=None, end=None, target_sheet=sheet, predicate=None):
        return [Goal(identifier, operation, Reference(target, expression.value_type if operation in {W.VALUE,W.FORMULA} else T.SCALAR, target_sheet, start, end),
                     expression, span, predicate=predicate)]
    direct = re.fullmatch(rf'(?:set|write|put) (?P<target>{CELL}) (?:to|=) (?P<value>{Q}|{NUMBER})', body, re.I)
    if direct:
        raw = direct['value']
        return goal(literal(raw[1:-1] if raw[:1] in "\"'" else float(raw)), direct['target'].upper(), W.VALUE)
    labeled_sum = re.fullmatch(rf'(?:write|put) (?:the )?sum of column (?P<value>{Q}) where column (?P<key>{Q}) equals (?P<criterion>{Q}|{NUMBER}) (?:in|into) (?P<target>{CELL})', body, re.I)
    if labeled_sum:
        raw = labeled_sum['criterion']
        expr = call('sum_matches',reference('@header:' + labeled_sum['value'][1:-1],T.NUMBERS),
                    reference('@header:' + labeled_sum['key'][1:-1],T.SCALARS),literal(raw[1:-1] if raw[:1] in "\"'" else float(raw)))
        return goal(expr,labeled_sum['target'].upper())
    labeled_lookup = re.fullmatch(rf'look up (?P<key>{CELL}) exactly in column (?P<keys>{Q}) and return (?:the corresponding )?(?P<kind>number|text) from column (?P<values>{Q}) (?:in|into) (?P<target>{CELL})',body,re.I)
    if labeled_lookup:
        expr=call('lookup_exact',reference(labeled_lookup['key'].upper(),T.SCALAR),
                  reference('@header:' + labeled_lookup['keys'][1:-1],T.SCALARS),
                  reference('@header:' + labeled_lookup['values'][1:-1],T.TEXTS if labeled_lookup['kind'].lower()=='text' else T.NUMBERS))
        return goal(expr,labeled_lookup['target'].upper())
    dates = re.fullmatch(rf'convert dates in column (?P<source>{COL}) from (?P<before>{Q}) to (?P<after>{Q}) (?:(?P<inplace>in place)|(?:in|into) column (?P<target>{COL}))',body,re.I)
    if dates:
        expr=call('date_text',reference(dates['source'].upper(),T.TEXT),literal(dates['before'][1:-1]),literal(dates['after'][1:-1]))
        return goal(expr,(dates['target'] or dates['source']).upper(),W.VALUE)
    identifiers = re.fullmatch(rf'fill missing identifiers in column (?P<key>{COL}) using alphabetic suffixes from the previous identifier, using column (?P<data>{COL}) as record domain',body,re.I)
    if identifiers:
        key=reference(identifiers['key'].upper(),T.SCALAR);data=reference(identifiers['data'].upper(),T.SCALAR)
        return goal(call('urn_next',key,data),identifiers['key'].upper(),W.VALUE,
                    predicate=call('and',call('is_blank',key),call('not',call('is_blank',data))))
    deletion = re.fullmatch(r'(?:delete|remove) (?:all )?rows (?:where|if) (?P<condition>.+?)(?:, starting (?:at|from) row (?P<start>[1-9]\d*))?', body, re.I)
    if deletion:
        start = int(deletion['start']) if deletion['start'] else None
        expr = condition(deletion['condition'], start)
        anchor = expr.references()[0].address
        return goal(expr, anchor, W.DELETE_ROW, start)
    blanks = re.fullmatch(r'(?:delete|remove) (?:all )?blank rows', body, re.I)
    if blanks:
        return goal(call('row_blank'), 'A', W.DELETE_ROW)
    duplicate = re.fullmatch(rf'(?:delete|remove) duplicate (?:rows|records) (?:based on|by) columns? (?P<columns>{COL}(?:(?:, | and ){COL})*)(?:,? keeping (?:the )?(?P<keep>first|last)(?: occurrence)?)?', body, re.I)
    if duplicate:
        columns = re.split(', | and ', duplicate['columns'].upper(), flags=re.I)
        expr = call('duplicate_' + (duplicate['keep'] or 'first').lower(), *(reference(c, T.SCALAR) for c in columns))
        return goal(expr, columns[0], W.DELETE_ROW)
    copied = re.fullmatch(rf'(?:copy|duplicate) (?:sheet|worksheet) (?P<source>{Q}) (?:as|to|named) (?P<target>{Q})', body, re.I)
    if copied:
        return goal(literal(copied['target'][1:-1]), '@sheet', W.COPY_SHEET, target_sheet=copied['source'][1:-1])
    insert = re.fullmatch(rf'insert (?:a |one )?(?:blank )?(?P<axis>row|column) (?:at|before) (?:(?:row|column) )?(?P<position>{COL}|[1-9]\d*)', body, re.I)
    if insert:
        axis, position = insert['axis'].lower(), insert['position'].upper()
        if axis == 'row' and not position.isdigit() or axis == 'column' and position.isdigit():
            return None
        return goal(literal(None), 'A' + position if axis == 'row' else position + '1', W.INSERT_ROW if axis == 'row' else W.INSERT_COLUMN)
    scope = re.fullmatch(rf'(?P<operation>unmerge|sort) (?:the )?(?:range |cells )?(?P<range>{CELL}:{CELL})(?: (?P<order>ascending)(?: by (?:the )?first column)?)?', body, re.I)
    if scope:
        if scope['operation'].lower() == 'sort' and not scope['order']:
            return None
        return [Goal(identifier, W.UNMERGE if scope['operation'].lower() == 'unmerge' else W.SORT,
                     Reference(scope['range'].upper(), T.SCALARS, sheet), literal('ascending') if scope['order'] else literal(None), span)]
    convert = re.fullmatch(rf'(?:convert|change) (?:all )?numbers (?:in|from) column (?P<column>{COL}) to text', body, re.I)
    if convert:
        return goal(call('to_text', reference(convert['column'].upper(), T.SCALAR)), convert['column'].upper(), W.VALUE)
    coalesce = re.fullmatch(rf'(?:write|put) (?:the )?unique nonblank value from columns (?P<columns>{COL}(?:(?:, | and ){COL})*) (?:in|into) column (?P<target>{COL}), (?:otherwise|or) (?:use|take) column (?P<fallback>{COL})', body, re.I)
    if coalesce:
        columns = re.split(', | and ', coalesce['columns'].upper(), flags=re.I)
        expr = call('coalesce_unique', *(reference(c, T.SCALAR) for c in columns), reference(coalesce['fallback'].upper(), T.SCALAR))
        return goal(expr, coalesce['target'].upper())
    transform = re.fullmatch(rf'(?P<operation>remove leading spaces|extract text after the first colon|remove ISO time|extract first and last name) from column (?P<source>{COL})(?: (?:in|into) column (?P<target>{COL}))?(?: starting (?:at|from) row (?P<start>\d+))?', body, re.I)
    if transform:
        names = {'remove leading spaces':'ltrim','extract text after the first colon':'after_colon','remove iso time':'strip_iso_time','extract first and last name':'first_last_name'}
        start = int(transform['start']) if transform['start'] else None
        expr = call(names[transform['operation'].lower()], reference(transform['source'].upper(), T.TEXT, start_row=start))
        return goal(expr, (transform['target'] or transform['source']).upper(), W.FORMULA if transform['target'] else W.VALUE, start)
    native = parse_native_frames(body, identifier, span, sheet)
    return native


def parse_native_frames(body, identifier, span, sheet):
    def make(expression, target, operation=W.FORMULA, start=None, end=None, target_sheet=sheet, predicate=None):
        return [Goal(identifier, operation, Reference(target, expression.value_type if operation in {W.VALUE,W.FORMULA} else T.SCALAR, target_sheet, start, end), expression, span, predicate=predicate)]
    numeric = re.fullmatch(rf'I need a VB \(Visual Basic\) code to delete all rows in my Excel worksheet that contain numerical values\. The data in question is shown in column (?P<col>{COL})\. I have attached the file with the relevant data for reference', body, re.I)
    if numeric:
        return make(call('is_number', reference(numeric['col'].upper(), T.SCALAR)), numeric['col'].upper(), W.DELETE_ROW)
    less = re.fullmatch(rf'I attempted to create a macro to delete all items in column (?P<col>{COL}) of my (?P<sheet>{Q}) sheet where the ageing is less than or equal to (?P<limit>\d+) days\. I programmed it to highlight the rows to be deleted, but when I ran the macro, no rows were removed\. However, I was able to resolve the issue by modifying my code\. The adjusted code correctly deletes the rows where the value in column (?P=col) is less than or equal to (?P=limit)', body, re.I)
    if less:
        return make(call('le', reference(less['col'].upper(), T.NUMBER), literal(int(less['limit']))), less['col'].upper(), W.DELETE_ROW, target_sheet=less['sheet'][1:-1])
    blank_rows = re.fullmatch(rf'I have a dataset in an Excel sheet named (?P<sheet>{Q}) that contains blank rows interspersed among the data\. I need a macro that can delete these blank rows by shifting the remaining rows upward so that there are no gaps, with the next filled row moving just below the last filled row of the dataset\. I attempted to write a macro myself to perform this task on a different sheet named {Q}, but it did not function correctly when I ran it\. Could someone please debug and correct my code', body, re.I)
    if blank_rows:
        return make(call('row_blank'), 'A', W.DELETE_ROW, target_sheet=blank_rows['sheet'][1:-1])
    duplicates = re.fullmatch(rf'optimize the VBA code I\'ve written to delete duplicate rows based on the ID in Column (?P<col>{COL}) of an Excel sheet so that it can process a larger number of rows faster, such as around [\d,]+ rows\? The code currently works with fewer lines but becomes significantly slower with larger datasets', body, re.I)
    duplicate_column = re.fullmatch(rf'I found a code in this forum that highlights and deletes duplicate rows within the current region in Excel, using the entire row content for comparison\. The existing code successfully identifies and highlights entire rows that are duplicates and then deletes them\. However, I need to modify the code so that it deletes duplicate rows based only on the data in Column (?P<col>{COL}), ignoring whether the other columns from {COL} to the last column match or not\. How can this be achieved', body, re.I)
    if duplicates or duplicate_column:
        match = duplicates or duplicate_column
        return make(call('duplicate_first', reference(match['col'].upper(), T.SCALAR)), match['col'].upper(), W.DELETE_ROW)
    marked = re.fullmatch(rf'(?:I need to )?create a macro that will remove duplicate records from a list\. These duplicates are defined by having identical values in the (?P<a>{Q}) and (?P<b>{Q}) columns\. However, the macro should only remove a duplicate record if its corresponding (?P<flag>{Q}) column is blank, leaving behind the duplicate record where (?P=flag) is marked (?P<keep>{Q})\. The solution should be more efficient than looping, concatenating columns, and comparing the concatenated strings as this method is too slow\. How can I achieve this', body, re.I)
    if marked:
        a, b, flag = (reference('@header:' + marked[k][1:-1], T.SCALAR) for k in ('a','b','flag'))
        return make(call('duplicate_unmarked', a, b, flag, literal(marked['keep'][1:-1])), '@header:' + marked['a'][1:-1], W.DELETE_ROW)
    blank_column = re.fullmatch(rf'I have written a macro intended to delete all blank rows in Column (?P<col>{COL}), starting from the row thatthe first value in that column appear and stopping at the last row \. However, the macro fails to remove these empty rows\. Please analyze my macro, determine what corrections are needed, and modify it as necessary\. The existing macro is as follows: Sub (?P<name>\w+)\(\) Dim (?P<last>\w+) As Long, (?P<i>\w+) As Long (?P=last) = Range\(\'(?P=col)\' & Rows.Count\)\.End\(xlUp\)\.Row For (?P=i) = (?P=last) To \d+ Step -1 On Error Resume Next Columns\((?P=i), 1\)\.SpecialCells\(xlCellTypeBlanks\)\.EntireRow\.Delete On Error GoTo 0 Next (?P=i) End Sub\. Additionally, a forum contributor has suggested that using AutoFilter to filter on blanks in Column (?P=col) and then deleting the visible rows might be a better solution, particularly for large datasets, but I have not tried this method yet', body, re.I)
    if blank_column:
        return make(call('is_blank', reference(blank_column['col'].upper(), T.SCALAR)), blank_column['col'].upper(), W.DELETE_ROW)
    letters = re.fullmatch(rf'I have written code intending to delete rows in Column (?P<col>{COL}) of an Excel worksheet where each cell\'s content includes any of the letters (?P<letters>[A-Z](?:, [A-Z])*, or [A-Z])\. These could be part of text or numbers such as .+?\. However, I encountered a runtime error stating {Q}\.? I had highlighted the rows to be deleted in yellow\. Can someone test my code and provide the necessary amendments\? Here is the code I\'ve been working with: Sub \w+\(\) Dim (?P<last>\w+) As Long With Sheets\((?P<sheet>{Q})\) (?P=last) = \.Cells\({Q}\)\.End\(xlUp\)\.Row For (?P<i>\w+) = (?P=last) To (?P<start>\d+) Step -1 If \.Cells\((?P=i), (?P<index>\d+)\)\.Value Like \'\*\[(?P<pattern>[A-Z]+)\]\*\' Then \.Cells\((?P=i), (?P=index)\)\.EntireRow\.Delete End If Next (?P=i) End With End Sub', body, re.I)
    if letters:
        from openpyxl.utils.cell import column_index_from_string
        values = re.findall(r'[A-Z]', letters['letters'])
        if set(values) != set(letters['pattern'].upper()) or column_index_from_string(letters['col']) != int(letters['index']):
            raise IRValidationError('CONFLICTING_DELETION_PREDICATE')
        start = int(letters['start'])
        col = letters['col'].upper()
        found = call('or', *(call('contains', reference(col, T.TEXT, start_row=start), literal(s)) for s in values))
        expr = call('if', call('is_text', reference(col, T.SCALAR, start_row=start)), found, literal(False))
        return make(expr, col, W.DELETE_ROW, start=start)
    text = re.fullmatch(rf'I have an Excel sheet with \d+ rows of data in Column (?P<col>{COL}), which I have changed from number format to text format\. Currently, to ensure each cell displays the actual text data correctly, I need to manually press F2 for each cell, which is time-consuming\. Is there a formula or shortcut to convert the entire column in one step instead of editing each cell individually', body, re.I)
    if text:
        return make(call('to_text', reference(text['col'].upper(), T.SCALAR)), text['col'].upper(), W.VALUE)
    colon = re.fullmatch(rf'remove the numbers and the text that appear before \':\' from each row in my Excel spreadsheet\? Currently, rows have data formatted like {Q}, and I want to transform them to just include the data after the colon, resulting in {Q}', body, re.I)
    if colon:
        return make(call('after_colon', reference('@text:colon', T.TEXT)), '@same', W.VALUE)
    iso = re.fullmatch(r'I have an Excel spreadsheet with a column of dates where some cells include a time value that starts with \'T\' and ends with \'Z\' and these strings are \d+ characters long\. I\'m seeking assistance to modify an existing macro to remove these time values, leaving just the date in each cell of the column', body, re.I)
    if iso:
        return make(call('strip_iso_time', reference('@text:iso_time', T.TEXT)), '@same', W.VALUE,
                    predicate=call('is_text', reference('@text:iso_time', T.SCALAR)))
    leading = re.fullmatch(r'remove an unwanted space at the beginning of cell content in a column with approximately [\d,]+ rows in my Excel spreadsheet, by using a formula in the adjacent column to clean up each individual cell', body, re.I)
    if leading:
        return make(call('ltrim', reference('@text:leading_space', T.TEXT)), '@adjacent')
    compact = re.fullmatch(r'I have a list of numbers in cells that represent dates, formatted as \'jjjjmmdd\' without spaces \(e\.g\., \d{8} for [A-Za-z]+ \d{1,2}, \d{4}\)\. I need to use a macro to reformat these numbers to include spaces, resulting in the format \'jjjj mm dd\'\. Changing the cell format directly does not achieve what I need\. Initially, I used \'jjjj\' thinking it was the correct notation for the year in my locale \([A-Za-z]+\), but it seems that \'yyyy\' is the correct notation\. How can I accomplish this with a macro', body, re.I)
    if compact:
        return make(call('date_text', call('to_text', reference('@text:compact_date', T.SCALAR)), literal('yyyymmdd'), literal('yyyy mm dd')), '@same', W.VALUE)
    urn = re.fullmatch(rf'I have an Excel spreadsheet with [\d,]+ rows\. Each row includes a Unique Reference Number \(URN\) in column (?P<key>{COL}) and associated data & times in column (?P<data>{COL})\. In column (?P=key), there are some blank cells representing newly inserted rows\. Instead of renumbering all the rows, I need a way to programmatically add letters to the numbers in these blank cells to create new URNs\. For example, new rows [\d-]+ should be labeled {Q}(?:, {Q})*(?:, etc\.)?, following the last numbered row\. This pattern should continue such that any blank cell takes on the number of the last non-blank cell above it followed by a sequential letter \(e\.g\., {Q}(?:, {Q})* for new rows after {Q}\)\. If the number of added rows exceeds 26, the labels should proceed to \'aa\', \'ab\', \'ac\', and so on\. I[^.]*? seeking a VBA solution that could automate this process across the entire sheet\. Some attempts at a solution have been made but without success, as the provided macros either haven\'t run as expected or nothing has happened upon running them', body, re.I)
    if urn:
        key = reference(urn['key'].upper(), T.SCALAR)
        data = reference(urn['data'].upper(), T.SCALAR)
        return make(call('urn_next', key, data), urn['key'].upper(), W.VALUE,
                    predicate=call('and', call('is_blank', key), call('not', call('is_blank', data))))
    dotted = re.fullmatch(rf'I need a VBA macro that will process my data, which is imported with dates in a specific format followed by other characters\. Specifically, I require the macro to remove all data including any spaces after the date entries and convert these date entries from the format \'yyyy\.mm\.dd\' to \'dd/mm/yyyy\'\. For example, the date \'\d{{4}}\.\d{{2}}\.\d{{2}}\' followed by any text such as {Q} should result in just the date \'\d{{2}}/\d{{2}}/\d{{4}}\'', body, re.I)
    if dotted:
        return make(call('date_text', reference('@text:ymd_dot', T.TEXT), literal('yyyy.mm.dd'), literal('dd/mm/yyyy')), '@same', W.VALUE)
    sap = re.fullmatch(r'convert date formats from \'Day, dd Month yyyy\' to \'dd-mm-yyyy\' when the data is imported from SAP extracts into Excel', body, re.I)
    if sap:
        return make(call('date_text', reference('@text:sap_date', T.TEXT), literal('Day, dd Month yyyy'), literal('dd-mm-yyyy')), '@same', W.VALUE)
    names = re.fullmatch(rf'I am working with data in Column (?P<col>{COL}) and I\'ve used the formula =(?P<formula>LEFT\((?P<cell>{CELL}),FIND\(" ",(?P=cell)\)-1\)) to extract the first name from a string containing the first name, surname, and email address\. However, the formula is only extracting the first name, not the surname\. I need an amended version of my formula that will correctly extract both the first name and the surname, omitting the email address from the extracted result', body, re.I)
    if names:
        col = re.sub(r'\d', '', names['cell']).upper()
        if col != names['col'].upper():
            raise IRValidationError('CONFLICTING_NAME_SOURCE_REFERENCE')
        start = int(re.search(r'\d+', names['cell'])[0])
        return make(call('first_last_name', reference(col, T.TEXT, start_row=start)), '@formula_column:' + names['formula'], start=start)
    alternatives = re.fullmatch(rf'I have an Excel spreadsheet for employee details with column (?P<old>{COL}) containing the current employee\'s name\. Columns (?P<a>{COL}), (?P<b>{COL}), and (?P<c>{COL}) may or may not contain the names of new employees poised to take over in the next year\. I need to create a formula for a new column \(column (?P<target>{COL})\) that pulls the name of a new employee from columns (?P=a), (?P=b), or (?P=c) if any exist, but if no new employee names are present, it should default to using the current employee\'s name from column (?P=old)\. With two columns, this would be a simple IF statement, but I\'m unsure how to approach it with three potential new employee columns\. An example was provided with the original question\. Column (?P=target) is what I\'m trying to generate with a formula', body, re.I)
    if alternatives:
        expr = call('coalesce_unique', *(reference(alternatives[r].upper(), T.SCALAR) for r in ('a','b','c','old')))
        return make(expr, alternatives['target'].upper())
    matching = re.fullmatch(rf'find rows in (?P<target_sheet>[\w ]+) when the alphanumeric data in column (?P<a>{COL}) and the dates in column (?P<b>{COL}) match corresponding data in columns (?P=a) and (?P=b) of Sheet named (?P<source_sheet>[\w ]+) in Excel\? Please fill the word (?P<word>{Q}) in column (?P<target>{COL}) for the rows that meet the requirements', body, re.I)
    if matching:
        args = []
        for col in (matching['a'].upper(), matching['b'].upper()):
            args.extend((reference('@domain:' + col, T.SCALARS, matching['source_sheet'].strip()), reference(col, T.SCALAR)))
        predicate = call('gt', call('count_matches', *args), literal(0))
        return make(literal(matching['word'][1:-1]), matching['target'].upper(), W.FORMULA, target_sheet=matching['target_sheet'].strip(), predicate=predicate)
    project = re.fullmatch(rf'In my Excel file containing project data, each project, identified by a consistent budget ID in column (?P<group>{COL}), may have multiple lines representing different components, such as (?P<type>\w+) and services listed in column (?P<kind>{COL}) as revenue types\. I need to determine which projects include (?P=type) and then mark all associated lines of such projects, regardless of whether a line item itself is (?P=type) or service, with a (?P<yes>{Q}) in column (?P<target>{COL})\. Projects without a linked budget should be marked as (?P<no>{Q}) in column (?P=target)\. The current formula only marks (?P=type) revenue lines and thus doesn\'t accurately reflect projects that include (?P=type)\. The sample workbook provided contains sample projects', body, re.I)
    if project:
        group = reference(project['group'].upper(), T.SCALAR)
        count = call('count_matches', reference('@domain:' + project['group'].upper(), T.SCALARS), group,
                     reference('@domain:' + project['kind'].upper(), T.SCALARS), literal(project['type']))
        expr = call('if', call('is_blank', group), literal(project['no'][1:-1]),
                    call('if', call('gt', count, literal(0)), literal(project['yes'][1:-1]), literal(project['no'][1:-1])))
        return make(expr, project['target'].upper())
    stock = re.fullmatch(rf'(?:use a formula in Excel to )?populate a list of currently out-of-stock products with the quantities sold last year\? I have a list of out-of-stock items in Column (?P<key>{COL}) on sheet (?P<target_sheet>\d+), and I need to search for these items in Column (?P=key) on sheet (?P<source_sheet>\d+), which also has the quantities sold in the corresponding Column (?P<value>{COL}), sorted in descending order\. I want to retrieve and place the sold quantity next to each out-of-stock item on sheet (?P=target_sheet)\. The formula data output result may be an empty string', body, re.I)
    if stock:
        from openpyxl.utils.cell import column_index_from_string, get_column_letter
        key = reference(stock['key'].upper(), T.SCALAR)
        expr = call('lookup_exact', key, reference('@domain:' + stock['key'].upper(), T.SCALARS, 'Sheet' + stock['source_sheet']),
                    reference('@domain:' + stock['value'].upper(), T.NUMBERS, 'Sheet' + stock['source_sheet']), literal(None))
        target = get_column_letter(column_index_from_string(stock['key'].upper()) + 1)
        return make(expr, target, target_sheet='Sheet' + stock['target_sheet'])
    running = re.fullmatch(rf'In my Excel worksheet, column (?P<group>{COL}) contains repeated values which define distinct ranges\. I\'m seeking to calculate and display in column (?P<target>{COL}) the maximum product of columns (?P<left>{COL}) and (?P<right>{COL}) for the current and all preceding cells within the same range delimited by the repeated values in column (?P=group), without including any cells below the current one in the calculation\. Is there a way to achieve this in Excel without resorting to macros', body, re.I)
    if running:
        expr = call('group_running_max_product', reference(running['group'].upper(), T.SCALAR), reference(running['left'].upper(), T.NUMBER), reference(running['right'].upper(), T.NUMBER))
        return make(expr, running['target'].upper())
    month_sum = re.fullmatch(rf'(?:I need to )?calculate a sum in cell (?P<target>{CELL}), where the sum is based on the values in Column (?P<value>{COL}) but only includes those values where the corresponding dates in Column (?P<date>{COL}) match a specific month\. My current formula is: SUMIFS\((?P<old>{CELL}:{CELL}),(?P<dates>{CELL}:{CELL}),">="&(?P<month>{CELL}),(?P=dates),"<="&EOMONTH\((?P=month),0\)\)', body, re.I)
    if month_sum:
        old_first, old_last = re.findall(r'\d+', month_sum['old'])
        first, last = re.findall(r'\d+', month_sum['dates'])
        if old_first != first or old_last != last or any(c.upper() != month_sum['date'].upper() for c in re.findall(COL, month_sum['dates'], re.I)):
            raise IRValidationError('CONFLICTING_MONTH_SUM_RANGES')
        expr = call('sum_month', reference(month_sum['value'].upper() + first + ':' + month_sum['value'].upper() + last, T.NUMBERS),
                    reference(month_sum['dates'].upper(), T.SCALARS), reference(month_sum['month'].upper(), T.SCALAR))
        return make(expr, month_sum['target'].upper())
    header_sum = re.fullmatch(rf'(?:I need|create) a formula in Excel that will calculate the sum of multiple cells subject to a specific criterion\? Specifically, I want to sum the quantities \(QTY\) in a column for the rows where the \'PRODUCT ID\' matches a given value \(e\.g\., (?P<id>\d+)\)\. Please output the answer in (?P<target>{CELL})\. For example, the expected result of this sum for \'PRODUCT ID\' (?P=id) should be \d+(?:\.\d+)?, deriving from the individual QTY values of [\d, .]+,? and \d+(?:\.\d+)?\. Sum of QTYs where PRODUCT ID=(?P=id)', body, re.I)
    if header_sum:
        expr = call('sum_matches', reference('@header:QTY', T.NUMBERS), reference('@header:PRODUCT ID', T.SCALARS), literal(int(header_sum['id'])))
        return make(expr, header_sum['target'].upper())
    dates_join = re.fullmatch(rf'In my Excel sheet, I\'m struggling to display multiple dates in a single cell in Column (?P<target>{COL}) using a date format rather than numerical values\. Specifically, for cells containing more than two dates, such as in row \d+ with three dates, my attempt with the TEXT function has been unfruitful\. How can I apply a formula directly in Column (?P=target) that will allow me to join these various dates with a delimiter and still maintain the date formatting, without having to resort to using a helper column\? My current formula is : (?P<formula>TEXTJOIN\((?P<delimiter>{Q}),TRUE,UNIQUE\(IF\((?P<key>{CELL})=(?P<sheet>[\w ]+)!(?P<keys>{COL}):(?P=keys),(?P=sheet)!(?P<dates>{COL}):(?P=dates),""\)\)\))', body, re.I)
    if dates_join:
        col = re.sub(r'\d', '', dates_join['key']).upper()
        start = int(re.search(r'\d+', dates_join['key'])[0])
        keys = reference('@domain:' + dates_join['keys'].upper(), T.SCALARS, dates_join['sheet'])
        dates = reference('@domain:' + dates_join['dates'].upper(), T.SCALARS, dates_join['sheet'])
        expr = call('join_dates', reference(col, T.SCALAR, start_row=start), keys, dates,
                    literal(dates_join['delimiter'][1:-1]), call('date_format', dates))
        return make(expr, '@formula_column:' + dates_join['target'].upper() + '|' + dates_join['formula'], start=start)
    matrix_sum = re.fullmatch(rf'I\'m having trouble with a SUMPRODUCT formula in Excel\. I\'ve noticed an error but can\'t seem to figure out what\'s wrong\. The formula I am trying to correct is intended to pull data from a range where multiple criteria match - these criterias are specified in cells (?P<k1>{CELL}), (?P<k2>{CELL}), and (?P<k3>{CELL}) against ranges (?P<a>{CELL}:{CELL}), (?P<b>{CELL}:{CELL}), and (?P<h>{CELL}:{CELL}) respectively\. The actual computation should happen over the range (?P<values>{CELL}:{CELL})\. I shared my Excel file for reference\. Could someone guide me on how to fix this or provide an alternative solution\? My wrong Formula: (?P<formula>SUMPRODUCT\(.+\))', body, re.I)
    if matrix_sum:
        from .semantic_formula import parse_formula
        from openpyxl.formula.tokenizer import Tokenizer
        tokens = Tokenizer('=' + matrix_sum['formula']).items
        supplied = {t.value.replace('$','').upper() for t in tokens if t.subtype == 'RANGE'}
        declared = {matrix_sum[k].upper() for k in ('k1','k2','k3','a','b','h','values')}
        if not supplied <= declared:
            raise IRValidationError('UNDECLARED_MATRIX_REFERENCE')
        expr = call('matrix_sum', reference(matrix_sum['values'].upper(), T.NUMBERS),
                    reference(matrix_sum['a'].upper(), T.SCALARS), reference(matrix_sum['k1'].upper(), T.SCALAR),
                    reference(matrix_sum['b'].upper(), T.SCALARS), reference(matrix_sum['k2'].upper(), T.SCALAR),
                    reference(matrix_sum['h'].upper(), T.SCALARS), reference(matrix_sum['k3'].upper(), T.SCALAR))
        return make(expr, '@formula:' + matrix_sum['formula'])
    return None
