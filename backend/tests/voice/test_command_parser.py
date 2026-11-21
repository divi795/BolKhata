import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.voice.command_parser import Intent, ParsedCommand, parse_command


def assert_ledger(result, intent, name, amount, offset=0):
    assert isinstance(result, ParsedCommand)
    assert result.intent == intent
    assert result.customer_name == name
    assert result.amount == amount
    assert result.date_offset == offset
    assert result.is_correction is False
    assert result.confidence == "HIGH"


class TestHindiCredit:
    @pytest.mark.parametrize(
        "text,name,amount",
        [
            ("Ramesh ko 500 rupaye udhaar", "Ramesh", 500.0),
            ("ramesh ko 500 rupaye udhar", "Ramesh", 500.0),
            ("Ramesh ko 500 udhaar karo", "Ramesh", 500.0),
            ("Ramesh ko 500 udhaar kiya", "Ramesh", 500.0),
            ("Suresh ko do sau rupaye credit", "Suresh", 200.0),
            ("Mohan ne 750 rupaye liya", "Mohan", 750.0),
            ("Ramesh ko 200 diye", "Ramesh", 200.0),
        ],
    )
    def test_credit_commands(self, text, name, amount):
        assert_ledger(parse_command(text), Intent.CREDIT, name, amount)


class TestHindiPayment:
    @pytest.mark.parametrize(
        "text,name,amount",
        [
            ("Ramesh ne 200 rupaye jama kiye", "Ramesh", 200.0),
            ("Ramesh ne 200 diye", "Ramesh", 200.0),
            ("Ramesh ne 200 diya", "Ramesh", 200.0),
            ("Suresh se 100 mile", "Suresh", 100.0),
            ("Ramesh ko 500 wapas", "Ramesh", 500.0),
            ("Ramesh ko 500 bhugtaan", "Ramesh", 500.0),
            ("Ramesh ne paanch sau diye", "Ramesh", 500.0),
        ],
    )
    def test_payment_commands(self, text, name, amount):
        assert_ledger(parse_command(text), Intent.PAYMENT, name, amount)


class TestHinglishMixed:
    def test_credit_keyword_overrides_diya(self):
        result = parse_command("Ramesh ko 500 udhaar, diya")
        assert_ledger(result, Intent.CREDIT, "Ramesh", 500.0)

    def test_payment_keyword_with_english_noun(self):
        assert_ledger(parse_command("Ramesh ko 500 jama"), Intent.PAYMENT, "Ramesh", 500.0)

    def test_mixed_rupees_and_credit(self):
        assert_ledger(
            parse_command("Ramesh ko 500 rupees ka udhaar"),
            Intent.CREDIT,
            "Ramesh",
            500.0,
        )

    def test_payment_strong_keyword_beats_credit_verb(self):
        assert_ledger(parse_command("Ramesh ne 100 liya aur jama"), Intent.PAYMENT, "Ramesh", 100.0)


class TestEnglish:
    @pytest.mark.parametrize(
        "text,intent,name,amount",
        [
            ("Amit paid 200 rupees", Intent.PAYMENT, "Amit", 200.0),
            ("500 rupees credit for Amit", Intent.CREDIT, "Amit", 500.0),
            ("payment from Suresh 300", Intent.PAYMENT, "Suresh", 300.0),
            ("200 given for Suresh", Intent.CREDIT, "Suresh", 200.0),
        ],
    )
    def test_english_commands(self, text, intent, name, amount):
        assert_ledger(parse_command(text), intent, name, amount)

    def test_english_history_query(self):
        result = parse_command("show transactions of Ramesh")
        assert_ledger(result, Intent.HISTORY_QUERY, "Ramesh", None)


class TestAmountExtraction:
    @pytest.mark.parametrize(
        "text,amount",
        [
            ("Ramesh ko 500 udhaar", 500.0),
            ("Ramesh ko rs 750 udhaar", 750.0),
            ("Ramesh ko inr 900 udhaar", 900.0),
            ("Ramesh ko \u20b9700 udhaar", 700.0),
            ("Ramesh ko sau rupaye udhaar", 100.0),
            ("Ramesh ko do sau rupaye udhaar", 200.0),
            ("Ramesh ko paanch sau pachaas rupaye udhaar", 550.0),
            ("Ramesh ko ek hazaar udhaar", 1000.0),
            ("Ramesh ko do hazaar paanch sau udhaar", 2500.0),
            ("Ramesh ko dedh hazaar udhaar", 1500.0),
            ("Ramesh ko dhai sau udhaar", 250.0),
        ],
    )
    def test_amount_values(self, text, amount):
        assert_ledger(parse_command(text), Intent.CREDIT, "Ramesh", amount)

    def test_amount_before_name(self):
        assert_ledger(parse_command("500 rupaye Ramesh ko udhaar"), Intent.CREDIT, "Ramesh", 500.0)

    def test_amount_absent_is_not_guessed(self):
        result = parse_command("Ramesh ko udhaar")
        assert result.amount is None
        assert result.intent == Intent.CLARIFICATION_NEEDED


class TestNameExtraction:
    @pytest.mark.parametrize(
        "text,name",
        [
            ("Ramesh Kumar ko 500 udhaar", "Ramesh Kumar"),
            ("ramesh kumar ko 500 udhaar", "Ramesh Kumar"),
            ("500 rupaye Ramesh ko udhaar", "Ramesh"),
            ("udhaar Ramesh ko 500 rupaye", "Ramesh"),
            ("Ramesh se 500 udhaar", "Ramesh"),
            ("  Ramesh   ko   500   udhaar  ", "Ramesh"),
            ("Ramesh ko 500 udhaar\n", "Ramesh"),
        ],
    )
    def test_customer_names(self, text, name):
        assert_ledger(parse_command(text), Intent.CREDIT, name, 500.0)


class TestCorrections:
    @pytest.mark.parametrize(
        "text,amount",
        [
            ("nahi, 430", 430.0),
            ("nhi, 250", 250.0),
            ("nahi 300 rupaye", 300.0),
            ("matlab 300 rupaye", 300.0),
            ("actually 250", 250.0),
            ("sorry, 150", 150.0),
            ("i mean 400", 400.0),
            ("correction: 500", 500.0),
        ],
    )
    def test_correction_markers(self, text, amount):
        result = parse_command(text)
        assert result.intent == Intent.CORRECTION
        assert result.is_correction is True
        assert result.amount == amount
        assert result.customer_name is None
        assert result.confidence == "HIGH"
        assert result.raw_text == text

    def test_correction_after_full_command(self):
        result = parse_command("Ramesh ko 500 rupaye udhaar, nahi 300")
        assert result.intent == Intent.CORRECTION
        assert result.is_correction is True
        assert result.amount == 300.0
        assert result.raw_text == "Ramesh ko 500 rupaye udhaar, nahi 300"

    def test_marker_without_amount_is_not_correction(self):
        result = parse_command("nahi yaar")
        assert result.intent == Intent.UNKNOWN
        assert result.is_correction is False
        assert result.amount is None


class TestRelativeDates:
    @pytest.mark.parametrize(
        "text,offset",
        [
            ("aaj Ramesh ko 500 udhaar", 0),
            ("kal Ramesh ko 500 udhaar", -1),
            ("parso Ramesh ko 500 udhaar", -2),
            ("today Ramesh ko 500 udhaar", 0),
            ("yesterday Ramesh ko 500 udhaar", -1),
        ],
    )
    def test_date_offsets(self, text, offset):
        assert_ledger(parse_command(text), Intent.CREDIT, "Ramesh", 500.0, offset=offset)

    def test_day_before_yesterday_offset(self):
        result = parse_command("day before yesterday Ramesh ko 500 udhaar")
        assert result.date_offset == -2
        assert result.intent == Intent.CREDIT
        assert result.amount == 500.0

    def test_no_date_defaults_to_today(self):
        assert parse_command("Ramesh ko 500 udhaar").date_offset == 0


class TestClarificationRequests:
    def test_missing_name_and_amount(self):
        result = parse_command("udhaar")
        assert result.intent == Intent.CLARIFICATION_NEEDED
        assert result.customer_name is None
        assert result.amount is None
        assert result.confidence == "LOW"
        assert "naam" in result.clarification_prompt
        assert "paisa" in result.clarification_prompt

    def test_missing_name_keeps_amount(self):
        result = parse_command("500 udhaar")
        assert result.intent == Intent.CLARIFICATION_NEEDED
        assert result.customer_name is None
        assert result.amount == 500.0
        assert result.confidence == "LOW"
        assert "Kiske liye" in result.clarification_prompt

    def test_missing_amount_keeps_name(self):
        result = parse_command("Ramesh ko udhaar")
        assert result.intent == Intent.CLARIFICATION_NEEDED
        assert result.customer_name == "Ramesh"
        assert result.amount is None
        assert result.confidence == "LOW"
        assert "Ramesh" in result.clarification_prompt
        assert "kitna" in result.clarification_prompt.lower()

    def test_ambiguous_balance_query(self):
        result = parse_command("kitna baaki hai")
        assert result.intent == Intent.CLARIFICATION_NEEDED
        assert result.customer_name is None
        assert "balance" in result.clarification_prompt

    def test_ambiguous_history_query(self):
        result = parse_command("history")
        assert result.intent == Intent.CLARIFICATION_NEEDED
        assert result.customer_name is None
        assert "history" in result.clarification_prompt


class TestBalanceAndHistory:
    @pytest.mark.parametrize(
        "text,intent",
        [
            ("Ramesh ka balance kitna hai", Intent.BALANCE_QUERY),
            ("Ramesh ka hisab batao", Intent.BALANCE_QUERY),
            ("Ramesh ki history dikhao", Intent.HISTORY_QUERY),
        ],
    )
    def test_queries_with_customer(self, text, intent):
        result = parse_command(text)
        assert result.intent == intent
        assert result.customer_name == "Ramesh"
        assert result.amount is None
        assert result.confidence == "HIGH"


class TestNewCustomer:
    @pytest.mark.parametrize(
        "text",
        ["naya customer Ramesh", "add customer Suresh", "new customer", "khata kholo"],
    )
    def test_new_customer_phrases(self, text):
        result = parse_command(text)
        assert result.intent == Intent.NEW_CUSTOMER
        assert result.confidence == "HIGH"


class TestUnknownAndSafety:
    @pytest.mark.parametrize("text", ["hello there", "namaste", "!!!", "\U0001F600"])
    def test_unrecognised_input_is_unknown(self, text):
        result = parse_command(text)
        assert result.intent == Intent.UNKNOWN
        assert result.confidence == "LOW"

    def test_empty_input_asks_to_repeat(self):
        result = parse_command("")
        assert result.intent == Intent.UNKNOWN
        assert result.confidence == "LOW"
        assert result.clarification_prompt is not None

    def test_whitespace_input_preserves_raw_text(self):
        result = parse_command("   ")
        assert result.intent == Intent.UNKNOWN
        assert result.raw_text == "   "

    def test_none_input_is_safe(self):
        result = parse_command(None)
        assert result.intent == Intent.UNKNOWN
        assert result.raw_text == ""

    def test_bare_number_is_unknown_with_amount(self):
        result = parse_command("12345")
        assert result.intent == Intent.UNKNOWN
        assert result.amount == 12345.0

    def test_parser_never_raises_on_odd_input(self):
        for text in ["a\nb\tc", "\u0905\u091c\u092f \u0915\u094b 500 \u0909\u0927\u093e\u0930", " " * 50]:
            result = parse_command(text)
            assert isinstance(result, ParsedCommand)


class TestResultContract:
    def test_raw_text_is_preserved_verbatim(self):
        text = "Ramesh ko 500 Rupaye UDHAAR!"
        assert parse_command(text).raw_text == text

    def test_parsing_is_deterministic(self):
        text = "kal Ramesh Kumar ko 250 rupaye udhaar"
        assert parse_command(text) == parse_command(text)

    def test_confidence_values_are_from_allowed_set(self):
        for text in ["Ramesh ko 500 udhaar", "hello", "udhaar", "nahi, 430"]:
            assert parse_command(text).confidence in {"HIGH", "MEDIUM", "LOW"}

    def test_successful_ledger_command_has_no_clarification_prompt(self):
        result = parse_command("Ramesh ko 500 udhaar")
        assert result.clarification_prompt is None
        assert result.description is None

# ---------------------------------------------------------------------------
# Devanagari Hindi
# ---------------------------------------------------------------------------

class TestDevanagari:
    def test_credit_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा को 500 रुपए उधार दिए।")
        assert result.intent == Intent.CREDIT
        assert result.customer_name == "Rahul Sharma"
        assert result.amount == 500.0

    def test_payment_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा ने 200 रुपए जमा किए।")
        assert result.intent == Intent.PAYMENT
        assert result.customer_name == "Rahul Sharma"
        assert result.amount == 200.0

    def test_balance_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा का बैलेंस कितना है?")
        assert result.intent == Intent.BALANCE_QUERY
        assert result.customer_name == "Rahul Sharma"

    def test_history_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा की हिस्ट्री दिखाओ।")
        assert result.intent == Intent.HISTORY_QUERY
        assert result.customer_name == "Rahul Sharma"

    def test_canonical_name_equivalence(self):
        from app.voice.command_parser import _transliterate_devanagari
        from app.voice.orchestrator import _canonical_name
        assert _transliterate_devanagari("राहुल शर्मा") == "rahul sharma"
        assert _canonical_name("राहुल शर्मा") == _canonical_name("Rahul Sharma")

    def test_devanagari_digits(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा को ५०० रुपए उधार दिए।")
        assert result.intent == Intent.CREDIT
        assert result.amount == 500.0

# ---------------------------------------------------------------------------
# Devanagari Hindi
# ---------------------------------------------------------------------------

class TestDevanagari:
    def test_credit_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा को 500 रुपए उधार दिए।")
        assert result.intent == Intent.CREDIT
        assert result.customer_name == "Rahul Sharma"
        assert result.amount == 500.0

    def test_payment_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा ने 200 रुपए जमा किए।")
        assert result.intent == Intent.PAYMENT
        assert result.customer_name == "Rahul Sharma"
        assert result.amount == 200.0

    def test_balance_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा का बैलेंस कितना है?")
        assert result.intent == Intent.BALANCE_QUERY
        assert result.customer_name == "Rahul Sharma"

    def test_history_command(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा की हिस्ट्री दिखाओ।")
        assert result.intent == Intent.HISTORY_QUERY
        assert result.customer_name == "Rahul Sharma"

    def test_canonical_name_equivalence(self):
        from app.voice.command_parser import _transliterate_devanagari
        from app.voice.orchestrator import _canonical_name
        assert _transliterate_devanagari("राहुल शर्मा") == "rahul sharma"
        assert _canonical_name("राहुल शर्मा") == _canonical_name("Rahul Sharma")

    def test_devanagari_digits(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("राहुल शर्मा को ५०० रुपए उधार दिए।")
        assert result.intent == Intent.CREDIT
        assert result.amount == 500.0

class TestDevanagariCorrection:
    def test_devanagari_correction_with_comma(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("नहीं, 430 रुपये।")
        assert result.intent == Intent.CORRECTION
        assert result.amount == 430.0
        assert result.is_correction is True

    def test_devanagari_correction_without_comma(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("नहीं 430")
        assert result.intent == Intent.CORRECTION
        assert result.amount == 430.0
        assert result.is_correction is True

    def test_romanized_correction_equivalent(self):
        from app.voice.command_parser import parse_command, Intent
        result = parse_command("nahi, 430")
        assert result.intent == Intent.CORRECTION
        assert result.amount == 430.0
        assert result.is_correction is True


def test_parse_commands_keeps_correction_phrase_together():
    from app.voice.command_parser import parse_commands

    commands = parse_commands("\u0930\u093e\u0939\u0941\u0932 \u0915\u094b 100 \u0930\u0941\u092a\u092f\u0947 \u0909\u0927\u093e\u0930 \u0926\u093f\u090f, \u0905\u0930\u0947 \u0928\u0939\u0940\u0902, 150 \u0930\u0941\u092a\u092f\u0947")

    assert len(commands) == 2
    assert commands[1].intent.value == 'CORRECTION'
    assert commands[1].amount == 150.0



def test_parse_commands_keeps_devanagari_correction_together():
    from app.voice.command_parser import parse_commands

    commands = parse_commands("\u0930\u093e\u0939\u0941\u0932 \u0915\u094b 100 \u0930\u0941\u092a\u092f\u0947 \u0909\u0927\u093e\u0930 \u0926\u093f\u090f, \u0905\u0930\u0947 \u0928\u0939\u0940\u0902, 150 \u0930\u0941\u092a\u092f\u0947")

    assert len(commands) == 2
    assert commands[0].intent.value == 'CREDIT'
    assert commands[0].amount == 100.0
    assert commands[1].intent.value == 'CORRECTION'
    assert commands[1].amount == 150.0


def test_parse_commands_keeps_roman_correction_together():
    from app.voice.command_parser import parse_commands

    commands = parse_commands('Rahul ko 100 rupaye udhaar, nahi, 150 rupaye')

    assert len(commands) == 2
    assert commands[0].intent.value == 'CREDIT'
    assert commands[0].amount == 100.0
    assert commands[1].intent.value == 'CORRECTION'
    assert commands[1].amount == 150.0



def test_devanagari_history_query_len_den_itih():
    from app.voice.command_parser import parse_command

    cmd = parse_command('\u0930\u093e\u0939\u0941\u0932 \u0915\u093e \u0932\u0947\u0928-\u0926\u0947\u0928 \u0915\u093e \u0907\u0924\u093f\u0939\u093e\u0938 \u092c\u0924\u093e\u0913')
    assert cmd.intent.value == 'HISTORY_QUERY'
    assert cmd.customer_name == 'Rahul'


def test_devanagari_history_query_history_batao():
    from app.voice.command_parser import parse_command

    cmd = parse_command('\u0930\u093e\u0939\u0941\u0932 \u0915\u0940 history \u092c\u0924\u093e\u0913')
    assert cmd.intent.value == 'HISTORY_QUERY'
    assert cmd.customer_name == 'Rahul'



def test_aggregate_credit_queries():
    from app.voice.command_parser import parse_command

    for text in ['Aaj kitna udhaar gaya?', 'Aaj total kitna udhaar diya?', 'Kal kitna udhaar gaya?']: 
        assert parse_command(text).intent.value == 'DAILY_CREDIT_TOTAL'


def test_aggregate_payment_queries():
    from app.voice.command_parser import parse_command

    for text in ['Aaj kitna jama hua?', 'Aaj total payment kitna mila?', 'Kal kitna jama hua?']: 
        assert parse_command(text).intent.value == 'DAILY_PAYMENT_TOTAL'



def test_devanagari_daily_credit_aggregate_query():
    from app.voice.command_parser import parse_command

    cmd = parse_command('\u0906\u091c \u0915\u093f\u0924\u0928\u093e \u0909\u0927\u093e\u0930 \u0917\u092f\u093e?')
    assert cmd.intent.value == 'DAILY_CREDIT_TOTAL'


def test_devanagari_daily_payment_aggregate_query():
    from app.voice.command_parser import parse_command

    cmd = parse_command('\u0906\u091c \u0915\u093f\u0924\u0928\u093e \u091c\u092e\u093e \u0939\u0941\u0906?')
    assert cmd.intent.value == 'DAILY_PAYMENT_TOTAL'


def test_roman_daily_credit_aggregate_query():
    from app.voice.command_parser import parse_command

    cmd = parse_command('Aaj kitna udhaar gaya?')
    assert cmd.intent.value == 'DAILY_CREDIT_TOTAL'


def test_roman_daily_payment_aggregate_query():
    from app.voice.command_parser import parse_command

    cmd = parse_command('Aaj kitna jama hua?')
    assert cmd.intent.value == 'DAILY_PAYMENT_TOTAL'

