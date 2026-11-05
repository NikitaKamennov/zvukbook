"""Bounded Russian number verbalization without loading another model/package."""
import re


_ONES = ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот")
_SCALES = (("", "", ""), ("тысяча", "тысячи", "тысяч"), ("миллион", "миллиона", "миллионов"),
           ("миллиард", "миллиарда", "миллиардов"), ("триллион", "триллиона", "триллионов"),
           ("квадриллион", "квадриллиона", "квадриллионов"), ("квинтиллион", "квинтиллиона", "квинтиллионов"))
_FRACTIONS = (("десятая", "десятых"), ("сотая", "сотых"), ("тысячная", "тысячных"),
              ("десятитысячная", "десятитысячных"), ("стотысячная", "стотысячных"), ("миллионная", "миллионных"))
_NUMBER = re.compile(r"(?:[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+(?![0-9])|[0-9]+)(?:[,.][0-9]+)?")


def number_form(value, forms):
    if 11 <= value % 100 <= 14:
        return forms[2]
    return forms[0] if value % 10 == 1 else forms[1] if 2 <= value % 10 <= 4 else forms[2]


def integer_words(digits, feminine=False):
    if not digits or any(character not in "0123456789" for character in digits):
        raise ValueError("Ожидается неотрицательное целое число из цифр.")
    if len(digits) > 21 or len(digits) > 1 and digits.startswith("0"):
        return " ".join(_ONES[int(character)] for character in digits)
    value = int(digits)
    if not value:
        return _ONES[0]
    groups = []
    scale = 0
    while value:
        group, value = value % 1000, value // 1000
        if group:
            words = []
            if group >= 100:
                words.append(_HUNDREDS[group // 100])
            tail = group % 100
            if 10 <= tail < 20:
                words.append(_TEENS[tail - 10])
            else:
                if tail >= 20:
                    words.append(_TENS[tail // 10])
                unit = tail % 10
                if unit:
                    words.append(("одна" if unit == 1 else "две") if unit in (1, 2) and (scale == 1 or scale == 0 and feminine) else _ONES[unit])
            if scale:
                words.append(number_form(group, _SCALES[scale]))
            groups.append(" ".join(words))
        scale += 1
    return " ".join(reversed(groups))


def number_words(raw, feminine=False):
    raw = re.sub(r"\s", "", raw)
    parts = re.split(r"[,.]", raw, maxsplit=1)
    if len(parts) == 1:
        return integer_words(raw, feminine=feminine)
    whole, fraction = parts
    if len(fraction) > len(_FRACTIONS):
        return integer_words(whole) + " запятая " + " ".join(_ONES[int(character)] for character in fraction)
    # Bound int conversion even for pathological OCR tokens.
    fractional_value = int(fraction)
    whole_ending = int(whole[-2:])
    singular = fractional_value % 10 == 1 and fractional_value % 100 != 11
    return (integer_words(whole, feminine=True) + " " + number_form(whole_ending, ("целая", "целых", "целых"))
            + " " + integer_words(str(fractional_value), feminine=True) + " "
            + _FRACTIONS[len(fraction) - 1][0 if singular else 1])


def verbalize_numbers(text):
    # Scientific notation and common scientific scale abbreviations come before
    # plain numbers; values stay represented, including signs and precision.
    text = re.sub(r"(?<!\w)([0-9]+(?:[,.][0-9]+)?)[eE]([+-]?[0-9]+)(?!\w)",
                  r"\1 умножить на десять в степени \2", text)
    scales = {"тыс": _SCALES[1], "млн": _SCALES[2], "млрд": _SCALES[3], "трлн": _SCALES[4]}
    def expand_scale(match):
        value, abbreviation = match.group(1), match.group(2).casefold()
        ending = int(value[-2:]) if value.isdigit() else 0
        form = number_form(ending, scales[abbreviation]) if value.isdigit() else scales[abbreviation][1]
        return value + " " + form
    text = re.sub(r"([0-9]+(?:[,.][0-9]+)?)\s*(млрд|млн|трлн|тыс)\.?(?![а-яё])", expand_scale, text, flags=re.I)
    text = re.sub(r"(?<![\w])[-−](?=[0-9])", "минус ", text)
    feminine_units = re.compile(r"\s+(?:тонна|тонны|тонн|минута|минуты|минут|секунда|секунды|секунд|"
                                r"миллисекунда|миллисекунды|миллисекунд|копейка|копейки|копеек|тысяча|тысячи|тысяч)(?!\w)", re.I)
    return _NUMBER.sub(lambda match: " " + number_words(match.group(), feminine=bool(feminine_units.match(text, match.end()))) + " ", text)
