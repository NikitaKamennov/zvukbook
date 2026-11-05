"""Conservative reading of common book abbreviations and measured quantities."""
import re

from .numerals import number_form


# Nominative singular, genitive singular, genitive plural, bare plural.
_UNITS = {
    "м": ("метр", "метра", "метров", "метры"),
    "км": ("километр", "километра", "километров", "километры"),
    "см": ("сантиметр", "сантиметра", "сантиметров", "сантиметры"),
    "мм": ("миллиметр", "миллиметра", "миллиметров", "миллиметры"),
    "дм": ("дециметр", "дециметра", "дециметров", "дециметры"),
    "мкм": ("микрометр", "микрометра", "микрометров", "микрометры"),
    "кг": ("килограмм", "килограмма", "килограммов", "килограммы"),
    "г": ("грамм", "грамма", "граммов", "граммы"),
    "мг": ("миллиграмм", "миллиграмма", "миллиграммов", "миллиграммы"),
    "мкг": ("микрограмм", "микрограмма", "микрограммов", "микрограммы"),
    "т": ("тонна", "тонны", "тонн", "тонны"),
    "л": ("литр", "литра", "литров", "литры"),
    "мл": ("миллилитр", "миллилитра", "миллилитров", "миллилитры"),
    "га": ("гектар", "гектара", "гектаров", "гектары"),
    "ч": ("час", "часа", "часов", "часы"),
    "мин": ("минута", "минуты", "минут", "минуты"),
    "с": ("секунда", "секунды", "секунд", "секунды"),
    "мс": ("миллисекунда", "миллисекунды", "миллисекунд", "миллисекунды"),
    "Гц": ("герц", "герца", "герц", "герцы"),
    "кГц": ("килогерц", "килогерца", "килогерц", "килогерцы"),
    "МГц": ("мегагерц", "мегагерца", "мегагерц", "мегагерцы"),
    "Вт": ("ватт", "ватта", "ватт", "ватты"),
    "кВт": ("киловатт", "киловатта", "киловатт", "киловатты"),
    "МВт": ("мегаватт", "мегаватта", "мегаватт", "мегаватты"),
    "Дж": ("джоуль", "джоуля", "джоулей", "джоули"),
    "кДж": ("килоджоуль", "килоджоуля", "килоджоулей", "килоджоули"),
    "В": ("вольт", "вольта", "вольт", "вольты"),
    "кВ": ("киловольт", "киловольта", "киловольт", "киловольты"),
    "Па": ("паскаль", "паскаля", "паскалей", "паскали"),
    "кПа": ("килопаскаль", "килопаскаля", "килопаскалей", "килопаскали"),
    "МПа": ("мегапаскаль", "мегапаскаля", "мегапаскалей", "мегапаскали"),
    "руб": ("рубль", "рубля", "рублей", "рубли"),
    "коп": ("копейка", "копейки", "копеек", "копейки"),
    "%": ("процент", "процента", "процентов", "проценты"),
    "°C": ("градус Цельсия", "градуса Цельсия", "градусов Цельсия", "градусы Цельсия"),
    "°F": ("градус Фаренгейта", "градуса Фаренгейта", "градусов Фаренгейта", "градусы Фаренгейта"),
}
_ALIASES = {"m": "м", "km": "км", "cm": "см", "mm": "мм", "сек": "с", "₽": "руб", "°С": "°C"}
_LENGTH = ("мкм", "км", "см", "мм", "дм", "м", "km", "cm", "mm", "m")
_LENGTH_PATTERN = "(?:" + "|".join(_LENGTH) + ")"
_POWER = r"(?:\s*\^\s*[23]|[23²³])"
_LENGTH_UNIT = rf"(?:(?:кв\.?|куб\.?)\s*{_LENGTH_PATTERN}|{_LENGTH_PATTERN}(?:{_POWER})?)"
_OTHER = "|".join(re.escape(value) for value in sorted(set(_UNITS) | set(_ALIASES), key=len, reverse=True))
_UNIT = rf"(?:{_LENGTH_UNIT}|(?:{_OTHER}))"
_EXPRESSION = rf"{_UNIT}(?:\s*/\s*{_UNIT}(?:{_POWER})?)?"
_NUMBER = r"(?:[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+|[0-9]+)(?:[,.][0-9]+)?"
_MEASUREMENT = re.compile(rf"(?<!\w)(?P<number>{_NUMBER})\s*(?:(?P<scale>тыс|млн|млрд|трлн)\.?\s*)?(?P<unit>{_EXPRESSION})(?!\w)")
_BARE_AREA = re.compile(rf"(?<!\w)(?:(?:кв\.?|куб\.?)\s*{_LENGTH_PATTERN}|{_LENGTH_PATTERN}{_POWER})(?!\w)")


def _unit(value):
    value = re.sub(r"\s+", "", value)
    power = 0
    if value.startswith("кв"):
        power, value = 2, re.sub(r"^кв\.?", "", value)
    elif value.startswith("куб"):
        power, value = 3, re.sub(r"^куб\.?", "", value)
    else:
        suffix = re.search(r"\^?([23²³])$", value)
        if suffix:
            power = 2 if suffix[1] in ("2", "²") else 3
            value = value[:suffix.start()]
    return _ALIASES.get(value, value), power


def _reading(value, form):
    unit, power = _unit(value)
    forms = _UNITS[unit]
    if not power:
        return forms[form]
    stem = "квадратн" if power == 2 else "кубическ"
    adjective = (stem + "ый" if power == 2 else stem + "ий", stem + "ого", stem + "ых" if power == 2 else stem + "их", stem + "ые" if power == 2 else stem + "ие")
    # Two/four use genitive singular nouns but genitive plural adjectives.
    return adjective[form] + " " + forms[form]


def _quantity(value, raw_number, *, scaled=False):
    numerator, *denominator = re.split(r"\s*/\s*", value)
    number = re.sub(r"\s", "", raw_number)
    fractional = "," in number or "." in number
    if scaled:
        form = 2
    elif fractional:
        form = 1
    else:
        ending = int(number[-2:])
        form = number_form(ending, (0, 1, 2))
    unit, power = _unit(numerator)
    if power and form == 1 and not fractional and not scaled:
        noun = _UNITS[unit][1]
        reading = ("квадратных " if power == 2 else "кубических ") + noun
    else:
        reading = _reading(numerator, form)
    if denominator:
        base, degree = _unit(denominator[0])
        if base in ("с", "ч", "мин"):
            reading += " в " + {"с": "секунду", "ч": "час", "мин": "минуту"}[base]
            if degree:
                reading += " в " + ("квадрате" if degree == 2 else "кубе")
        else:
            reading += " на " + _reading(denominator[0], 0)
    return reading


def expand_book_abbreviations(text):
    def measurement(match):
        # г. is very often a date, while г without a dot is a mass unit.
        # Likewise с. before a number can introduce a page reference.
        if match["unit"] == "г" and text[match.end():].startswith("."):
            return match.group()
        if match["unit"] == "с" and re.match(r"\.\s*[0-9]", text[match.end():]):
            return match.group()
        scale = match["scale"]
        value = match["number"] + (" " + scale if scale else "")
        return value + " " + _quantity(match["unit"], match["number"], scaled=bool(scale))
    text = _MEASUREMENT.sub(measurement, text)
    text = _BARE_AREA.sub(lambda match: _reading(match.group(), 3), text)
    # Lowercase, isolated м. is a length label; initials such as М. Горький
    # and a following capitalised name are deliberately left alone.
    text = re.sub(r"(?<!\w)м\.(?!\w|\s*[А-ЯЁ])", "метры.", text)
    phrases = (
        (r"и\s+т\s*\.\s*д\.", "и так далее", True),
        (r"и\s+т\s*\.\s*п\.", "и тому подобное", True),
        (r"т\s*\.\s*е\.", "то есть", False),
        (r"т\s*\.\s*к\.", "так как", False),
        (r"т\s*\.\s*о\.", "таким образом", False),
        (r"н\s*\.\s*э\.", "нашей эры", True),
        (r"и\s+др\.", "и другие", True),
        (r"и\s+пр\.", "и прочее", True),
        (r"напр\.", "например", False),
    )
    for pattern, spoken, terminal in phrases:
        def phrase(match):
            following = text[match.end():].lstrip()
            period = terminal and (not following or following[0].isupper())
            return spoken + ("." if period else "")
        text = re.sub(r"(?<!\w)" + pattern + r"(?!\w)", phrase, text, flags=re.I)
    for short, full in (("рис", "рисунок"), ("табл", "таблица"), ("стр", "страница"), ("с", "страница"), ("гл", "глава")):
        text = re.sub(r"(?<!\w)" + short + r"\.\s*(?=[0-9])", full + " ", text)
    text = re.sub(r"(?<!\w)см\.(?=\s+[а-яёА-ЯЁ0-9])", "смотри", text)
    return text
