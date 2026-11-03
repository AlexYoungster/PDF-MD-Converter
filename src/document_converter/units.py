"""Conservative unit syntax, preserving division and denominator grouping.

This describes the printed symbols, never converts their values or guesses
missing multiplication, missing parentheses, or ambiguous temperature offsets.
"""
from __future__ import annotations

import html
import json
import re

ATOMS = set(('W kW m cm mm km s min h K ℃ °C °F °R N kN J kJ cal kcal '
             'Btu kg g mg kgf lbf ft in Pa kPa MPa GPa bar atm at Hz V A '
             'Ω F H C S T Wb mol cd lx lm rad sr r L l mmHg mmH₂O').split())
SUP = dict(zip('⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻', '0123456789+-'))
UNIT_CONTEXT = re.compile(r'单位|计量|长度|宽度|高度|直径|距离|面积|体积|质量|密度|压力|应力|功率|能量|扭矩|转速|速度|流量|温度|频率|电压|电流|电阻|电容|电感|电荷|牛顿|瓦|焦耳|帕|赫兹|伏特|安培|欧姆|特斯拉|韦伯|库仑|摩尔|坎德拉|弧度|秒|米|克')


def ascii_unit(value: str) -> str:
    value = value.translate(str.maketrans('（）［］', '()[]'))
    value = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+', lambda m: '^' + ''.join(SUP[c] for c in m[0]), value)
    return re.sub(r'\s+', '', value).replace('·', '*').replace('−', '-')


def parse_unit(value: str) -> dict | None:
    source = ascii_unit(value)
    if not source or len(source) > 256:
        return None
    depth = 0
    for c in source:
        depth += (c == '(') - (c == ')')
        if depth < 0 or depth > 16:
            return None
    if depth:
        return None
    parts = re.findall(r'mmH₂O|[A-Za-z]+|[Ω℃°][A-Za-z]?|[*/()^]|[+-]?\d+', source)
    if ''.join(parts) != source:
        return None
    position = 0

    def atom():
        nonlocal position
        if position >= len(parts):
            raise ValueError('missing unit')
        token = parts[position]
        position += 1
        if token == '(':
            node = expression()
            if position >= len(parts) or parts[position] != ')':
                raise ValueError('unbalanced denominator')
            position += 1
        elif token in ATOMS:
            node = {'op': 'unit', 'symbol': token}
        else:
            raise ValueError('unrecognized unit symbol')
        if position < len(parts) and parts[position] == '^':
            position += 1
            if position >= len(parts) or not re.fullmatch(r'[+-]?\d+', parts[position]):
                raise ValueError('invalid exponent')
            node = {'op': 'power', 'base': node, 'exponent': int(parts[position])}
            position += 1
        return node

    def expression():
        nonlocal position
        node = atom()
        while position < len(parts) and parts[position] in {'*', '/'}:
            operator = parts[position]
            position += 1
            node = {'op': 'divide' if operator == '/' else 'multiply', 'left': node, 'right': atom()}
        return node

    try:
        node = expression()
        if position != len(parts):
            return None
    except (ValueError, RecursionError):
        return None
    return {'expression': value.strip(), 'ascii_expression': source, 'ast': node,
            'interpretation': 'printed_symbol_syntax_only'}


def cell_unit(value: str) -> dict | None:
    """Parse a whole unit or a trailing balanced symbol group, no guessing."""
    stripped = value.rstrip()
    if not stripped:
        return None
    end = len(stripped)
    pair = {')': '(', '）': '（', ']': '[', '］': '［'}
    if stripped[-1] in pair:
        stack = []
        start = None
        opens = set(pair.values())
        for i in range(end - 1, -1, -1):
            c = stripped[i]
            if c in pair:
                stack.append(pair[c])
            elif c in opens:
                if not stack or stack.pop() != c:
                    return None
                if not stack:
                    start = i
                    break
        if start is not None:
            parsed = parse_unit(stripped[start + 1:end - 1])
            # A complete expression such as W/(m*K) must not be reduced to its
            # last denominator group; use the complete string if parseable.
            whole = parse_unit(stripped)
            if whole and whole['ast']['op'] != 'unit':
                return whole | {'range': [0, end]}
            if parsed and (parsed['ast']['op'] != 'unit' or UNIT_CONTEXT.search(stripped[:start])):
                return parsed | {'range': [start, end]}
    parsed = parse_unit(stripped)
    return parsed | {'range': [0, end]} if parsed and parsed['ast']['op'] != 'unit' else None


def unit_html(text: str, render) -> str:
    unit = cell_unit(text)
    if not unit:
        return render(text)
    start, end = unit['range']
    expression = html.escape(unit['ascii_expression'], quote=True)
    ast = html.escape(json.dumps(unit['ast'], ensure_ascii=False, separators=(',', ':')), quote=True)
    return (render(text[:start]) +
            f'<span class="unit-expression" data-unit="{expression}" data-unit-ast="{ast}" '
            f'style="white-space:nowrap">{render(text[start:end])}</span>' + render(text[end:]))
