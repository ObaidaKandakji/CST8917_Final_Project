import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import azure.durable_functions as df
import azure.functions as func

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

VALID_CATEGORIES = {"travel", "meals", "supplies", "equipment", "software", "other"}
MANAGER_DECISION_EVENT = "ManagerDecision"
DEFAULT_TIMEOUT_SECONDS = 60
logger = logging.getLogger("expense_approval")


def _json_response(payload: Any, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, indent=2, default=str),
        status_code=status_code,
        mimetype="application/json",
    )


def _read_json(req: func.HttpRequest) -> Optional[Dict[str, Any]]:
    try:
        body = req.get_json()
        return body if isinstance(body, dict) else None
    except ValueError:
        return None


def _base_url(req: func.HttpRequest) -> str:
    if "/api/" in req.url:
        return req.url.split("/api/")[0]
    return req.url.rstrip("/")


def _runtime_status_name(status: Any) -> str:
    if status is None:
        return "Unknown"
    return getattr(status, "name", str(status))


def _safe_datetime_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            return str(value)
    return str(value)


def _maybe_parse_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return value
        try:
            return json.loads(stripped)
        except ValueError:
            return value
    return value


def _serialize_status(status: Any, fallback_instance_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "name": getattr(status, "name", None),
        "instanceId": getattr(status, "instance_id", None) or fallback_instance_id,
        "runtimeStatus": _runtime_status_name(getattr(status, "runtime_status", None)),
        "createdTime": _safe_datetime_iso(getattr(status, "created_time", None)),
        "lastUpdatedTime": _safe_datetime_iso(getattr(status, "last_updated_time", None)),
        "input": _maybe_parse_json(getattr(status, "input", None)),
        "customStatus": _maybe_parse_json(getattr(status, "custom_status", None)),
        "output": _maybe_parse_json(getattr(status, "output", None)),
    }


def _coerce_event_payload(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, str):
                return {"decision": parsed}
        except ValueError:
            return {"decision": stripped}
    return {"decision": str(payload)}


@app.route(route="expenses/start", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_expense(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    body = _read_json(req)
    if body is None:
        return _json_response({"error": "Request body must be valid JSON."}, 400)

    timeout_seconds = int(os.getenv("EXPENSE_APPROVAL_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))

    orchestration_input = {
        "employee_name": body.get("employee_name"),
        "employee_email": body.get("employee_email"),
        "amount": body.get("amount"),
        "category": body.get("category"),
        "description": body.get("description"),
        "manager_email": body.get("manager_email"),
        "timeout_seconds": timeout_seconds,
    }

    instance_id = await client.start_new("expense_approval_orchestrator", None, orchestration_input)
    base = _base_url(req)

    return _json_response(
        {
            "message": "Expense approval orchestration started.",
            "instanceId": instance_id,
            "statusUrl": f"{base}/api/expenses/status/{instance_id}",
            "decisionUrl": f"{base}/api/expenses/decision/{instance_id}",
            "decisionExamples": {
                "approve": {"decision": "approved", "manager_email": orchestration_input.get("manager_email")},
                "reject": {"decision": "rejected", "manager_email": orchestration_input.get("manager_email")},
            },
            "timeoutSeconds": timeout_seconds,
        },
        202,
    )


@app.route(route="expenses/status/{instanceId}", methods=["GET"])
@app.durable_client_input(client_name="client")
async def get_expense_status(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    instance_id = req.route_params.get("instanceId")
    if not instance_id:
        return _json_response({"error": "instanceId is required."}, 400)

    status = await client.get_status(instance_id, show_input=True)
    if status is None:
        return _json_response({"error": f"No orchestration found for instanceId '{instance_id}'."}, 404)

    return _json_response(_serialize_status(status, instance_id), 200)


@app.route(route="expenses/decision/{instanceId}", methods=["POST"])
@app.durable_client_input(client_name="client")
async def submit_manager_decision(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    instance_id = req.route_params.get("instanceId")
    if not instance_id:
        return _json_response({"error": "instanceId is required."}, 400)

    body = _read_json(req)
    if body is None:
        return _json_response({"error": "Request body must be valid JSON."}, 400)

    raw_decision = str(body.get("decision", "")).strip().lower()
    decision_map = {
        "approved": "approved",
        "approve": "approved",
        "rejected": "rejected",
        "reject": "rejected",
    }
    decision = decision_map.get(raw_decision)
    if decision is None:
        return _json_response(
            {"error": "decision must be one of: approved, approve, rejected, reject."},
            400,
        )

    status = await client.get_status(instance_id)
    if status is None:
        return _json_response({"error": f"No orchestration found for instanceId '{instance_id}'."}, 404)

    runtime_status = _runtime_status_name(status.runtime_status)
    if runtime_status not in {"Pending", "Running"}:
        return _json_response(
            {
                "error": "This orchestration is no longer accepting manager decisions.",
                "runtimeStatus": runtime_status,
            },
            409,
        )

    event_payload = {
        "decision": decision,
        "manager_email": body.get("manager_email"),
        "comment": body.get("comment"),
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    await client.raise_event(instance_id, MANAGER_DECISION_EVENT, event_payload)

    return _json_response(
        {
            "message": f"Manager decision '{decision}' sent.",
            "instanceId": instance_id,
            "statusUrl": f"{_base_url(req)}/api/expenses/status/{instance_id}",
        },
        202,
    )


@app.orchestration_trigger(context_name="context")
def expense_approval_orchestrator(context: df.DurableOrchestrationContext):
    input_data = context.get_input() or {}

    context.set_custom_status(
        {
            "stage": "validating",
            "message": "Validating expense request.",
            "instanceId": context.instance_id,
        }
    )
    validation = yield context.call_activity("validate_expense", input_data)

    if not validation["is_valid"]:
        result = yield context.call_activity(
            "finalize_expense",
            {
                "instance_id": context.instance_id,
                "expense": input_data,
                "decision": "validation_error",
                "reason": "Validation failed.",
                "validation_errors": validation["errors"],
            },
        )
        yield context.call_activity("notify_employee", result)
        return result

    expense = validation["expense"]

    if expense["amount"] < 100:
        context.set_custom_status(
            {
                "stage": "auto_approving",
                "message": "Expense is under $100 and will be auto-approved.",
                "instanceId": context.instance_id,
            }
        )
        result = yield context.call_activity(
            "finalize_expense",
            {
                "instance_id": context.instance_id,
                "expense": expense,
                "decision": "auto_approved",
                "reason": "Amount is under $100.",
            },
        )
        yield context.call_activity("notify_employee", result)
        return result

    timeout_seconds = expense["timeout_seconds"]
    timeout_at = context.current_utc_datetime + timedelta(seconds=timeout_seconds)

    context.set_custom_status(
        {
            "stage": "waiting_for_manager",
            "message": "Waiting for manager decision.",
            "instanceId": context.instance_id,
            "managerEmail": expense["manager_email"],
            "timeoutSeconds": timeout_seconds,
            "timeoutAtUtc": timeout_at.isoformat(),
        }
    )

    yield context.call_activity(
        "request_manager_review",
        {
            "instance_id": context.instance_id,
            "expense": expense,
            "timeout_at_utc": timeout_at.isoformat(),
        },
    )

    approval_event = context.wait_for_external_event(MANAGER_DECISION_EVENT)
    timeout_task = context.create_timer(timeout_at)
    winner = yield context.task_any([approval_event, timeout_task])

    if winner == approval_event:
        try:
            timeout_task.cancel()
        except ValueError:
            pass

        decision_payload = _coerce_event_payload(approval_event.result)
        decision = str(decision_payload.get("decision", "")).strip().lower()
        result = yield context.call_activity(
            "finalize_expense",
            {
                "instance_id": context.instance_id,
                "expense": expense,
                "decision": decision,
                "reason": f"Manager {decision} the expense.",
                "manager_email": decision_payload.get("manager_email") or expense["manager_email"],
                "manager_comment": decision_payload.get("comment"),
                "received_at_utc": decision_payload.get("received_at_utc"),
            },
        )
    else:
        result = yield context.call_activity(
            "finalize_expense",
            {
                "instance_id": context.instance_id,
                "expense": expense,
                "decision": "timeout",
                "reason": "No manager response received before timeout.",
            },
        )

    yield context.call_activity("notify_employee", result)
    return result


@app.activity_trigger(input_name="expense")
def validate_expense(expense) -> Dict[str, Any]:
    expense = expense or {}
    required_fields = [
        "employee_name",
        "employee_email",
        "amount",
        "category",
        "description",
        "manager_email",
    ]

    errors: List[str] = []

    for field in required_fields:
        value = expense.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"{field} is required.")

    category = str(expense.get("category", "")).strip().lower()
    if category and category not in VALID_CATEGORIES:
        errors.append(
            "category must be one of: travel, meals, supplies, equipment, software, other."
        )

    amount_value = None
    try:
        amount_value = float(expense.get("amount"))
        if amount_value < 0:
            errors.append("amount must be greater than or equal to 0.")
    except (TypeError, ValueError):
        errors.append("amount must be a valid number.")

    timeout_seconds = expense.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    normalized_expense = {
        "employee_name": str(expense.get("employee_name", "")).strip(),
        "employee_email": str(expense.get("employee_email", "")).strip(),
        "amount": amount_value,
        "category": category,
        "description": str(expense.get("description", "")).strip(),
        "manager_email": str(expense.get("manager_email", "")).strip(),
        "timeout_seconds": timeout_seconds,
    }

    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "expense": normalized_expense,
    }


@app.activity_trigger(input_name="review_request")
def request_manager_review(review_request) -> Dict[str, Any]:
    review_request = review_request or {}
    instance_id = review_request.get("instance_id")
    expense = review_request.get("expense", {})
    timeout_at_utc = review_request.get("timeout_at_utc")

    logger.info(
        "Manager review requested | instanceId=%s | employee=%s | amount=%.2f | category=%s | manager=%s | timeoutAtUtc=%s",
        instance_id,
        expense.get("employee_name"),
        expense.get("amount", 0.0),
        expense.get("category"),
        expense.get("manager_email"),
        timeout_at_utc,
    )

    return {
        "logged": True,
        "instanceId": instance_id,
    }


@app.activity_trigger(input_name="finalize_request")
def finalize_expense(finalize_request) -> Dict[str, Any]:
    finalize_request = finalize_request or {}
    expense = finalize_request.get("expense", {})
    decision = str(finalize_request.get("decision", "")).strip().lower()

    result = {
        "instanceId": finalize_request.get("instance_id"),
        "employee_name": expense.get("employee_name"),
        "employee_email": expense.get("employee_email"),
        "amount": expense.get("amount"),
        "category": expense.get("category"),
        "description": expense.get("description"),
        "manager_email": finalize_request.get("manager_email") or expense.get("manager_email"),
        "reason": finalize_request.get("reason"),
        "validation_errors": finalize_request.get("validation_errors", []),
        "manager_comment": finalize_request.get("manager_comment"),
        "manager_response_at_utc": finalize_request.get("received_at_utc"),
        "approved": False,
        "escalated": False,
        "status": "validation_error",
        "approval_source": None,
    }

    if decision == "validation_error":
        result["status"] = "validation_error"
    elif decision == "auto_approved":
        result["status"] = "approved"
        result["approved"] = True
        result["approval_source"] = "auto"
    elif decision == "approved":
        result["status"] = "approved"
        result["approved"] = True
        result["approval_source"] = "manager"
    elif decision == "rejected":
        result["status"] = "rejected"
        result["approved"] = False
        result["approval_source"] = "manager"
    elif decision == "timeout":
        result["status"] = "escalated"
        result["approved"] = True
        result["escalated"] = True
        result["approval_source"] = "timeout"
    else:
        result["status"] = "rejected"
        result["approved"] = False
        result["approval_source"] = "manager"
        result["reason"] = result["reason"] or "Unsupported decision value received."

    return result


@app.activity_trigger(input_name="notification_result")
def notify_employee(notification_result) -> Dict[str, Any]:
    notification_result = notification_result or {}

    logger.info(
        "Employee notification (logged locally) | instanceId=%s | employee=%s | email=%s | status=%s | approved=%s | escalated=%s | reason=%s | validationErrors=%s",
        notification_result.get("instanceId"),
        notification_result.get("employee_name"),
        notification_result.get("employee_email"),
        notification_result.get("status"),
        notification_result.get("approved"),
        notification_result.get("escalated"),
        notification_result.get("reason"),
        notification_result.get("validation_errors"),
    )

    return {
        "logged": True,
        "instanceId": notification_result.get("instanceId"),
        "status": notification_result.get("status"),
    }
