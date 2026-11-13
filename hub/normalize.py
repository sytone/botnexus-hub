"""The only data allowed across the Hub snapshot and command boundary."""

from __future__ import annotations

from datetime import datetime

from .security import InvalidInput, MAX_FRAME_BYTES, canonical_json, routing_id, validate_base_url

MAX_ITEMS = 100
MAX_CHOICES = 50
MAX_MESSAGE = 16_384


def text(value, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return "".join(char for char in value[:limit] if ord(char) >= 32 or char in "\n\t")


def timestamp(value, *, strict=False) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return value
    except ValueError:
        if strict:
            raise InvalidInput("Invalid observed timestamp") from None
        return None


def array(frame, key):
    value = frame.get(key, [])
    if not isinstance(value, list):
        raise InvalidInput("Invalid snapshot array")
    return value[:MAX_ITEMS]


def normalize_snapshot(frame, world: dict) -> dict:
    if not isinstance(frame, dict) or frame.get("type") != "snapshot":
        raise InvalidInput("Invalid snapshot")
    if type(frame.get("protocolVersion")) is not int or frame["protocolVersion"] != 1:
        raise InvalidInput("Unsupported protocol version")
    if frame.get("worldId") != world["id"]:
        raise InvalidInput("World scope mismatch")
    base_url = validate_base_url(frame["baseUrl"]) if "baseUrl" in frame else world["baseUrl"]
    agents, conversations, questions = [], [], []
    agent_ids, conversation_owners, question_ids = set(), {}, set()
    excluded_conversations = set()
    for item in array(frame, "agents"):
        if not isinstance(item, dict) or not routing_id(item.get("id")):
            continue
        if item.get("enabled", True) is not True or item.get("visible", True) is not True or item.get("hidden", False) is not False:
            continue
        if item["id"] in agent_ids:
            continue
        agent_ids.add(item["id"])
        agents.append({"id": item["id"], "name": text(item.get("name"), 256), "description": text(item.get("description"), 512)})
    for item in array(frame, "conversations"):
        if not isinstance(item, dict) or not routing_id(item.get("id")):
            continue
        if item.get("agentId") not in agent_ids or item.get("status") != "active" or item.get("userFacing", True) is not True or item.get("isUserFacing", True) is not True:
            excluded_conversations.add(item["id"])
            continue
        if item.get("parentId") or item.get("parentConversationId") or item.get("isTopLevel", True) is not True or item.get("isSubagent", False) is not False:
            excluded_conversations.add(item["id"])
            continue
        if item["id"] in conversation_owners:
            continue
        conversation_owners[item["id"]] = item["agentId"]
        conversations.append({
            "id": item["id"], "agentId": item["agentId"], "title": text(item.get("title"), 256),
            "updatedAt": timestamp(item.get("updatedAt")), "status": "active", "pendingQuestion": None,
        })
    for item in array(frame, "questions"):
        if not isinstance(item, dict) or not routing_id(item.get("requestId")):
            continue
        conversation_id, agent_id = item.get("conversationId"), item.get("agentId")
        if not routing_id(conversation_id) or agent_id not in agent_ids or conversation_id in excluded_conversations:
            continue
        # All visible pending questions may include older conversations outside
        # the connector's recent-activity window. Keep their routing, not history.
        if conversation_id in conversation_owners and conversation_owners[conversation_id] != agent_id:
            continue
        identity = (conversation_id, item["requestId"])
        if identity in question_ids:
            continue
        question_ids.add(identity)
        choices = []
        if isinstance(item.get("choices", []), list):
            for choice in item.get("choices", [])[:MAX_CHOICES]:
                if isinstance(choice, str):
                    choices.append(text(choice, 256))
                elif isinstance(choice, dict) and isinstance(choice.get("value"), str):
                    choices.append({"value": text(choice["value"], 256), "label": text(choice.get("label", choice["value"]), 256)})
        questions.append({
            "conversationId": conversation_id, "agentId": agent_id, "title": text(item.get("title"), 256),
            "requestId": item["requestId"], "prompt": text(item.get("prompt"), 4000),
            "inputType": text(item.get("inputType"), 64, "free_form"), "choices": choices,
            "allowMultiple": item.get("allowMultiple") is True,
            "allowFreeForm": item.get("allowFreeForm") is True or item.get("inputType") in {"free_form", "choice_or_free_form"},
        })
    result = {
        "type": "snapshot", "protocolVersion": 1, "worldId": world["id"],
        "name": text(frame.get("name"), 256, world["name"]), "baseUrl": base_url,
        "version": text(frame.get("version"), 128), "observedAt": timestamp(frame.get("observedAt"), strict=True),
        "agents": agents, "conversations": conversations, "questions": questions,
    }
    # Input and stored output both have hard byte limits, including Unicode expansion.
    while len(canonical_json(result).encode("utf-8")) > MAX_FRAME_BYTES:
        if questions:
            questions.pop()
        elif conversations:
            conversations.pop()
        elif agents:
            agents.pop()
        else:
            raise InvalidInput("Snapshot exceeds storage limit")
    return result


def require_string(payload, key, limit, *, optional=False, empty=False):
    value = payload.get(key)
    if optional and value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise InvalidInput("Invalid command field")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise InvalidInput("Invalid command field")
    return value


def normalize_command(body) -> dict:
    if not isinstance(body, dict) or set(body) != {"action", "payload"}:
        raise InvalidInput("Invalid command")
    action, payload = body["action"], body["payload"]
    fields = {
        "start_conversation": {"agentId", "title", "message"},
        "send_message": {"agentId", "conversationId", "message"},
        "answer_question": {"agentId", "conversationId", "requestId", "text", "selectedValues", "cancelled"},
    }
    if not isinstance(action, str) or action not in fields or not isinstance(payload, dict) or set(payload) - fields[action]:
        raise InvalidInput("Unsupported command or fields")
    if not routing_id(payload.get("agentId")):
        raise InvalidInput("Invalid agent ID")
    result = {"agentId": payload["agentId"]}
    if action != "start_conversation":
        if not routing_id(payload.get("conversationId")):
            raise InvalidInput("Invalid conversation ID")
        result["conversationId"] = payload["conversationId"]
    if action == "start_conversation":
        title = require_string(payload, "title", 200, optional=True, empty=True)
        if title is not None:
            result["title"] = title
    if action in {"start_conversation", "send_message"}:
        result["message"] = require_string(payload, "message", MAX_MESSAGE)
    else:
        if not routing_id(payload.get("requestId")):
            raise InvalidInput("Invalid question request ID")
        result["requestId"] = payload["requestId"]
        result["text"] = require_string(payload, "text", MAX_MESSAGE, optional=True, empty=True) or ""
        selected = payload.get("selectedValues", [])
        if not isinstance(selected, list) or len(selected) > MAX_CHOICES or any(not isinstance(value, str) or len(value) > 256 for value in selected):
            raise InvalidInput("Invalid selected values")
        result["selectedValues"] = selected
        cancelled = payload.get("cancelled", False)
        if not isinstance(cancelled, bool):
            raise InvalidInput("Invalid cancellation flag")
        result["cancelled"] = cancelled
        if not cancelled and not result["text"].strip() and not selected:
            raise InvalidInput("An answer is required")
    normalized = {"action": action, "payload": result}
    if len(canonical_json(normalized).encode("utf-8")) > 60 * 1024:
        raise InvalidInput("Command payload is too large")
    return normalized


def command_in_scope(command: dict, snapshot: dict | None) -> bool:
    if snapshot is None:
        return False
    payload = command["payload"]
    if payload["agentId"] not in {agent["id"] for agent in snapshot["agents"]}:
        return False
    if command["action"] == "start_conversation":
        return True
    if command["action"] == "answer_question":
        return any(all(question[key] == payload[key] for key in ("agentId", "conversationId", "requestId")) for question in snapshot["questions"])
    return any(conversation["id"] == payload["conversationId"] and conversation["agentId"] == payload["agentId"] and conversation["status"] == "active" for conversation in snapshot["conversations"])


def normalize_result(frame) -> tuple[bool, dict | None, str | None]:
    if type(frame.get("ok")) is not bool:
        raise InvalidInput("Invalid command result")
    if frame["ok"]:
        data = frame.get("data", {})
        if not isinstance(data, dict):
            raise InvalidInput("Invalid result data")
        result = {}
        if "conversationId" in data:
            if not routing_id(data["conversationId"]):
                raise InvalidInput("Invalid result conversation ID")
            result["conversationId"] = data["conversationId"]
        if data.get("status") == "accepted":
            result["status"] = "accepted"
        return True, result, None
    safe_errors = {
        "command_failed", "agent_not_allowed", "conversation_not_found", "conversation_not_active",
        "question_not_found", "stale_request", "invalid_payload", "not_supported", "queue_full", "permission_denied",
        "outcome_unknown", "agent_unavailable", "conversation_not_writable", "busy", "no_route", "rejected",
        "answer_unavailable", "no_pending_question", "request_mismatch", "invalid_answer", "command_capacity", "command_conflict",
    }
    error = frame.get("error")
    return False, None, error if isinstance(error, str) and error in safe_errors else "command_failed"
