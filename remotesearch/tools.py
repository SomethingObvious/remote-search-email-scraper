"""Everyday tools: a calculator, unit and currency conversion, and translation."""

import ast
import math
import operator
import re
from collections.abc import Callable
from typing import Any

from . import net

# The calculator walks the parsed expression itself instead of calling eval, so a
# text can only ever do arithmetic.
OPERATORS: dict[type, Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "sin": lambda x: math.sin(math.radians(x)),
    "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)),
    "log": math.log10,
    "ln": math.log,
}
CONSTANTS = {"pi": math.pi, "e": math.e}


class CalcError(Exception):
    pass


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        left, right = _evaluate(node.left), _evaluate(node.right)
        # 9**9**9 would take the process down trying to build the number.
        if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 1e100):
            raise CalcError("that power is too big to work out")
        return OPERATORS[type(node.op)](left, right)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in FUNCTIONS
        and len(node.args) == 1
        and not node.keywords
    ):
        return FUNCTIONS[node.func.id](_evaluate(node.args[0]))
    raise CalcError("it only does numbers, + - * / ^ %, brackets, sqrt, sin, cos, tan, log and ln")


def calculate(expression: str) -> float:
    """Work out an arithmetic expression the way it'd be typed on a phone."""
    text = expression.lower().replace("\N{MULTIPLICATION SIGN}", "*").replace("^", "**")
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 1,000 is one thousand
    text = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", "*", text)  # 3 x 4
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\b", r"(\1/100)*", text)  # 15% of 80
    if len(text) > 100:
        raise CalcError("that's too long to work out")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise CalcError("that isn't an expression it can read") from None
    try:
        return float(_evaluate(tree))
    except ZeroDivisionError:
        raise CalcError("it can't divide by zero") from None
    except (OverflowError, ValueError):
        raise CalcError("the answer is out of range") from None


def number_text(value: float) -> str:
    """Up to 10 significant digits, without a trailing .0."""
    if value.is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.10g}"


def source_calc(expression: str) -> str:
    try:
        return f"{expression} = {number_text(calculate(expression))}"
    except CalcError as exc:
        return f"Couldn't work out '{expression}', as {exc}."


# Each unit's size in the base unit of its kind (metres, kilograms, litres, m/s).
UNITS: dict[str, tuple[str, float]] = {}
for kind, table in {
    "length": {
        ("mm", "millimetre", "millimeter"): 0.001,
        ("cm", "centimetre", "centimeter"): 0.01,
        ("m", "metre", "meter"): 1,
        ("km", "kilometre", "kilometer"): 1000,
        ("in", "inch", "inches"): 0.0254,
        ("ft", "foot", "feet"): 0.3048,
        ("yd", "yard"): 0.9144,
        ("mi", "mile"): 1609.344,
        ("nmi", "nautical mile"): 1852,
    },
    "weight": {
        ("mg", "milligram"): 0.000001,
        ("g", "gram"): 0.001,
        ("kg", "kilogram", "kilo"): 1,
        ("oz", "ounce"): 0.028349523125,
        ("lb", "lbs", "pound"): 0.45359237,
        ("st", "stone"): 6.35029318,
        ("tonne", "metric ton"): 1000,
    },
    "volume": {
        ("ml", "millilitre", "milliliter"): 0.001,
        ("l", "litre", "liter"): 1,
        ("tsp", "teaspoon"): 0.00492892159375,
        ("tbsp", "tablespoon"): 0.01478676478125,
        ("fl oz", "floz", "fluid ounce"): 0.0295735295625,
        ("cup",): 0.2365882365,
        ("pt", "pint"): 0.473176473,
        ("qt", "quart"): 0.946352946,
        ("gal", "gallon"): 3.785411784,
    },
    "speed": {
        ("km/h", "kmh", "kph"): 1 / 3.6,
        ("mph",): 0.44704,
        ("m/s",): 1,
        ("kn", "knot", "kt"): 1852 / 3600,
    },
}.items():
    for names, size in table.items():
        for name in names:
            UNITS[name] = (kind, size)
TEMPERATURES = {"c": "C", "celsius": "C", "f": "F", "fahrenheit": "F", "k": "K", "kelvin": "K"}
CONVERSION = re.compile(r"(-?[\d,]*\.?\d+)\s*(.+?)\s+(?:to|in|into)\s+(.+)", re.IGNORECASE)


def _unit(name: str) -> str:
    """Normalize a unit name, so "Miles", "mile" and "mi." all read the same."""
    name = name.lower().strip(" .").replace("degrees ", "").replace("\N{DEGREE SIGN}", "")
    if name in UNITS or name in TEMPERATURES:
        return name
    return name[:-1] if name.endswith("s") and name[:-1] in UNITS else name


def _temperature(value: float, src: str, dst: str) -> float:
    celsius = {"C": value, "F": (value - 32) * 5 / 9, "K": value - 273.15}[src]
    return {"C": celsius, "F": celsius * 9 / 5 + 32, "K": celsius + 273.15}[dst]


def _currency(value: float, src: str, dst: str) -> str | None:
    rates = net.get_json(
        "https://api.frankfurter.dev/v1/latest",
        "Frankfurter",
        params={"base": src, "symbols": dst, "amount": value},
    )
    rate = ((rates or {}).get("rates") or {}).get(dst)
    if rate is None:
        return (
            f"Frankfurter has no rate from {src} to {dst}. It covers about 30 currencies "
            "from the European Central Bank."
        )
    return f"{number_text(value)} {src} = {rate:,.2f} {dst} (ECB rate for {rates.get('date')})"


def source_convert(text: str) -> str | None:
    """Convert units, temperatures or currencies, as in "10 km to mi" or "50 usd to cad"."""
    match = CONVERSION.fullmatch(text.strip())
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    src, dst = _unit(match.group(2)), _unit(match.group(3))
    if src in TEMPERATURES and dst in TEMPERATURES:
        a, b = TEMPERATURES[src], TEMPERATURES[dst]
        return f"{number_text(value)}{a} = {number_text(round(_temperature(value, a, b), 2))}{b}"
    if src in UNITS and dst in UNITS:
        (kind, size), (other, target) = UNITS[src], UNITS[dst]
        if kind != other:
            return f"Can't convert {kind} to {other}."
        amount = number_text(round(value * size / target, 4))
        return f"{number_text(value)} {match.group(2)} = {amount} {match.group(3)}"
    if re.fullmatch(r"[a-z]{3}", src) and re.fullmatch(r"[a-z]{3}", dst):
        return _currency(value, src.upper(), dst.upper())
    return None


# MyMemory wants ISO 639-1 codes, and people text the language's name.
LANGUAGES = {
    "arabic": "ar",
    "chinese": "zh",
    "mandarin": "zh",
    "cantonese": "zh-HK",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "icelandic": "is",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "norwegian": "no",
    "persian": "fa",
    "farsi": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "punjabi": "pa",
    "romanian": "ro",
    "russian": "ru",
    "spanish": "es",
    "swedish": "sv",
    "tagalog": "tl",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "vietnamese": "vi",
}


def translation_request(text: str) -> tuple[str, str, str] | None:
    """Split "where is the bus to french" into the words, the language and its code."""
    match = re.fullmatch(r"(.+?)\s+(?:to|into)\s+([a-z]+)\.?", text.strip(), re.IGNORECASE)
    code = LANGUAGES.get(match.group(2).lower()) if match else None
    if not match or not code:
        return None
    return match.group(1), match.group(2).capitalize(), code


def mymemory(words: str, code: str) -> str | None:
    found = net.get_json(
        "https://api.mymemory.translated.net/get",
        "MyMemory",
        params={"q": words, "langpair": f"autodetect|{code}"},
    )
    found = found if isinstance(found, dict) else {}
    if found.get("quotaFinished"):
        raise net.SourceError("MyMemory", "it's out of free translations for today")
    if str(found.get("responseStatus")) != "200":
        raise net.SourceError("MyMemory", str(found.get("responseDetails") or "it sent an error"))
    return (found.get("responseData") or {}).get("translatedText") or None
