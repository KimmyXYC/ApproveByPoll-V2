from utils.i18n.en_us import MESSAGES as EN_US_MESSAGES
from utils.i18n.fr_fr import MESSAGES as FR_FR_MESSAGES
from utils.i18n.ru_ru import MESSAGES as RU_RU_MESSAGES
from utils.i18n.zh_cn import MESSAGES as ZH_CN_MESSAGES
from utils.i18n.zh_tw import MESSAGES as ZH_TW_MESSAGES

SUPPORTED_LANGUAGES = {
    "en_US": EN_US_MESSAGES,
    "fr_FR": FR_FR_MESSAGES,
    "ru_RU": RU_RU_MESSAGES,
    "zh_CN": ZH_CN_MESSAGES,
    "zh_TW": ZH_TW_MESSAGES,
}

LANGUAGE_LABELS = {
    "zh_CN": "\U0001f1e8\U0001f1f3 简体中文",
    "zh_TW": "\U0001f1f9\U0001f1fc 繁體中文",
    "en_US": "\U0001f1fa\U0001f1f8 English",
    "fr_FR": "\U0001f1eb\U0001f1f7 Français",
    "ru_RU": "\U0001f1f7\U0001f1fa Русский",
}


def normalize_language_code(language: str | None) -> str:
    if not language:
        return "en_US"
    language = language.replace("-", "_")
    if language in SUPPORTED_LANGUAGES:
        return language
    aliases = {
        "zh": "zh_CN",
        "en": "en_US",
        "fr": "fr_FR",
        "ru": "ru_RU",
        "zh_Hans": "zh_CN",
        "zh_Hant": "zh_TW",
    }
    return aliases.get(language, "en_US")


def t(language: str | None, key: str, **kwargs) -> str:
    language = normalize_language_code(language)
    template = SUPPORTED_LANGUAGES[language].get(key)
    if template is None:
        template = SUPPORTED_LANGUAGES["en_US"].get(key, key)
    return template.format(**kwargs)
