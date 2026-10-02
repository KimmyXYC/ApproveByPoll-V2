import re

from telebot import types

from utils.i18n import LANGUAGE_LABELS, normalize_language_code, t
from utils.database import BotDatabase
from utils.database_defaults import MIN_VOTE_TIME, MAX_VOTE_TIME
from utils.duration import format_duration as _format_vote_time

TOGGLE_ITEMS = [
    "vote_to_join",
    "anonymous_vote",
    "pin_msg",
    "clean_pinned_message",
    "advanced_vote",
]
VOTE_TIME_OPTIONS = [
    60,
    120,
    300,
    600,
    900,
    1200,
    1800,
    2700,
    3600,
    6 * 3600,
    12 * 3600,
    86400,
    3 * 86400,
    7 * 86400,
    14 * 86400,
    MAX_VOTE_TIME,
]
MINI_VOTERS_OPTIONS = [1, 2, 3, 5, 10, 20, 50, 100, 200]


def _to_bool(value: str) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


async def _can_change_group_info(bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    if member.status == "creator":
        return True
    if member.status != "administrator":
        return False
    return bool(getattr(member, "can_change_info", False))


async def _bot_can_delete_messages(bot, chat_id: int) -> bool:
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        if bot_member.status == "creator":
            return True
        if bot_member.status != "administrator":
            return False
        return bool(getattr(bot_member, "can_delete_messages", False))
    except Exception:
        return False


async def _bot_can_pin_messages(bot, chat_id: int) -> bool:
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        if bot_member.status == "creator":
            return True
        if bot_member.status != "administrator":
            return False
        return bool(getattr(bot_member, "can_pin_messages", False))
    except Exception:
        return False


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time_seconds(value: str) -> int | None:
    direct_int = _parse_int(value)
    if direct_int is not None:
        return direct_int

    normalized = value.strip().lower()
    match = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", normalized)
    if not match:
        return None

    if not any(match.groups()):
        return None
    return sum(
        int(part or 0) * size
        for part, size in zip(match.groups(), (86400, 3600, 60, 1))
    )


async def _handle_setting_command_with_args(
    bot,
    message: types.Message,
    language: str,
    group_id: int,
    is_anonymous_admin: bool = False,
) -> bool:
    text = (message.text or "").strip()
    parts = text.split()

    if len(parts) == 1:
        return False

    if len(parts) != 3:
        await bot.reply_to(message, t(language, "setting_command_usage"))
        return True

    item = parts[1].lower()
    raw_value = parts[2]

    if is_anonymous_admin and item in {"time", "voter", "mini_voters"}:
        await bot.reply_to(message, t(language, "setting_anonymous_admin_not_allowed"))
        return True

    if item == "time":
        parsed_value = _parse_time_seconds(raw_value)
        if parsed_value is None:
            await bot.reply_to(message, t(language, "setting_invalid_integer"))
            return True
        if not MIN_VOTE_TIME <= parsed_value <= MAX_VOTE_TIME:
            await bot.reply_to(message, t(language, "setting_time_out_of_range"))
            return True
        await BotDatabase.update_group_setting(
            group_id=group_id,
            item="vote_time",
            value=parsed_value,
        )
        await bot.reply_to(
            message,
            t(
                language,
                "setting_time_updated",
                value=_format_vote_time(language, parsed_value),
            ),
        )
        return True

    if item in {"voter", "mini_voters"}:
        parsed_value = _parse_int(raw_value)
        if parsed_value is None:
            await bot.reply_to(message, t(language, "setting_invalid_integer"))
            return True
        if not 1 <= parsed_value <= 500:
            await bot.reply_to(message, t(language, "setting_voter_out_of_range"))
            return True
        await BotDatabase.update_group_setting(
            group_id=group_id,
            item="mini_voters",
            value=parsed_value,
        )
        await bot.reply_to(
            message,
            t(language, "setting_voter_updated", value=parsed_value),
        )
        return True

    await bot.reply_to(message, t(language, "setting_command_usage"))
    return True


def _build_settings_text(group_settings: dict) -> str:
    language = normalize_language_code(group_settings.get("language"))
    lines = [
        t(language, "setting_title"),
        f"{t(language, 'setting_vote_to_join')}: {'ON' if group_settings.get('vote_to_join') else 'OFF'}",
        f"{t(language, 'setting_anonymous_vote')}: {'ON' if group_settings.get('anonymous_vote') else 'OFF'}",
        f"{t(language, 'setting_pin_msg')}: {'ON' if group_settings.get('pin_msg') else 'OFF'}",
        f"{t(language, 'setting_clean_pinned_message')}: {'ON' if group_settings.get('clean_pinned_message') else 'OFF'}",
        f"{t(language, 'setting_advanced_vote')}: {'ON' if group_settings.get('advanced_vote') else 'OFF'}",
        f"{t(language, 'setting_vote_time')}: {_format_vote_time(language, int(group_settings.get('vote_time', 600)))}",
        f"{t(language, 'setting_mini_voters')}: {int(group_settings.get('mini_voters', 3))}",
        f"{t(language, 'setting_language')}: {LANGUAGE_LABELS.get(language, 'English')}",
    ]
    return "\n".join(lines)


def build_main_keyboard(group_settings: dict) -> types.InlineKeyboardMarkup:
    language = normalize_language_code(group_settings.get("language"))
    group_id = group_settings["group_id"]
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    vote_to_join = bool(group_settings.get("vote_to_join", True))
    vote_to_join_icon = "✅" if vote_to_join else "❌"
    keyboard.add(
        types.InlineKeyboardButton(
            f"{vote_to_join_icon} {t(language, 'setting_vote_to_join')}",
            callback_data=f"setting {group_id} vote_to_join {str(not vote_to_join).lower()}",
        )
    )

    two_column_buttons = []
    for item in TOGGLE_ITEMS:
        if item == "vote_to_join":
            continue
        current_value = bool(group_settings.get(item, False))
        icon = "✅" if current_value else "❌"
        two_column_buttons.append(
            types.InlineKeyboardButton(
                f"{icon} {t(language, f'setting_{item}')}",
                callback_data=f"setting {group_id} {item} {str(not current_value).lower()}",
            )
        )

    two_column_buttons.extend(
        [
            types.InlineKeyboardButton(
                f"⏱️ {t(language, 'setting_vote_time')}",
                callback_data=f"setting {group_id} vote_time menu",
            ),
            types.InlineKeyboardButton(
                f"👥 {t(language, 'setting_mini_voters')}",
                callback_data=f"setting {group_id} mini_voters menu",
            ),
            types.InlineKeyboardButton(
                f"🌐 {t(language, 'setting_language')}",
                callback_data=f"setting {group_id} language menu",
            ),
        ]
    )

    keyboard.add(*two_column_buttons)

    keyboard.add(
        types.InlineKeyboardButton(
            f"✖️ {t(language, 'setting_close')}",
            callback_data=f"setting {group_id} close true",
        )
    )
    return keyboard


def build_vote_time_keyboard(group_settings: dict) -> types.InlineKeyboardMarkup:
    language = normalize_language_code(group_settings.get("language"))
    group_id = group_settings["group_id"]
    current_vote_time = int(group_settings.get("vote_time", 600))
    keyboard = types.InlineKeyboardMarkup(row_width=3)

    buttons = []
    for option in VOTE_TIME_OPTIONS:
        label = _format_vote_time(language, option)
        if option == current_vote_time:
            label = f"✅ {label}"
        buttons.append(
            types.InlineKeyboardButton(
                label,
                callback_data=f"setting {group_id} vote_time {option}",
            )
        )
    keyboard.add(*buttons)
    keyboard.add(
        types.InlineKeyboardButton(
            f"↩️ {t(language, 'setting_back')}",
            callback_data=f"setting {group_id} back main",
        )
    )
    return keyboard


def build_language_keyboard(group_settings: dict) -> types.InlineKeyboardMarkup:
    language = normalize_language_code(group_settings.get("language"))
    group_id = group_settings["group_id"]
    current_language = normalize_language_code(group_settings.get("language"))

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    preferred_order = ["zh_CN", "zh_TW", "en_US", "fr_FR", "ru_RU"]
    ordered_codes = [code for code in preferred_order if code in LANGUAGE_LABELS]
    ordered_codes.extend(code for code in LANGUAGE_LABELS if code not in ordered_codes)

    for code in ordered_codes:
        label = LANGUAGE_LABELS[code]
        if code == current_language:
            label = f"✅ {label}"
        keyboard.add(
            types.InlineKeyboardButton(
                label,
                callback_data=f"setting {group_id} language {code}",
            )
        )
    keyboard.add(
        types.InlineKeyboardButton(
            f"↩️ {t(language, 'setting_back')}",
            callback_data=f"setting {group_id} back main",
        )
    )
    return keyboard


def build_mini_voters_keyboard(group_settings: dict) -> types.InlineKeyboardMarkup:
    language = normalize_language_code(group_settings.get("language"))
    group_id = group_settings["group_id"]
    current_value = int(group_settings.get("mini_voters", 3))

    keyboard = types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for option in MINI_VOTERS_OPTIONS:
        label = str(option)
        if option == current_value:
            label = f"✅ {label}"
        buttons.append(
            types.InlineKeyboardButton(
                label,
                callback_data=f"setting {group_id} mini_voters {option}",
            )
        )
    keyboard.add(*buttons)
    keyboard.add(
        types.InlineKeyboardButton(
            f"↩️ {t(language, 'setting_back')}",
            callback_data=f"setting {group_id} back main",
        )
    )
    return keyboard


async def open_settings(bot, message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return

    group_settings = await BotDatabase.get_group_settings(message.chat.id)
    language = normalize_language_code(group_settings.get("language"))

    is_anonymous_admin = bool(
        message.sender_chat
        and message.chat
        and message.sender_chat.id == message.chat.id
        and message.chat.type in ["group", "supergroup"]
    )

    if is_anonymous_admin:
        handled = await _handle_setting_command_with_args(
            bot=bot,
            message=message,
            language=language,
            group_id=message.chat.id,
            is_anonymous_admin=True,
        )
        if handled:
            return

        await bot.reply_to(
            message=message,
            text=_build_settings_text(group_settings),
            reply_markup=build_main_keyboard(group_settings),
        )
        return

    if not message.from_user:
        return

    has_permission = await _can_change_group_info(
        bot, message.chat.id, message.from_user.id
    )
    if not has_permission:
        await bot.reply_to(message, t(language, "insufficient_permissions"))
        return

    handled = await _handle_setting_command_with_args(
        bot=bot,
        message=message,
        language=language,
        group_id=message.chat.id,
        is_anonymous_admin=False,
    )
    if handled:
        return

    await bot.reply_to(
        message=message,
        text=_build_settings_text(group_settings),
        reply_markup=build_main_keyboard(group_settings),
    )


async def handle_settings_callback(bot, call: types.CallbackQuery):
    if not call.message or not call.data or not call.from_user:
        return

    parts = call.data.split(" ")
    if len(parts) != 4 or parts[0] != "setting":
        return

    group_id = int(parts[1])
    item = parts[2]
    status = parts[3]

    if call.message.chat.id != group_id:
        await bot.answer_callback_query(
            callback_query_id=call.id, text="Invalid target"
        )
        return

    group_settings = await BotDatabase.get_group_settings(group_id)
    language = normalize_language_code(group_settings.get("language"))

    has_permission = await _can_change_group_info(bot, group_id, call.from_user.id)
    if not has_permission:
        await bot.answer_callback_query(
            callback_query_id=call.id,
            text=t(language, "insufficient_permissions"),
            show_alert=True,
        )
        return

    if item == "close":
        await bot.delete_message(call.message.chat.id, call.message.message_id)
        await bot.answer_callback_query(callback_query_id=call.id)
        return

    if item == "back" and status == "main":
        group_settings = await BotDatabase.get_group_settings(group_id)
        await bot.edit_message_text(
            text=_build_settings_text(group_settings),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_main_keyboard(group_settings),
        )
        await bot.answer_callback_query(callback_query_id=call.id)
        return

    if item == "vote_time" and status == "menu":
        await bot.edit_message_text(
            text="\n".join(
                [
                    t(language, "setting_vote_time_menu"),
                    t(
                        language,
                        "setting_current_value",
                        value=_format_vote_time(
                            language,
                            int(group_settings.get("vote_time", 600)),
                        ),
                    ),
                ]
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_vote_time_keyboard(group_settings),
        )
        await bot.answer_callback_query(callback_query_id=call.id)
        return

    if item == "language" and status == "menu":
        await bot.edit_message_text(
            text="\n".join(
                [
                    t(language, "setting_language_menu"),
                    t(
                        language,
                        "setting_current_value",
                        value=LANGUAGE_LABELS.get(
                            language, "\U0001f1fa\U0001f1f8 English"
                        ),
                    ),
                ]
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_language_keyboard(group_settings),
        )
        await bot.answer_callback_query(callback_query_id=call.id)
        return

    if item == "mini_voters" and status == "menu":
        await bot.edit_message_text(
            text="\n".join(
                [
                    t(language, "setting_mini_voters_menu"),
                    t(
                        language,
                        "setting_current_value",
                        value=str(int(group_settings.get("mini_voters", 3))),
                    ),
                ]
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_mini_voters_keyboard(group_settings),
        )
        await bot.answer_callback_query(callback_query_id=call.id)
        return

    if item in TOGGLE_ITEMS:
        bool_value = _to_bool(status)
        if bool_value is None:
            await bot.answer_callback_query(
                callback_query_id=call.id, text="Invalid value"
            )
            return

        if item == "clean_pinned_message" and bool_value:
            can_delete = await _bot_can_delete_messages(bot, group_id)
            if not can_delete:
                await bot.answer_callback_query(
                    callback_query_id=call.id,
                    text=t(language, "insufficient_permissions"),
                    show_alert=True,
                )
                return

        if item == "pin_msg" and bool_value:
            can_pin = await _bot_can_pin_messages(bot, group_id)
            if not can_pin:
                await bot.answer_callback_query(
                    callback_query_id=call.id,
                    text=t(language, "insufficient_permissions"),
                    show_alert=True,
                )
                return

        await BotDatabase.update_group_setting(
            group_id=group_id, item=item, value=bool_value
        )
        group_settings = await BotDatabase.get_group_settings(group_id)
        await bot.edit_message_text(
            text=_build_settings_text(group_settings),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_main_keyboard(group_settings),
        )
        await bot.answer_callback_query(
            callback_query_id=call.id,
            text=t(
                normalize_language_code(group_settings.get("language")), "setting_saved"
            ),
        )
        return

    if item == "vote_time":
        if not status.isdigit():
            await bot.answer_callback_query(
                callback_query_id=call.id, text="Invalid value"
            )
            return
        vote_time = int(status)
        if vote_time not in VOTE_TIME_OPTIONS:
            await bot.answer_callback_query(
                callback_query_id=call.id, text="Invalid value"
            )
            return
        await BotDatabase.update_group_setting(
            group_id=group_id, item="vote_time", value=vote_time
        )
        group_settings = await BotDatabase.get_group_settings(group_id)
        language = normalize_language_code(group_settings.get("language"))
        await bot.edit_message_text(
            text="\n".join(
                [
                    t(language, "setting_vote_time_menu"),
                    t(
                        language,
                        "setting_current_value",
                        value=_format_vote_time(language, vote_time),
                    ),
                ]
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_vote_time_keyboard(group_settings),
        )
        await bot.answer_callback_query(
            callback_query_id=call.id,
            text=t(language, "setting_saved"),
        )
        return

    if item == "language":
        if status not in LANGUAGE_LABELS:
            await bot.answer_callback_query(
                callback_query_id=call.id, text="Invalid value"
            )
            return
        await BotDatabase.update_group_setting(
            group_id=group_id, item="language", value=status
        )
        group_settings = await BotDatabase.get_group_settings(group_id)
        language = normalize_language_code(group_settings.get("language"))
        await bot.edit_message_text(
            text="\n".join(
                [
                    t(language, "setting_language_menu"),
                    t(
                        language,
                        "setting_current_value",
                        value=LANGUAGE_LABELS.get(
                            language, "\U0001f1fa\U0001f1f8 English"
                        ),
                    ),
                ]
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_language_keyboard(group_settings),
        )
        await bot.answer_callback_query(
            callback_query_id=call.id,
            text=t(language, "setting_saved"),
        )
        return

    if item == "mini_voters":
        if not status.isdigit():
            await bot.answer_callback_query(
                callback_query_id=call.id, text="Invalid value"
            )
            return
        mini_voters = int(status)
        if mini_voters not in MINI_VOTERS_OPTIONS:
            await bot.answer_callback_query(
                callback_query_id=call.id, text="Invalid value"
            )
            return
        await BotDatabase.update_group_setting(
            group_id=group_id,
            item="mini_voters",
            value=mini_voters,
        )
        group_settings = await BotDatabase.get_group_settings(group_id)
        language = normalize_language_code(group_settings.get("language"))
        await bot.edit_message_text(
            text="\n".join(
                [
                    t(language, "setting_mini_voters_menu"),
                    t(
                        language,
                        "setting_current_value",
                        value=str(mini_voters),
                    ),
                ]
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_mini_voters_keyboard(group_settings),
        )
        await bot.answer_callback_query(
            callback_query_id=call.id,
            text=t(language, "setting_saved"),
        )
        return

    await bot.answer_callback_query(
        callback_query_id=call.id, text="Unsupported action"
    )
