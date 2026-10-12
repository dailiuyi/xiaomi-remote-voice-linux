"""Explicit transcript normalization and edit-distance scoring for local ASR."""
import re
import unicodedata

DIGITS = dict(zip('零〇一二两三四五六七八九', '001223456789'))
UNITS = {'十': 10, '百': 100, '千': 1000, '万': 10000}


def number(match):
    text = match.group()
    if not any(char in UNITS for char in text):
        return ''.join(DIGITS[char] for char in text)
    total = section = digit = 0
    for char in text:
        if char in DIGITS:
            digit = int(DIGITS[char])
        elif char == '万':
            total += (section + digit) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * UNITS[char]
            digit = 0
    return str(total + section + digit)


def normalize(text):
    text = unicodedata.normalize('NFKC', text).lower()
    text = re.sub(r'点\s*(?=[a-z])', '.', text)
    text = re.sub(r'[零〇一二两三四五六七八九十百千万]+', number, text)
    return ''.join(char for char in text if char.isalnum())


def edits(reference, hypothesis):
    """Levenshtein total and deterministic substitution/deletion/insertion counts."""
    previous = [(i, 0, 0, i) for i in range(len(hypothesis) + 1)]
    for i, ref in enumerate(reference, 1):
        current = [(i, 0, i, 0)]
        for j, hyp in enumerate(hypothesis, 1):
            if ref == hyp:
                current.append(previous[j - 1])
                continue
            e, s, d, ins = previous[j - 1]
            substitution = (e + 1, s + 1, d, ins)
            e, s, d, ins = previous[j]
            deletion = (e + 1, s, d + 1, ins)
            e, s, d, ins = current[-1]
            insertion = (e + 1, s, d, ins + 1)
            current.append(min((substitution, deletion, insertion), key=lambda result: result[0]))
        previous = current
    return previous[-1]


def score(reference, text, terms=()):
    ref, hyp = normalize(reference), normalize(text)
    count, substitutions, deletions, insertions = edits(ref, hyp)
    return {'reference_chars': len(ref), 'hypothesis_chars': len(hyp),
            'edit_errors': count, 'substitutions': substitutions,
            'deletions': deletions, 'insertions': insertions,
            'normalized_cer': count / len(ref) if ref else None,
            'term_matches': {term: normalize(term) in hyp for term in terms},
            'normalized_reference': ref, 'normalized_hypothesis': hyp}
