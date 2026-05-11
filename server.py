import json
import os
import time
import uuid
import logging
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse
import httpx

logger = logging.getLogger("proxy-to-codex")

# ── Config ──────────────────────────────────────────────────
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
def get_api_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")

MODEL_MAP = {
    "gpt-5.4": "deepseek-v4-pro",
    "gpt-5.5": "deepseek-v4-pro",
    "gpt-4o": "deepseek-v4-flash",
    "gpt-4o-mini": "deepseek-v4-flash",
    "gpt-4.1": "deepseek-v4-pro",
    "gpt-4.1-mini": "deepseek-v4-flash",
}
DEFAULT_MODEL = "deepseek-v4-pro"


# ── SessionStore ────────────────────────────────────────────
class SessionStore:
    def __init__(self, ttl: int = 3600):
        self._store: dict[str, tuple[float, list[dict]]] = {}
        self._ttl = ttl

    def set(self, response_id: str, messages: list[dict]) -> None:
        self._store[response_id] = (time.time() + self._ttl, messages)

    def get(self, response_id: str) -> list[dict] | None:
        entry = self._store.get(response_id)
        if entry is None:
            return None
        expires, messages = entry
        if time.time() > expires:
            del self._store[response_id]
            return None
        return messages

    def cleanup(self) -> None:
        now = time.time()
        expired = [k for k, (exp, _) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]


session_store = SessionStore()


# ── Schema utility ──────────────────────────────────────────
def _clean_schema(obj: dict) -> dict:
    if not isinstance(obj, dict):
        return obj
    cleaned = {}
    for k, v in obj.items():
        if k in ("additionalProperties", "strict"):
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif isinstance(v, list):
            cleaned[k] = [_clean_schema(item) if isinstance(item, dict) else item for item in v]
        else:
            cleaned[k] = v
    return cleaned


# ── Request translation ─────────────────────────────────────
def translate_response_create_to_chat(body: dict) -> tuple[dict, str, dict]:
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    original_model = body.get("model", "gpt-5.4")

    prev_id = body.get("previous_response_id")
    if prev_id:
        stored_messages = session_store.get(prev_id)
    else:
        stored_messages = None

    messages: list[dict] = []
    if stored_messages:
        messages = list(stored_messages)

    instructions = body.get("instructions", "")
    if instructions and not stored_messages:
        messages.insert(0, {"role": "system", "content": instructions})

    pending_tool_calls: list[dict] = []
    has_new_input = False

    for item in body.get("input", []):
        item_type = item.get("type", "")

        if item_type == "message":
            role = item.get("role", "user")
            if role == "developer":
                role = "system"

            content_parts = item.get("content", [])
            if isinstance(content_parts, str):
                text = content_parts
            elif isinstance(content_parts, list):
                texts = []
                for part in content_parts:
                    if part.get("type") in ("input_text", "output_text"):
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "input_image":
                        texts.append("[image]")
                text = "\n".join(texts)
            else:
                text = ""

            if pending_tool_calls and role in ("user", "system"):
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(pending_tool_calls),
                })
                pending_tool_calls = []

            if role in ("user", "system"):
                messages.append({"role": role, "content": text})
            elif role == "assistant":
                messages.append({"role": "assistant", "content": text})
            has_new_input = True

        elif item_type == "function_call":
            call_id = item.get("call_id", f"call_{uuid.uuid4().hex[:8]}")
            pending_tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                },
            })

        elif item_type == "function_call_output":
            if pending_tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(pending_tool_calls),
                })
                pending_tool_calls = []
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            if isinstance(output, dict):
                output = json.dumps(output)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": output,
            })
            has_new_input = True

    if pending_tool_calls:
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": list(pending_tool_calls),
        })
        pending_tool_calls = []

    messages = _reorder_messages(messages)

    tools = []
    for tool in body.get("tools", []):
        if tool.get("type") != "function":
            continue
        cleaned_params = _clean_schema(tool.get("parameters", {}))
        tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": cleaned_params,
            },
        })

    ds_model = MODEL_MAP.get(original_model, DEFAULT_MODEL)

    chat_body: dict = {
        "model": ds_model,
        "messages": messages,
        "stream": True,
    }

    if tools:
        chat_body["tools"] = tools

    if body.get("temperature") is not None:
        chat_body["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        chat_body["top_p"] = body["top_p"]
    max_tokens = body.get("max_output_tokens")
    if max_tokens:
        chat_body["max_tokens"] = max_tokens

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort", "")
        if effort in ("low", "medium", "high"):
            chat_body["thinking"] = {"type": "enabled"}
            chat_body["reasoning_effort"] = "high"
        elif effort == "max":
            chat_body["thinking"] = {"type": "enabled"}
            chat_body["reasoning_effort"] = "max"

    tc = body.get("tool_choice")
    if tc:
        if tc == "auto" or tc == "none" or tc == "required":
            chat_body["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("type") == "function":
            chat_body["tool_choice"] = {
                "type": "function",
                "function": {"name": tc.get("name", "")},
            }

    meta = {
        "response_id": response_id,
        "original_model": original_model,
        "created_at": int(time.time()),
        "input_items": body.get("input", []),
        "has_input": has_new_input,
    }

    return chat_body, response_id, meta


def _reorder_messages(messages: list[dict]) -> list[dict]:
    if not messages:
        return messages

    result: list[dict] = []
    buffer: list[dict] = []
    pending_system: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            pending_system.append(msg)
        elif role == "assistant" and msg.get("tool_calls"):
            result.extend(pending_system)
            pending_system = []
            buffer.append(msg)
        elif role == "tool" and buffer:
            buffer.append(msg)
        else:
            if buffer:
                result.extend(buffer)
                buffer = []
            result.extend(pending_system)
            pending_system = []
            result.append(msg)

    result.extend(buffer)
    result.extend(pending_system)
    return result


# ── SSE Parser ──────────────────────────────────────────────
async def parse_sse_stream(response: httpx.Response):
    buffer = ""
    async for chunk in response.aiter_bytes():
        buffer += chunk.decode("utf-8", errors="replace")
        lines = buffer.split("\n")
        buffer = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data: "):
                payload = line[6:]
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue


# ── Response translation (SSE chunk → WS events) ───────────
def translate_sse_chunk_to_ws_events(sse_data: dict, state: dict, meta: dict) -> list[dict]:
    events: list[dict] = []
    choices = sse_data.get("choices", [])
    choice = choices[0] if choices else {}
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")

    if "error" in sse_data:
        return [{
            "type": "response.failed",
            "response": {"id": meta["response_id"], "object": "response", "status": "failed"},
            "error": {"code": "upstream_error", "message": str(sse_data["error"])},
        }]

    has_usage = "usage" in sse_data
    if has_usage:
        state["usage"] = sse_data["usage"]

    if delta.get("role") == "assistant" and not state.get("has_lifecycle_emitted"):
        state["has_lifecycle_emitted"] = True
        state["sequence_number"] = 0
        state["accumulated_text"] = ""
        state["accumulated_reasoning"] = ""
        state["has_content_part"] = False
        state["tool_call_states"] = {}
        state["output_count"] = 0
        state["output_indices"] = {}

        events.append({
            "type": "response.created",
            "response": {
                "id": meta["response_id"],
                "object": "response",
                "created_at": meta["created_at"],
                "model": meta["original_model"],
                "status": "in_progress",
                "output": [],
            },
        })
        events.append({
            "type": "response.in_progress",
            "response": {"id": meta["response_id"], "object": "response", "status": "in_progress"},
        })
        return events

    if not state.get("has_lifecycle_emitted"):
        return events

    reasoning = delta.get("reasoning_content", "")
    if reasoning:
        state["accumulated_reasoning"] += reasoning
        events.append({
            "type": "response.reasoning_summary_text.delta",
            "delta": reasoning,
            "output_index": 0,
            "content_index": 0,
        })

    tool_calls = delta.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            idx = tc.get("index", 0)
            tc_states = state.setdefault("tool_call_states", {})

            if idx not in tc_states:
                tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
                tc_name = tc.get("function", {}).get("name", "")
                output_idx = state.setdefault("output_count", 0)
                state["output_count"] += 1
                item_id = f"item_{uuid.uuid4().hex[:12]}"

                tc_states[idx] = {
                    "id": tc_id,
                    "name": tc_name,
                    "arguments": "",
                    "item_id": item_id,
                    "output_index": output_idx,
                    "name_emitted": False,
                }

                events.append({
                    "type": "response.output_item.added",
                    "output_index": output_idx,
                    "item": {
                        "id": item_id,
                        "type": "function_call",
                        "name": tc_name,
                        "call_id": tc_id,
                        "status": "in_progress",
                    },
                })

            tc_state = tc_states[idx]
            tc_name = tc.get("function", {}).get("name", "")
            if tc_name and not tc_state.get("name_emitted"):
                tc_state["name"] = tc_name
                tc_state["name_emitted"] = True

            args_delta = tc.get("function", {}).get("arguments", "")
            if args_delta:
                tc_state["arguments"] += args_delta
                events.append({
                    "type": "response.function_call_arguments.delta",
                    "output_index": tc_state["output_index"],
                    "delta": args_delta,
                })

    content = delta.get("content", "")
    if content:
        if not state.get("has_content_part"):
            state["has_content_part"] = True
            text_item_id = f"item_{uuid.uuid4().hex[:12]}"
            state["text_item_id"] = text_item_id
            text_output_idx = state.setdefault("output_count", 0)
            state["output_count"] += 1
            state["text_output_index"] = text_output_idx

            events.append({
                "type": "response.output_item.added",
                "output_index": text_output_idx,
                "item": {
                    "id": text_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            })
            events.append({
                "type": "response.content_part.added",
                "output_index": text_output_idx,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })

        state["accumulated_text"] += content
        state["sequence_number"] = state.get("sequence_number", 0) + 1

        events.append({
            "type": "response.output_text.delta",
            "output_index": state.get("text_output_index", 0),
            "content_index": 0,
            "delta": content,
            "sequence_number": state["sequence_number"],
        })

    if finish_reason:
        if state.get("has_content_part"):
            text_idx = state.get("text_output_index", 0)
            events.append({
                "type": "response.output_text.done",
                "output_index": text_idx,
                "content_index": 0,
                "text": state.get("accumulated_text", ""),
            })
            events.append({
                "type": "response.content_part.done",
                "output_index": text_idx,
                "content_index": 0,
                "part": {
                    "type": "output_text",
                    "text": state.get("accumulated_text", ""),
                    "annotations": [],
                },
            })
            events.append({
                "type": "response.output_item.done",
                "output_index": text_idx,
                "item": {
                    "id": state.get("text_item_id", ""),
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text",
                        "text": state.get("accumulated_text", ""),
                        "annotations": [],
                    }],
                },
            })

        for idx, tc_state in sorted(state.get("tool_call_states", {}).items()):
            events.append({
                "type": "response.function_call_arguments.done",
                "output_index": tc_state["output_index"],
                "arguments": tc_state["arguments"],
                "name": tc_state["name"],
                "call_id": tc_state["id"],
            })
            events.append({
                "type": "response.output_item.done",
                "output_index": tc_state["output_index"],
                "item": {
                    "id": tc_state["item_id"],
                    "type": "function_call",
                    "name": tc_state["name"],
                    "call_id": tc_state["id"],
                    "arguments": tc_state["arguments"],
                    "status": "completed",
                },
            })

        usage = state.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)
        reasoning_tokens = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)

        output_items = []
        if state.get("has_content_part"):
            output_items.append({
                "id": state.get("text_item_id", ""),
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": state.get("accumulated_text", ""),
                    "annotations": [],
                }],
            })
        for idx in sorted(state.get("tool_call_states", {}).keys()):
            tc_state = state["tool_call_states"][idx]
            output_items.append({
                "id": tc_state["item_id"],
                "type": "function_call",
                "name": tc_state["name"],
                "call_id": tc_state["id"],
                "arguments": tc_state["arguments"],
                "status": "completed",
            })

        status = "completed"
        if finish_reason == "length":
            status = "incomplete"

        events.append({
            "type": "response.completed",
            "response": {
                "id": meta["response_id"],
                "object": "response",
                "created_at": meta["created_at"],
                "model": meta["original_model"],
                "status": status,
                "output": output_items,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                },
            },
        })

    return events


def build_assistant_from_state(state: dict) -> dict:
    accumulated_text = state.get("accumulated_text", "")
    accumulated_reasoning = state.get("accumulated_reasoning", "")
    tool_call_states = state.get("tool_call_states", {})

    msg: dict = {"role": "assistant", "content": accumulated_text or None}
    if accumulated_reasoning:
        msg["reasoning_content"] = accumulated_reasoning

    if tool_call_states:
        tool_calls = []
        for idx in sorted(tool_call_states.keys()):
            tc = tool_call_states[idx]
            tool_calls.append({
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                },
            })
        msg["tool_calls"] = tool_calls

    return msg


# ── Chat response → Responses API translation ───────────────
def _translate_chat_response_to_responses(
    chat_response: dict, response_id: str, meta: dict
) -> dict:
    choice = chat_response["choices"][0]
    message = choice.get("message", {})
    usage = chat_response.get("usage", {})

    output_items = []
    content = message.get("content", "")
    if content:
        output_items.append({
            "id": f"item_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": content,
                "annotations": [],
            }],
        })

    for tc in message.get("tool_calls", []):
        output_items.append({
            "id": f"item_{uuid.uuid4().hex[:12]}",
            "type": "function_call",
            "name": tc["function"]["name"],
            "call_id": tc["id"],
            "arguments": tc["function"]["arguments"],
            "status": "completed",
        })

    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    reasoning_tokens = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)

    status = "completed"
    if choice.get("finish_reason") == "length":
        status = "incomplete"

    return {
        "id": response_id,
        "object": "response",
        "created_at": meta["created_at"],
        "model": meta["original_model"],
        "status": status,
        "output": output_items,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


# ── Create App ──────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/v1/responses")
    async def responses_websocket(ws: WebSocket):
        await ws.accept()
        turn_state: dict = {}

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "code": "invalid_request",
                        "message": "Invalid JSON",
                    }))
                    continue

                event_type = body.get("type", "")
                if event_type != "response.create":
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "code": "invalid_event_type",
                        "message": f"Unsupported event type: {event_type}",
                    }))
                    continue

                turn_state.clear()

                try:
                    chat_body, response_id, meta = translate_response_create_to_chat(body)
                except Exception as e:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "code": "internal_error",
                        "message": f"Request translation failed: {str(e)}",
                    }))
                    continue

                turn_state["meta"] = meta
                turn_state["response_id"] = response_id

                if not chat_body.get("messages") or all(
                    m.get("role") in ("system",) for m in chat_body["messages"]
                ):
                    rid = meta["response_id"]
                    await ws.send_text(json.dumps({
                        "type": "response.created",
                        "response": {
                            "id": rid, "object": "response",
                            "created_at": meta["created_at"],
                            "model": meta["original_model"],
                            "status": "in_progress", "output": [],
                        },
                    }))
                    await ws.send_text(json.dumps({
                        "type": "response.completed",
                        "response": {
                            "id": rid, "object": "response",
                            "created_at": meta["created_at"],
                            "model": meta["original_model"],
                            "status": "completed", "output": [],
                            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                                      "output_tokens_details": {"reasoning_tokens": 0}},
                        },
                    }))
                    session_store.set(rid, list(chat_body.get("messages", [])))
                    continue

                headers = {
                    "Authorization": f"Bearer {get_api_key()}",
                    "Content-Type": "application/json",
                }

                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                        async with client.stream(
                            "POST",
                            f"{DEEPSEEK_BASE}/chat/completions",
                            headers=headers,
                            json=chat_body,
                        ) as resp:
                            if resp.status_code != 200:
                                error_text = await resp.aread()
                                error_body = error_text.decode(errors="replace")[:1000]
                                logger.error(
                                    f"DeepSeek {resp.status_code}: {error_body}\n"
                                    f"  → model={chat_body.get('model')}, "
                                    f"msgs={len(chat_body.get('messages', []))}, "
                                    f"stream={chat_body.get('stream')}, "
                                    f"keys={list(chat_body.keys())}"
                                )
                                await ws.send_text(json.dumps({
                                    "type": "response.failed",
                                    "response": {
                                        "id": response_id,
                                        "object": "response",
                                        "status": "failed",
                                    },
                                    "error": {
                                        "code": "upstream_error",
                                        "message": f"DeepSeek returned {resp.status_code}: {error_body}",
                                    },
                                }))
                                continue

                            async for sse_data in parse_sse_stream(resp):
                                ws_events = translate_sse_chunk_to_ws_events(
                                    sse_data, turn_state, meta)
                                for event in ws_events:
                                    await ws.send_text(json.dumps(event, ensure_ascii=False))

                except httpx.TimeoutException:
                    await ws.send_text(json.dumps({
                        "type": "response.failed",
                        "response": {"id": response_id, "object": "response", "status": "failed"},
                        "error": {"code": "timeout", "message": "DeepSeek API timed out"},
                    }))
                except httpx.RequestError as e:
                    await ws.send_text(json.dumps({
                        "type": "response.failed",
                        "response": {"id": response_id, "object": "response", "status": "failed"},
                        "error": {"code": "upstream_error", "message": f"DeepSeek connection failed: {str(e)}"},
                    }))

                assistant_msg = build_assistant_from_state(turn_state)
                stored = list(chat_body.get("messages", [])) + [assistant_msg]
                session_store.set(response_id, stored)
                session_store.cleanup()

        except WebSocketDisconnect:
            pass
        except Exception as e:
            try:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "code": "internal_error",
                    "message": str(e),
                }))
            except Exception:
                pass

    @app.post("/v1/responses")
    async def responses_http(request: Request):
        """HTTP POST handler for when WebSocket is unavailable."""
        raw_body = await request.body()

        if not raw_body:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_request", "message": "Empty body"}},
            )

        # Decode body: try UTF-8 first, then common fallbacks
        body_text = None
        content_type = request.headers.get("content-type", "")
        for encoding in ("utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030", "latin-1"):
            try:
                body_text = raw_body.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if body_text is None:
            logger.error(
                f"Cannot decode request body (first 32 bytes): {raw_body[:32].hex()}"
            )
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_request", "message": "Cannot decode request body"}},
            )

        try:
            body = json.loads(body_text)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON body (first 200 chars): {body_text[:200]}")
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_request", "message": f"Invalid JSON: {str(e)}"}},
            )

        # Accept both wrapped (response.create) and unwrapped requests
        if isinstance(body, dict) and body.get("type") == "response.create":
            inner = dict(body)
        else:
            inner = body

        try:
            chat_body, response_id, meta = translate_response_create_to_chat(inner)
        except Exception as e:
            logger.error(f"Request translation failed: {e}")
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_request", "message": str(e)}},
            )

        # Empty input: return empty response immediately
        if not chat_body.get("messages") or all(
            m.get("role") in ("system",) for m in chat_body["messages"]
        ):
            resp_data = {
                "id": response_id,
                "object": "response",
                "created_at": meta["created_at"],
                "model": meta["original_model"],
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            }
            session_store.set(response_id, list(chat_body.get("messages", [])))
            return JSONResponse(content=resp_data)

        # Non-streaming call to DeepSeek
        chat_body["stream"] = False
        chat_body.pop("stream_options", None)

        headers = {
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                resp = await client.post(
                    f"{DEEPSEEK_BASE}/chat/completions",
                    headers=headers,
                    json=chat_body,
                )
                if resp.status_code != 200:
                    error_text = resp.text[:1000]
                    logger.error(
                        f"DeepSeek HTTP {resp.status_code}: {error_text}\n"
                        f"  → model={chat_body.get('model')}, "
                        f"msgs={len(chat_body.get('messages', []))}, "
                        f"keys={list(chat_body.keys())}"
                    )
                    return JSONResponse(
                        status_code=502,
                        content={
                            "error": {
                                "code": "upstream_error",
                                "message": f"DeepSeek returned {resp.status_code}: {error_text}",
                            }
                        },
                    )

                chat_response = resp.json()
        except httpx.TimeoutException:
            return JSONResponse(
                status_code=504,
                content={"error": {"code": "timeout", "message": "DeepSeek API timed out"}},
            )
        except httpx.RequestError as e:
            return JSONResponse(
                status_code=502,
                content={"error": {"code": "upstream_error", "message": str(e)}},
            )

        responses_data = _translate_chat_response_to_responses(chat_response, response_id, meta)

        # Store for session chaining
        choice = chat_response["choices"][0]
        msg = choice.get("message", {})
        stored = list(chat_body.get("messages", [])) + [msg]
        session_store.set(response_id, stored)
        session_store.cleanup()

        logger.info(f"POST /v1/responses → {meta['original_model']} → 200 (HTTP)")
        return JSONResponse(content=responses_data)

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model"}
                for m in ["gpt-5.4", "gpt-5.5", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"]
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body_j = await request.json()
        model = body_j.get("model", "gpt-5.4")
        body_j["model"] = MODEL_MAP.get(model, DEFAULT_MODEL)

        headers = {
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
        }

        if body_j.get("stream"):
            client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
            req = client.build_request(
                "POST", f"{DEEPSEEK_BASE}/chat/completions",
                headers=headers, json=body_j,
            )
            resp = await client.send(req, stream=True)
            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=resp.status_code,
                headers={"Content-Type": "text/event-stream"},
            )

        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE}/chat/completions",
                headers=headers, json=body_j,
            )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
