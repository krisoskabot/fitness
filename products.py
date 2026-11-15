import base64
import ctypes
import json
import math
import re
from ctypes import c_char_p, c_int, c_uint, c_ulong, c_void_p
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def valid_code(code):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", code):
        return False
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(code[:-1])))
    return (-total) % 10 == int(code[-1])


@lru_cache(maxsize=1)
def scanner_library():
    lib = ctypes.CDLL("libzbar.so.0")
    signatures = {
        "image_scanner_create": (c_void_p, []), "image_create": (c_void_p, []),
        "image_set_format": (None, [c_void_p, c_ulong]),
        "image_set_size": (None, [c_void_p, c_uint, c_uint]),
        "image_set_data": (None, [c_void_p, c_void_p, c_ulong, c_void_p]),
        "scan_image": (c_int, [c_void_p, c_void_p]),
        "image_first_symbol": (c_void_p, [c_void_p]),
        "symbol_next": (c_void_p, [c_void_p]), "symbol_get_data": (c_char_p, [c_void_p]),
        "image_destroy": (None, [c_void_p]), "image_scanner_destroy": (None, [c_void_p]),
    }
    for name, (result, args) in signatures.items():
        func = getattr(lib, "zbar_" + name)
        func.restype, func.argtypes = result, args
    return lib


def decode_image(entry):
    width, height = entry["width"], entry["height"]
    if not all(type(x) is int and 1 <= x <= 1600 for x in (width, height)):
        raise ValueError()
    gray = base64.b64decode(entry["gray"], validate=True)
    if len(gray) != width * height:
        raise ValueError()
    lib = scanner_library()
    scanner, image = lib.zbar_image_scanner_create(), lib.zbar_image_create()
    try:
        lib.zbar_image_set_format(image, int.from_bytes(b"Y800", "little"))
        lib.zbar_image_set_size(image, width, height)
        lib.zbar_image_set_data(image, gray, len(gray), None)
        lib.zbar_scan_image(scanner, image)
        symbol = lib.zbar_image_first_symbol(image)
        while symbol:
            code = lib.zbar_symbol_get_data(symbol).decode("ascii", "ignore")
            if valid_code(code):
                return code
            symbol = lib.zbar_symbol_next(symbol)
    finally:
        lib.zbar_image_destroy(image)
        lib.zbar_image_scanner_destroy(scanner)
    return None


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def lookup(conn, code):
    if not valid_code(code):
        raise ValueError()
    row = conn.execute("SELECT body FROM product_labels WHERE code=?", (code,)).fetchone()
    if row:
        return {"product": json.loads(row[0]), "source": "label"}
    fields = "product_name_ru,product_name,brands,nutriments,nutrition_data_per,product_quantity_unit"
    request = Request(f"https://world.openfoodfacts.org/api/v2/product/{code}.json?fields={fields}",
                      headers={"User-Agent": "MoyDen/1.0 (https://github.com/krisoskabot/fitness)"})
    try:
        with urlopen(request, timeout=7) as response:
            body = json.load(response)
    except HTTPError as exc:
        return {"reason": "missing" if exc.code == 404 else "offline"}
    except (URLError, TimeoutError, OSError, ValueError):
        return {"reason": "offline"}
    p = body.get("product") or {}
    if not p:
        return {"reason": "missing"}
    nutrients = p.get("nutriments") or {}
    values = {key: number(nutrients.get(name + "_100g"))
              for key, name in (("kcal", "energy-kcal"), ("p", "proteins"), ("f", "fat"), ("c", "carbohydrates"))}
    if values["kcal"] is None:
        kj = number(nutrients.get("energy-kj_100g"))
        if kj is not None:
            values["kcal"] = round(kj / 4.184, 1)
    liquid = p.get("nutrition_data_per") == "100ml" or p.get("product_quantity_unit") == "ml"
    return {"source": "off", "product": {"name": p.get("product_name_ru") or p.get("product_name") or p.get("brands") or "",
            "unit": "ml" if liquid else "g", **values}}


def save_label(conn, code, label):
    if not valid_code(code) or not isinstance(label, dict):
        raise ValueError()
    if not isinstance(label.get("name"), str) or not 0 < len(label["name"].strip()) <= 180 or label.get("unit") not in ("g","ml"):
        raise ValueError()
    for key in ("kcal","p","f","c"):
        value = label.get(key)
        if type(value) not in (float, int) or number(value) is None or value > (1000 if key == "kcal" else 100):
            raise ValueError()
    conn.execute("INSERT INTO product_labels VALUES (?,?) ON CONFLICT(code) DO UPDATE SET body=excluded.body",
                 (code, json.dumps(label, ensure_ascii=False)))
