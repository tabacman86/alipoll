import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_TRANSLATE_LANG = os.getenv("TRANSLATE_LANG", "iw")

# Hardcoded Hebrew status translations
_STATUS_HE: dict[str, str] = {
    "awaiting delivery":    "ממתין למסירה",
    "delivered":            "נמסר",
    "out for delivery":     "יצא למסירה",
    "ready to ship":        "מוכן למשלוח",
    "in transit":           "בדרך",
    "shipped":              "נשלח",
    "on the way":           "בדרך",
    "delivering":           "במסירה",
    "arrived at destination": "הגיע ליעד",
    "packed":               "ארוז",
    "dispatched":           "נשלח",
    "processing":           "בעיבוד",
    "payment successful":   "תשלום בוצע",
    "payment pending":      "ממתין לתשלום",
    "completed":            "הושלם",
    "cancelled":            "בוטל",
    "closed":               "סגור",
    "unknown":              "לא ידוע",
    "new":                  "חדש",
}


def translate_status(status: str) -> str:
    return _STATUS_HE.get(status.lower(), status)


async def translate_item_name(name: str, store) -> str:
    """Translate an item name to Hebrew, using DB cache."""
    cached = await store.get_translation(name)
    if cached:
        return cached

    try:
        translated = await asyncio.to_thread(_google_translate, name)
        if translated and translated != name:
            await store.save_translation(name, translated)
            return translated
    except Exception as e:
        logger.debug("Translation failed for '%s': %s", name[:40], e)

    return name  # fallback to original


def _google_translate(text: str) -> str:
    from deep_translator import GoogleTranslator
    result = GoogleTranslator(source="auto", target=_TRANSLATE_LANG).translate(text)
    return result or text
