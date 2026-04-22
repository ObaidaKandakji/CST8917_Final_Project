import json
import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.servicebus import ServiceBusClient, ServiceBusMessage

MANAGER_DECISIONS_TABLE = os.getenv("MANAGER_DECISIONS_TABLE_NAME", "ManagerDecisions")
SERVICE_BUS_QUEUE_NAME = os.getenv("SERVICE_BUS_QUEUE_NAME", "expense-requests")
VALID_CATEGORIES = {
    "travel",
    "meals",
    "supplies",
    "equipment",
    "software",
    "other",
}

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _json_response(payload: dict, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


def _load_json_body(req: func.HttpRequest) -> dict:
    return req.get_json()


def _get_storage_connection_string() -> str:
    storage_connection_string = os.getenv("AzureWebJobsStorage")
    if not storage_connection_string:
        raise RuntimeError("AzureWebJobsStorage is not configured.")
    return storage_connection_string


def _get_table_client():
    table_service = TableServiceClient.from_connection_string(_get_storage_connection_string())
    table_service.create_table_if_not_exists(table_name=MANAGER_DECISIONS_TABLE)
    return table_service.get_table_client(MANAGER_DECISIONS_TABLE)


def _validate_decision(decision: str | None) -> str | None:
    if not isinstance(decision, str):
        return None
    lowered = decision.lower()
    return lowered if lowered in {"approved", "rejected"} else None


def normalize_expense(raw_payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    required_fields = [
        "employeeName",
        "employeeEmail",
        "amount",
        "category",
        "description",
        "managerEmail",
    ]
    errors: list[str] = []
    normalized: dict[str, Any] = {}

    for field_name in required_fields:
        value = raw_payload.get(field_name)
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            errors.append(f"'{field_name}' is required.")
            continue
        normalized[field_name] = value

    if "amount" in normalized:
        try:
            normalized["amount"] = round(float(normalized["amount"]), 2)
        except (TypeError, ValueError):
            errors.append("'amount' must be a valid number.")

    category = normalized.get("category")
    if isinstance(category, str):
        normalized["category"] = category.lower()
        if normalized["category"] not in VALID_CATEGORIES:
            errors.append(
                "'category' must be one of: travel, meals, supplies, equipment, software, other."
            )

    return normalized, errors


def build_validation_result(raw_payload: dict[str, Any]) -> dict[str, Any]:
    normalized, errors = normalize_expense(raw_payload)
    return {
        "isValid": len(errors) == 0,
        "errors": errors,
        "expense": normalized if len(errors) == 0 else raw_payload,
    }


@app.route(route="validate-expense", methods=["POST"])
def validate_expense(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = _load_json_body(req)
    except ValueError:
        return _json_response({"message": "Request body must be valid JSON."}, 400)

    validation_result = build_validation_result(payload or {})
    if not validation_result["isValid"]:
        return _json_response(validation_result, 400)

    return _json_response(validation_result, 200)


@app.route(route="expense-requests", methods=["POST"])
def enqueue_expense_request(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = _load_json_body(req)
    except ValueError:
        return _json_response({"message": "Request body must be valid JSON."}, 400)

    validation_result = build_validation_result(payload or {})
    if not validation_result["isValid"]:
        return _json_response(validation_result, 400)

    expense_id = str(uuid4())
    expense = {
        **validation_result["expense"],
        "expenseId": expense_id,
        "createdAtUtc": datetime.now(UTC).isoformat(),
    }

    service_bus_connection_string = os.getenv("SERVICE_BUS_CONNECTION_STRING")
    if not service_bus_connection_string:
        raise RuntimeError("SERVICE_BUS_CONNECTION_STRING is not configured.")

    with ServiceBusClient.from_connection_string(service_bus_connection_string) as client:
        with client.get_queue_sender(queue_name=SERVICE_BUS_QUEUE_NAME) as sender:
            sender.send_messages(
                ServiceBusMessage(
                    json.dumps(expense),
                    content_type="application/json",
                    message_id=expense_id,
                )
            )

    logging.info("Queued expense request %s for employee %s", expense_id, expense["employeeEmail"])
    return _json_response(
        {
            "message": "Expense request accepted and queued.",
            "expenseId": expense_id,
            "status": "queued",
            "expense": expense,
        },
        202,
    )


@app.route(route="manager-decisions", methods=["POST"])
def store_manager_decision(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = _load_json_body(req)
    except ValueError:
        return _json_response({"message": "Request body must be valid JSON."}, 400)

    expense_id = payload.get("expenseId")
    decision = _validate_decision(payload.get("decision"))
    if not isinstance(expense_id, str) or not expense_id.strip():
        return _json_response({"message": "'expenseId' is required."}, 400)
    if decision is None:
        return _json_response(
            {"message": "'decision' must be either 'approved' or 'rejected'."},
            400,
        )

    table_client = _get_table_client()
    entity = {
        "PartitionKey": "manager-decisions",
        "RowKey": expense_id.strip(),
        "decision": decision,
        "updatedAtUtc": datetime.now(UTC).isoformat(),
    }
    table_client.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)

    return _json_response(
        {
            "message": "Manager decision stored.",
            "expenseId": expense_id.strip(),
            "decision": decision,
        },
        202,
    )


@app.route(route="manager-decisions/{expenseId}", methods=["GET"])
def get_manager_decision(req: func.HttpRequest) -> func.HttpResponse:
    expense_id = req.route_params.get("expenseId")
    if not expense_id:
        return _json_response({"message": "'expenseId' is required."}, 400)

    table_client = _get_table_client()
    try:
        entity = table_client.get_entity(
            partition_key="manager-decisions",
            row_key=expense_id,
        )
    except ResourceNotFoundError:
        return _json_response({"expenseId": expense_id, "found": False}, 200)

    return _json_response(
        {
            "expenseId": expense_id,
            "found": True,
            "decision": entity["decision"],
            "updatedAtUtc": entity["updatedAtUtc"],
        },
        200,
    )
