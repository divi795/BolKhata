"""
BolKhata - Voice Command Orchestrator
=====================================
Turns a parsed voice command into a deterministic service-layer action and a
spoken-style response. No STT/TTS, no routing, no HTTP layer.

Safety rules:
  - A customer is matched only by exact, case-insensitive, trimmed name.
  - Transactions are never created for an unknown or ambiguous customer.
  - Customers are never auto-created during a CREDIT/PAYMENT command.
  - CORRECTION never executes without pending-transaction context.
"""

from __future__ import annotations

import re

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
from difflib import SequenceMatcher

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Customer, Transaction
from app.schemas.customer import CustomerCreate
from app.schemas.transaction import (
    CreditTransactionCreate,
    PaymentTransactionCreate,
    TransactionResponse,
    TransactionType,
)
from app.services.customer_service import (
    create_customer,
    get_customer_balance,
)
from app.services.ledger_service import (
    add_credit_transaction,
    add_payment_transaction,
    get_customer_transactions,
    get_daily_total,
    update_transaction_amount,
)
from app.voice.command_parser import Intent, ParsedCommand, parse_command, parse_commands, _transliterate_devanagari


STATUS_SUCCESS = "success"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_NEEDS_CUSTOMER_CONFIRMATION = "needs_customer_confirmation"
STATUS_NEEDS_CUSTOMER_CLARIFICATION = "needs_customer_clarification"
STATUS_CUSTOMER_NOT_FOUND = "customer_not_found"
STATUS_CUSTOMER_EXISTS = "customer_exists"
STATUS_NEEDS_REPEAT = "needs_repeat"
STATUS_UNKNOWN = "unknown"
STATUS_ERROR = "error"


@dataclass
class VoiceCommandResult:
    """Structured outcome of one voice command."""

    status: str
    response_text: str
    intent: str
    customer: Optional[Dict[str, Any]] = None
    transaction: Optional[Dict[str, Any]] = None
    transactions: Optional[List[Dict[str, Any]]] = None
    last_transaction_id: Optional[int] = None
    pending_customer_clarification: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _customer_payload(customer: Customer) -> Dict[str, Any]:
    return {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
    }


def _transaction_payload(tx: TransactionResponse) -> Dict[str, Any]:
    return {
        "id": tx.id,
        "customer_id": tx.customer_id,
        "type": tx.type.value,
        "amount": tx.amount,
        "description": tx.description,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }


def _canonical_name(name: str) -> str:
    """Canonical, script-independent name used for exact customer matching."""
    value = (name or "").strip()
    if any("\u0900" <= c <= "\u097F" for c in value):
        value = _transliterate_devanagari(value)
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _find_customers_by_name(db: Session, name: str) -> List[Customer]:
    """Find exact, prefix and similar customer-name matches."""
    canonical = _canonical_name(name)
    if not canonical:
        return []

    customers = db.query(Customer).order_by(Customer.id.asc()).all()
    exact = [c for c in customers if _canonical_name(c.name) == canonical]
    if exact:
        # A short name such as "Rahul" should also surface longer names
        # beginning with it, such as "Rahul Sharma".
        prefix = [
            c for c in customers
            if _canonical_name(c.name).startswith(canonical)
            and _canonical_name(c.name) != canonical
        ]
        return exact + prefix

    prefix = [c for c in customers if _canonical_name(c.name).startswith(canonical)]
    if prefix:
        return prefix

    similar = []
    for customer in customers:
        candidate = _canonical_name(customer.name)
        if not candidate:
            continue
        ratio = SequenceMatcher(None, canonical, candidate).ratio()
        if ratio >= 0.82 and abs(len(candidate) - len(canonical)) <= 4:
            similar.append(customer)
    return similar


def _multiple_match_response(
    name: str,
    matches: List[Customer],
    intent_value: str,
    cmd: Optional[ParsedCommand] = None,
    pending_transaction_id: Optional[int] = None,
) -> VoiceCommandResult:
    amount = getattr(cmd, "amount", None) if cmd is not None else None
    date_offset = getattr(cmd, "date_offset", 0) if cmd is not None else 0
    description = getattr(cmd, "description", None) if cmd is not None else None
    options = " ya ".join(c.name for c in matches)
    context = {
        "intent": intent_value,
        "customer_name": name,
        "amount": amount,
        "date_offset": date_offset,
        "description": description,
        "candidate_customers": [{"id": c.id, "name": c.name} for c in matches],
        "pending_transaction_id": pending_transaction_id,
    }
    if intent_value == Intent.CREDIT.value:
        question = f"{options}? Kisko udhaar dena hai?"
    elif intent_value == Intent.PAYMENT.value:
        question = f"{options}? Kiska payment record karna hai?"
    elif intent_value == Intent.BALANCE_QUERY.value:
        question = f"{options}? Kiska balance dekhna hai?"
    elif intent_value == Intent.HISTORY_QUERY.value:
        question = f"{options}? Kiski history dekhni hai?"
    else:
        question = f"{options}? Kaunsa customer hai?"
    return VoiceCommandResult(
        status=STATUS_NEEDS_CUSTOMER_CLARIFICATION,
        response_text=question,
        intent=intent_value,
        customer={"name": name},
        pending_customer_clarification=context,
    )


def _handle_transaction(
    db: Session, cmd: ParsedCommand, intent_value: str
) -> VoiceCommandResult:
    name = (cmd.customer_name or "").strip()
    amount = cmd.amount

    if not name or amount is None or amount <= 0:
        return VoiceCommandResult(
            status=STATUS_NEEDS_CLARIFICATION,
            response_text=(
                "Transaction ke liye customer ka naam aur sahi amount dono "
                "bataye, jaise: 'Ramesh ko 500 rupaye udhaar'."
            ),
            intent=intent_value,
        )

    matches = _find_customers_by_name(db, name)
    if not matches:
        return VoiceCommandResult(
            status=STATUS_NEEDS_CUSTOMER_CONFIRMATION,
            response_text=(
                f"'{name}' naam ka koi customer nahi mila. "
                "Kya aap ise naya customer add karna chahte hain?"
            ),
            intent=intent_value,
            customer={"name": name},
        )
    if len(matches) > 1:
        return _multiple_match_response(name, matches, intent_value, cmd=cmd)

    customer = matches[0]

    try:
        if cmd.intent == Intent.CREDIT:
            tx = add_credit_transaction(
                db,
                customer.id,
                CreditTransactionCreate(
                    amount=amount,
                    description=cmd.description,
                ),
                date_offset=cmd.date_offset,
            )
            label = "udhaar (credit)"
        else:
            tx = add_payment_transaction(
                db,
                customer.id,
                PaymentTransactionCreate(
                    amount=amount,
                    description=cmd.description,
                ),
                date_offset=cmd.date_offset,
            )
            label = "payment (jama)"

        balance = get_customer_balance(db, customer.id)
    except HTTPException as exc:
        db.rollback()
        return VoiceCommandResult(
            status=STATUS_ERROR,
            response_text=f"Transaction record nahi ho paya: {exc.detail}",
            intent=intent_value,
            customer=_customer_payload(customer),
        )

    return VoiceCommandResult(
        status=STATUS_SUCCESS,
        response_text=(
            f"{customer.name} ke liye Rs. {tx.amount:.2f} ka {label} "
            f"record ho gaya. Ab baaki balance Rs. {balance.current_balance:.2f} hai."
        ),
        intent=intent_value,
        customer=_customer_payload(customer),
        transaction=_transaction_payload(tx),
        last_transaction_id=tx.id,
    )


def _handle_balance(db: Session, cmd: ParsedCommand, intent_value: str) -> VoiceCommandResult:
    name = (cmd.customer_name or "").strip()
    if not name:
        return VoiceCommandResult(
            status=STATUS_NEEDS_CLARIFICATION,
            response_text="Kis customer ka balance dekhna hai? Naam bataye.",
            intent=intent_value,
        )

    matches = _find_customers_by_name(db, name)
    if not matches:
        return VoiceCommandResult(
            status=STATUS_CUSTOMER_NOT_FOUND,
            response_text=f"'{name}' naam ka koi customer nahi mila.",
            intent=intent_value,
            customer={"name": name},
        )
    if len(matches) > 1:
        return _multiple_match_response(name, matches, intent_value, cmd=cmd)

    customer = matches[0]
    balance = get_customer_balance(db, customer.id)

    return VoiceCommandResult(
        status=STATUS_SUCCESS,
        response_text=(
            f"{customer.name} ka baaki balance Rs. {balance.current_balance:.2f} hai. "
            f"Kul udhaar Rs. {balance.total_credit:.2f} aur kul jama "
            f"Rs. {balance.total_payment:.2f} hai."
        ),
        intent=intent_value,
        customer=_customer_payload(customer),
    )


def _handle_history(db: Session, cmd: ParsedCommand, intent_value: str) -> VoiceCommandResult:
    name = (cmd.customer_name or "").strip()
    if not name:
        return VoiceCommandResult(
            status=STATUS_NEEDS_CLARIFICATION,
            response_text="Kis customer ki history dekhni hai? Naam bataye.",
            intent=intent_value,
        )

    matches = _find_customers_by_name(db, name)
    if not matches:
        return VoiceCommandResult(
            status=STATUS_CUSTOMER_NOT_FOUND,
            response_text=f"'{name}' naam ka koi customer nahi mila.",
            intent=intent_value,
            customer={"name": name},
        )
    if len(matches) > 1:
        return _multiple_match_response(name, matches, intent_value, cmd=cmd)

    customer = matches[0]
    transactions = get_customer_transactions(db, customer.id)
    payloads = [_transaction_payload(tx) for tx in transactions]

    if not payloads:
        response_text = f"{customer.name} ka abhi koi transaction record nahi hai."
    else:
        lines = []
        for tx in payloads[:5]:
            label = "Udhaar" if tx["type"] == "CREDIT" else "Jama"
            date = (tx["created_at"] or "")[:10]
            lines.append(f"{date} {label} Rs. {tx['amount']:.2f}")
        response_text = (
            f"{customer.name} ke {len(payloads)} transaction mile. "
            f"Recent: " + "; ".join(lines)
        )

    return VoiceCommandResult(
        status=STATUS_SUCCESS,
        response_text=response_text,
        intent=intent_value,
        customer=_customer_payload(customer),
        transactions=payloads,
    )


def _handle_daily_credit_total(db: Session, cmd: ParsedCommand, intent_value: str) -> VoiceCommandResult:
    total = get_daily_total(db, TransactionType.CREDIT, cmd.date_offset)
    label = "Aaj" if cmd.date_offset == 0 else "Kal" if cmd.date_offset == -1 else "Selected din"
    return VoiceCommandResult(
        status=STATUS_SUCCESS,
        response_text=f"{label} total {total:g} rupaye ka udhaar gaya.",
        intent=intent_value,
    )


def _handle_daily_payment_total(db: Session, cmd: ParsedCommand, intent_value: str) -> VoiceCommandResult:
    total = get_daily_total(db, TransactionType.PAYMENT, cmd.date_offset)
    label = "Aaj" if cmd.date_offset == 0 else "Kal" if cmd.date_offset == -1 else "Selected din"
    return VoiceCommandResult(
        status=STATUS_SUCCESS,
        response_text=f"{label} total {total:g} rupaye jama hue.",
        intent=intent_value,
    )


def _handle_new_customer(
    db: Session, cmd: ParsedCommand, intent_value: str
) -> VoiceCommandResult:
    name = (cmd.customer_name or "").strip()
    if not name:
        return VoiceCommandResult(
            status=STATUS_NEEDS_CLARIFICATION,
            response_text="Naye customer ka naam bataye.",
            intent=intent_value,
        )

    matches = _find_customers_by_name(db, name)
    if matches:
        return VoiceCommandResult(
            status=STATUS_CUSTOMER_EXISTS,
            response_text=f"'{matches[0].name}' naam ka customer pehle se maujood hai.",
            intent=intent_value,
            customer=_customer_payload(matches[0]),
        )

    try:
        created = create_customer(db, CustomerCreate(name=name))
    except HTTPException as exc:
        db.rollback()
        return VoiceCommandResult(
            status=STATUS_ERROR,
            response_text=f"Customer add nahi ho paya: {exc.detail}",
            intent=intent_value,
        )

    return VoiceCommandResult(
        status=STATUS_SUCCESS,
        response_text=f"'{created.name}' ko naye customer ke roop mein add kar diya gaya.",
        intent=intent_value,
        customer={
            "id": created.id,
            "name": created.name,
            "phone": created.phone,
        },
    )


def _resolve_pending_transaction_id(
    pending_transaction_id=None,
    last_transaction_id=None,
    session_transaction_id=None,
    correction_context=None,
) -> Optional[int]:
    for candidate in (
        pending_transaction_id,
        last_transaction_id,
        session_transaction_id,
    ):
        if candidate is not None:
            try:
                return int(candidate)
            except (TypeError, ValueError):
                return None
    if isinstance(correction_context, dict):
        value = correction_context.get("transaction_id")
        if value is None:
            value = correction_context.get("id")
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    if correction_context is not None and not isinstance(correction_context, dict):
        try:
            return int(correction_context)
        except (TypeError, ValueError):
            return None
    return None


def _handle_correction(
    db: Session,
    cmd: ParsedCommand,
    intent_value: str,
    pending_transaction_id: Optional[int],
) -> VoiceCommandResult:
    if pending_transaction_id is None:
        return VoiceCommandResult(
            status=STATUS_NEEDS_REPEAT,
            response_text=(
                "Correction ke liye koi pending transaction nahi hai. "
                "Please poori command dobara boliye, jaise: "
                "'Ramesh ko 500 rupaye udhaar'."
            ),
            intent=intent_value,
        )

    if cmd.amount is None or cmd.amount <= 0:
        return VoiceCommandResult(
            status=STATUS_NEEDS_CLARIFICATION,
            response_text="Correction ke liye sahi amount bataye, jaise: 'Nahi, 430'.",
            intent=intent_value,
        )

    existing = db.query(Transaction).filter(Transaction.id == pending_transaction_id).first()
    if existing is None:
        return VoiceCommandResult(
            status=STATUS_NEEDS_REPEAT,
            response_text=(
                "Correction ke liye koi pending transaction nahi hai. "
                "Please poori command dobara boliye, jaise: "
                "'Ramesh ko 500 rupaye udhaar'."
            ),
            intent=intent_value,
        )

    try:
        updated = update_transaction_amount(db, pending_transaction_id, cmd.amount)
    except HTTPException as exc:
        db.rollback()
        return VoiceCommandResult(
            status=STATUS_ERROR,
            response_text=f"Transaction update nahi ho paya: {exc.detail}",
            intent=intent_value,
        )

    customer = db.query(Customer).filter(Customer.id == updated.customer_id).first()
    return VoiceCommandResult(
        status=STATUS_SUCCESS,
        response_text=f"Theek hai, amount {updated.amount:g} rupaye kar diya.",
        intent=intent_value,
        customer=_customer_payload(customer) if customer is not None else {"id": updated.customer_id},
        transaction=_transaction_payload(updated),
        last_transaction_id=updated.id,
    )


def _dispatch_parsed_command(
    db: Session,
    cmd: ParsedCommand,
    pending_transaction_id: Optional[int] = None,
    *,
    last_transaction_id=None,
    session_transaction_id=None,
    correction_context=None,
) -> VoiceCommandResult:
    """
    Parse one transcript and perform the matching ledger action.

    Deterministic and side-effect-free apart from the intended database writes
    for CREDIT, PAYMENT, and NEW_CUSTOMER commands.
    """
    resolved_pending_transaction_id = _resolve_pending_transaction_id(
        pending_transaction_id,
        last_transaction_id,
        session_transaction_id,
        correction_context,
    )

    intent_value = cmd.intent.value

    if cmd.intent == Intent.CORRECTION:
        return _handle_correction(db, cmd, intent_value, resolved_pending_transaction_id)

    if cmd.intent in (Intent.CREDIT, Intent.PAYMENT):
        return _handle_transaction(db, cmd, intent_value)

    if cmd.intent == Intent.DAILY_CREDIT_TOTAL:
        return _handle_daily_credit_total(db, cmd, intent_value)

    if cmd.intent == Intent.DAILY_PAYMENT_TOTAL:
        return _handle_daily_payment_total(db, cmd, intent_value)

    if cmd.intent == Intent.BALANCE_QUERY:
        return _handle_balance(db, cmd, intent_value)

    if cmd.intent == Intent.HISTORY_QUERY:
        return _handle_history(db, cmd, intent_value)

    if cmd.intent == Intent.NEW_CUSTOMER:
        return _handle_new_customer(db, cmd, intent_value)

    if cmd.intent == Intent.CLARIFICATION_NEEDED:
        return VoiceCommandResult(
            status=STATUS_NEEDS_CLARIFICATION,
            response_text=(
                cmd.clarification_prompt
                or "Command adhoora hai. Customer ka naam aur amount bataye."
            ),
            intent=intent_value,
        )


    return VoiceCommandResult(
        status=STATUS_UNKNOWN,
        response_text=(
            "Samajh nahi aaya. Aap udhaar, jama, balance ya history "
            "ke bare mein boliye."
        ),
        intent=intent_value,
    )


def _resolve_customer_clarification(
    db: Session,
    transcript: str,
    context: Dict[str, Any],
) -> VoiceCommandResult:
    intent = Intent(context.get("intent", ""))
    candidates = context.get("candidate_customers") or []
    pending_transaction_id = context.get("pending_transaction_id")
    normalized = _normalize(transcript)

    if normalized in {"cancel", "cancel karo", "rehne do", "leave it"}:
        return VoiceCommandResult(
            status=STATUS_NEEDS_REPEAT,
            response_text="Theek hai, transaction cancel kar diya.",
            intent=intent.value,
        )

    selected = None
    id_match = re.search(r"\b(\d+)\b", transcript)
    if id_match:
        selected_id = int(id_match.group(1))
        selected = next((c for c in candidates if c["id"] == selected_id), None)

    if selected is None:
        for candidate in candidates:
            if _canonical_name(candidate["name"]) in _canonical_name(transcript):
                selected = candidate
                break

    if selected is None:
        extracted = _extract_name(normalized)
        if extracted:
            for candidate in candidates:
                if _canonical_name(candidate["name"]) == _canonical_name(extracted):
                    selected = candidate
                    break

    if selected is None:
        names = " ya ".join(c["name"] for c in candidates)
        return VoiceCommandResult(
            status=STATUS_NEEDS_CUSTOMER_CLARIFICATION,
            response_text=f"{names}? Kaunsa customer hai? Customer ID bataye.",
            intent=intent.value,
            pending_customer_clarification=context,
        )

    cmd = ParsedCommand(
        intent=intent,
        customer_name=selected["name"],
        amount=context.get("amount"),
        date_offset=context.get("date_offset", 0),
        description=context.get("description"),
    )
    return _dispatch_parsed_command(db, cmd, pending_transaction_id)


def process_voice_command(
    db: Session,
    transcript: str,
    pending_transaction_id=None,
    *,
    last_transaction_id=None,
    session_transaction_id=None,
    correction_context=None,
    pending_customer_clarification=None,
) -> VoiceCommandResult:
    resolved_pending_transaction_id = _resolve_pending_transaction_id(
        pending_transaction_id,
        last_transaction_id,
        session_transaction_id,
        correction_context,
    )

    if pending_customer_clarification:
        return _resolve_customer_clarification(
            db,
            transcript,
            pending_customer_clarification,
        )

    cmd = parse_command(transcript)
    result = _dispatch_parsed_command(db, cmd, resolved_pending_transaction_id)
    if result.last_transaction_id is None and result.transaction and isinstance(result.transaction, dict):
        result.last_transaction_id = result.transaction.get("id")
    return result


def process_voice_commands(
    db: Session,
    transcript: str,
    pending_transaction_id=None,
    *,
    last_transaction_id=None,
    session_transaction_id=None,
    correction_context=None,
    pending_customer_clarification=None,
) -> VoiceCommandResult:
    resolved_pending_transaction_id = _resolve_pending_transaction_id(
        pending_transaction_id,
        last_transaction_id,
        session_transaction_id,
        correction_context,
    )

    if pending_customer_clarification:
        return _resolve_customer_clarification(
            db,
            transcript,
            pending_customer_clarification,
        )

    commands = parse_commands(transcript)
    if len(commands) == 1:
        return process_voice_command(
            db,
            transcript,
            pending_transaction_id=resolved_pending_transaction_id,
        )

    results = []
    current_pending = resolved_pending_transaction_id
    last_success_id = resolved_pending_transaction_id
    for cmd in commands:
        # Corrections in a batch must update the immediately preceding transaction.
        command_pending = current_pending
        result = _dispatch_parsed_command(db, cmd, command_pending)
        if result.last_transaction_id is None and result.transaction and isinstance(result.transaction, dict):
            result.last_transaction_id = result.transaction.get("id")
        if (
            result.status == STATUS_SUCCESS
            and result.intent in {Intent.CREDIT.value, Intent.PAYMENT.value}
            and result.last_transaction_id is not None
        ):
            current_pending = result.last_transaction_id
            last_success_id = result.last_transaction_id
        results.append(result)

    all_success = bool(results) and all(r.status == STATUS_SUCCESS for r in results)
    response_parts = [r.response_text for r in results if r.response_text]
    return VoiceCommandResult(
        status=STATUS_SUCCESS if all_success else "partial_success",
        response_text="; ".join(response_parts),
        intent="BATCH",
        transactions=[r.transaction for r in results if r.transaction],
        last_transaction_id=last_success_id,
    )
