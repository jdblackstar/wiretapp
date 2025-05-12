import hashlib
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from functools import wraps
from http import HTTPStatus
from typing import (
    Any,
)

import openai

# Module-level configuration and state
_original_methods: dict[str, Callable[..., Any]] = {}
_app_name: str = "default_app"
_wiretapp_endpoint: str = os.getenv("WIRETAPP_ENDPOINT", "http://localhost:8000/events")
_include_content: bool = os.getenv("WIRETAPP_INCLUDE_CONTENT", "0") == "1"

# Initialize logger
logger = logging.getLogger(__name__)  # Added logger

@dataclass
class Config:
    """Global configuration for the SDK."""
    app_name: str = "default_app"
    wiretapp_endpoint: str = os.getenv("WIRETAPP_ENDPOINT", "http://localhost:8000/events")
    include_content: bool = os.getenv("WIRETAPP_INCLUDE_CONTENT", "0") == "1"
    original_methods: dict[str, Callable[..., Any]] = field(default_factory=dict)

_config = Config()

# Helper to access nested attributes safely
def _safe_getattr(obj: Any, attrs: str, default: Any = None) -> Any:
    for attr in attrs.split("."):
        if hasattr(obj, attr):
            obj = getattr(obj, attr)
        else:
            return default
    return obj


def _hash_identifier(identifier: str | None) -> str | None:
    """
    Hashes an identifier using SHA-256 if it's provided.

    Args:
        identifier: The string to hash.

    Returns:
        The hex digest of the hash, or None if the input is None.
    """
    if identifier is None:
        return None
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def _send_telemetry(data: dict[str, Any]) -> None:
    """
    Sends telemetry data to the configured endpoint.
    For now, it prints the data. In the future, it will make an HTTP POST request.

    Args:
        data: The dictionary of telemetry data to send.
    """
    try:
        import requests

        response = requests.post(_config.wiretapp_endpoint, json=data, timeout=5)
        response.raise_for_status()
        logger.debug(
            f"Telemetry sent successfully to {_config.wiretapp_endpoint}"
        )  # Changed from optional print to logger.debug
    except requests.RequestException as e:
        logger.error(
            f"Failed to send telemetry to {_config.wiretapp_endpoint} - {e}"
        )  # Changed from print to logger.error
    except ImportError:
        logger.error(
            "'requests' library is not installed. Cannot send telemetry. Please add 'requests' to your project dependencies."
        )  # Changed from print to logger.error


def _extract_call_details(method_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    Extracts common details from the OpenAI call arguments for SDK v1.x.
    """
    details: dict[str, Any] = {
        "model_name": kwargs.get("model"),
        # For OpenAI SDK v1.x, 'user' is a valid parameter in chat/completions.create
        "user_id_hashed": _hash_identifier(kwargs.get("user")),
        # 'session_id' is not standard; include if users pass it via extra_body or similar
        "session_id_hashed": _hash_identifier(kwargs.get("session_id")),
    }

    if _config.include_content:
        # Method names like "ChatCompletions.create", "Embeddings.create"
        if "ChatCompletions" in method_name:  # Covers sync and async
            details["prompt_messages"] = kwargs.get("messages")
        elif (
            "Completions" in method_name and "Chat" not in method_name
        ):  # Legacy completions
            details["prompt_text"] = kwargs.get("prompt")
        elif "Embeddings" in method_name:
            details["input_text"] = kwargs.get("input")
    return details


def _extract_usage_details(response: Any) -> dict[str, Any]:
    """Extract usage details from response object."""
    details: dict[str, Any] = {}
    if hasattr(response, "usage") and response.usage:
        if hasattr(response.usage, "prompt_tokens"):
            details["token_usage_prompt"] = response.usage.prompt_tokens
        if hasattr(response.usage, "completion_tokens"):
            details["token_usage_completion"] = response.usage.completion_tokens
        if hasattr(response.usage, "total_tokens"):
            details["token_usage_total"] = response.usage.total_tokens
    return details


def _extract_completion_details(method_name: str, response: Any) -> dict[str, Any]:
    """Extract completion details from response object."""
    details: dict[str, Any] = {}
    if not hasattr(response, "choices") or not response.choices:
        return details
    try:
        first_choice = response.choices[0]
        if "ChatCompletions" in method_name:
            if hasattr(first_choice, "message") and first_choice.message:
                details["completion_message_full"] = (
                    first_choice.message.model_dump()
                    if hasattr(first_choice.message, "model_dump")
                    else {
                        "role": _safe_getattr(first_choice.message, "role"),
                        "content": _safe_getattr(first_choice.message, "content"),
                    }
                )
        elif "Completions" in method_name and "Chat" not in method_name:
            if hasattr(first_choice, "text"):
                details["completion_text_full"] = first_choice.text
    except (AttributeError, IndexError):
        pass
    return details


def _extract_response_details(
    method_name: str, response: Any, is_stream_response: bool = False
) -> dict[str, Any]:
    """Extracts details from the OpenAI API response for SDK v1.x."""
    details = _extract_usage_details(response)
    if not is_stream_response and _config.include_content:
        details.update(_extract_completion_details(method_name, response))
    return details


class _StreamAccumulator:
    """Helper to accumulate data from OpenAI stream chunks."""

    def __init__(self, method_name: str, include_content: bool):
        self.method_name = method_name
        self.include_content = include_content
        self.accumulated_content: list[str] = []
        self.full_completion_deltas: list[
            dict[str, Any]
        ] = []  # For chat, if needed for structured reconstruction
        self.role: str | None = None

    def process_chunk(self, chunk: Any) -> None:
        """Processes a single chunk from an OpenAI stream."""
        if not self.include_content:
            return

        if not hasattr(chunk, "choices") or not chunk.choices:
            return

        try:
            delta = chunk.choices[0].delta
            if delta:
                if hasattr(delta, "role") and delta.role is not None:
                    self.role = delta.role  # Capture role if present

                content_piece = _safe_getattr(delta, "content")
                if content_piece:
                    self.accumulated_content.append(content_piece)

                # For more structured accumulation if needed (e.g. tool calls in delta)
                # This part can be expanded based on specific needs for streamed content structure
                if hasattr(delta, "model_dump"):
                    self.full_completion_deltas.append(
                        delta.model_dump(exclude_unset=True)
                    )
                elif (
                    delta.content is not None or delta.role is not None
                ):  # basic content
                    self.full_completion_deltas.append(
                        {"role": delta.role, "content": delta.content}
                    )

        except (AttributeError, IndexError):
            # Chunk structure might vary or be empty
            pass

    def get_accumulated_content_details(self) -> dict[str, Any]:
        """Returns a dictionary with the accumulated content from the stream."""
        if not self.include_content or not self.accumulated_content:
            return {}

        details: dict[str, Any] = {}
        joined_content = "".join(self.accumulated_content)

        if "ChatCompletions" in self.method_name:
            # For chat, it's common to provide the full message object if possible
            # Reconstructing the exact message structure from deltas can be complex,
            # especially with tool calls. For now, we send aggregated text content.
            # And the list of deltas for more detailed inspection if needed.
            details["completion_content_streamed"] = joined_content
            final_message_guess = {
                "role": self.role or "assistant",
                "content": joined_content,
            }
            details["completion_message_streamed_aggregated"] = final_message_guess
            # details["completion_deltas_streamed"] = self.full_completion_deltas # Optional: for very detailed logging

        elif "Completions" in self.method_name and "Chat" not in self.method_name:
            details["completion_text_streamed"] = joined_content

        return details


def _wrap_sync_stream(
    original_stream: Iterator[Any], telemetry_payload: dict[str, Any], method_name: str
) -> Iterator[Any]:
    """Wraps a synchronous stream iterator to capture telemetry."""
    stream_start_time_s = telemetry_payload[
        "timestamp_client_call_utc"
    ]  # Start time of the initial API call
    accumulator = _StreamAccumulator(method_name, _config.include_content)
    error_info_stream: dict[str, Any] | None = None
    status_code_stream: int = HTTPStatus.OK  # Assume success unless error during stream

    try:
        for chunk in original_stream:
            accumulator.process_chunk(chunk)
            yield chunk
    except openai.APIStatusError as e:
        status_code_stream = e.status_code
        error_info_stream = {
            "error_type": type(e).__name__,
            "error_message": str(_safe_getattr(e, "message", str(e))),
            "error_code": _safe_getattr(e, "code"),
            "error_param": _safe_getattr(e, "param"),
        }
        raise  # Re-raise to propagate to user
    except Exception as e:
        status_code_stream = HTTPStatus.INTERNAL_SERVER_ERROR
        error_info_stream = {"error_type": type(e).__name__, "error_message": str(e)}
        raise  # Re-raise
    finally:
        telemetry_payload.update(accumulator.get_accumulated_content_details())

        final_response_obj = _safe_getattr(original_stream, "response")
        if final_response_obj:
            # Extract usage and potentially other final metadata (status code from final response if available)
            usage_and_metadata = _extract_response_details(
                method_name, final_response_obj, is_stream_response=True
            )
            telemetry_payload.update(usage_and_metadata)
            # If the stream errored, status_code_stream would be set. Otherwise, try to get from final response.
            if status_code_stream == HTTPStatus.OK and hasattr(final_response_obj, "status_code"):
                status_code_stream = final_response_obj.status_code
            elif hasattr(original_stream, "_response") and hasattr(
                original_stream._response, "status_code"
            ):  # Fallback for older stream objects
                if status_code_stream == HTTPStatus.OK:
                    status_code_stream = original_stream._response.status_code
        else:
            # This case means we couldn't get a final response object from the stream to extract usage.
            # Token usage will be missing. Latency will still be recorded.
            logger.warning(
                f"Could not retrieve final response object from stream for {method_name}. Token usage data may be missing."
            )

        telemetry_payload["latency_ms"] = int(
            (time.time() - stream_start_time_s) * 1000
        )
        telemetry_payload["status_code"] = (
            status_code_stream  # This reflects stream processing status
        )
        if error_info_stream:
            telemetry_payload["error_info"] = error_info_stream

        _send_telemetry(telemetry_payload)


async def _wrap_async_stream(
    original_stream: AsyncIterator[Any],
    telemetry_payload: dict[str, Any],
    method_name: str,
) -> AsyncIterator[Any]:
    """Wraps an asynchronous stream iterator to capture telemetry."""
    stream_start_time_s = telemetry_payload["timestamp_client_call_utc"]
    accumulator = _StreamAccumulator(method_name, _config.include_content)
    error_info_stream: dict[str, Any] | None = None
    status_code_stream: int = HTTPStatus.OK

    try:
        async for chunk in original_stream:
            accumulator.process_chunk(chunk)
            yield chunk
    except openai.APIStatusError as e:
        status_code_stream = e.status_code
        error_info_stream = {
            "error_type": type(e).__name__,
            "error_message": str(_safe_getattr(e, "message", str(e))),
            "error_code": _safe_getattr(e, "code"),
            "error_param": _safe_getattr(e, "param"),
        }
        raise
    except Exception as e:
        status_code_stream = HTTPStatus.INTERNAL_SERVER_ERROR
        error_info_stream = {"error_type": type(e).__name__, "error_message": str(e)}
        raise
    finally:
        telemetry_payload.update(accumulator.get_accumulated_content_details())

        final_response_obj = _safe_getattr(original_stream, "response")
        if final_response_obj:
            usage_and_metadata = _extract_response_details(
                method_name, final_response_obj, is_stream_response=True
            )
            telemetry_payload.update(usage_and_metadata)
            if status_code_stream == HTTPStatus.OK and hasattr(final_response_obj, "status_code"):
                status_code_stream = final_response_obj.status_code
            elif hasattr(original_stream, "_response") and hasattr(
                original_stream._response, "status_code"
            ):
                if status_code_stream == HTTPStatus.OK:
                    status_code_stream = original_stream._response.status_code
        else:
            logger.warning(
                f"Could not retrieve final response object from async stream for {method_name}. Token usage data may be missing."
            )

        telemetry_payload["latency_ms"] = int(
            (time.time() - stream_start_time_s) * 1000
        )
        telemetry_payload["status_code"] = status_code_stream
        if error_info_stream:
            telemetry_payload["error_info"] = error_info_stream

        _send_telemetry(telemetry_payload)


def _create_wrapper(
    original_func: Callable[..., Any], method_identifier: str
) -> Callable[..., Any]:
    """
    Creates a synchronous wrapper for SDK v1.x. Handles regular and streaming calls.
    """
    @wraps(original_func)
    def wrapper(self_instance: Any, *args: Any, **kwargs: Any) -> Any:
        start_time_s = time.time()  # For non-streaming latency or base for streaming

        telemetry_payload_base: dict[str, Any] = {
            "event_id": hashlib.sha256(os.urandom(32)).hexdigest(),
            "app_name": _config.app_name,
            "api_method": method_identifier,
            "timestamp_client_call_utc": start_time_s,
        }
        telemetry_payload_base.update(_extract_call_details(method_identifier, kwargs))

        if kwargs.get("stream") is True:
            stream_iterator = original_func(self_instance, *args, **kwargs)
            # Telemetry is sent by _wrap_sync_stream after stream is consumed
            return _wrap_sync_stream(
                stream_iterator, telemetry_payload_base, method_identifier
            )
        else:
            response_obj: Any = None
            error_info: dict[str, Any] | None = None
            status_code: int = 0
            try:
                response_obj = original_func(self_instance, *args, **kwargs)
                if (
                    hasattr(self_instance, "_client")
                    and hasattr(self_instance._client, "last_response")
                    and self_instance._client.last_response
                ):
                    status_code = self_instance._client.last_response.status_code
                else:
                    status_code = HTTPStatus.OK
                telemetry_payload_base.update(
                    _extract_response_details(method_identifier, response_obj)
                )
            except openai.APIStatusError as e:
                status_code = e.status_code
                error_info = {
                    "error_type": type(e).__name__,
                    "error_message": str(_safe_getattr(e, "message", str(e))),
                    "error_code": _safe_getattr(e, "code"),
                    "error_param": _safe_getattr(e, "param"),
                }
                raise
            except openai.APIError as e:
                status_code = _safe_getattr(e, "status_code", HTTPStatus.INTERNAL_SERVER_ERROR)
                error_info = {
                    "error_type": type(e).__name__,
                    "error_message": str(_safe_getattr(e, "message", str(e))),
                    "error_code": _safe_getattr(e, "code"),
                }
                raise
            except Exception as e:
                status_code = HTTPStatus.INTERNAL_SERVER_ERROR
                error_info = {"error_type": type(e).__name__, "error_message": str(e)}
                raise
            finally:
                telemetry_payload_base["latency_ms"] = int(
                    (time.time() - start_time_s) * 1000
                )
                telemetry_payload_base["status_code"] = status_code
                if error_info:
                    telemetry_payload_base["error_info"] = error_info
                _send_telemetry(telemetry_payload_base)
            return response_obj

    return wrapper


def _create_async_wrapper(
    original_func: Callable[..., Coroutine[Any, Any, Any]], method_identifier: str
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """
    Creates an asynchronous wrapper for SDK v1.x. Handles regular and streaming calls.
    """
    @wraps(original_func)
    async def async_wrapper(self_instance: Any, *args: Any, **kwargs: Any) -> Any:
        start_time_s = time.time()

        telemetry_payload_base: dict[str, Any] = {
            "event_id": hashlib.sha256(os.urandom(32)).hexdigest(),
            "app_name": _config.app_name,
            "api_method": method_identifier,
            "timestamp_client_call_utc": start_time_s,
        }
        telemetry_payload_base.update(_extract_call_details(method_identifier, kwargs))

        if kwargs.get("stream") is True:
            stream_iterator = await original_func(self_instance, *args, **kwargs)
            # Telemetry is sent by _wrap_async_stream after stream is consumed
            return _wrap_async_stream(
                stream_iterator, telemetry_payload_base, method_identifier
            )
        else:
            response_obj: Any = None
            error_info: dict[str, Any] | None = None
            status_code: int = 0
            try:
                response_obj = await original_func(self_instance, *args, **kwargs)
                if (
                    hasattr(self_instance, "_client")
                    and hasattr(self_instance._client, "last_response")
                    and self_instance._client.last_response
                ):
                    status_code = self_instance._client.last_response.status_code
                else:
                    status_code = HTTPStatus.OK
                telemetry_payload_base.update(
                    _extract_response_details(method_identifier, response_obj)
                )
            except openai.APIStatusError as e:
                status_code = e.status_code
                error_info = {
                    "error_type": type(e).__name__,
                    "error_message": str(_safe_getattr(e, "message", str(e))),
                    "error_code": _safe_getattr(e, "code"),
                    "error_param": _safe_getattr(e, "param"),
                }
                raise
            except openai.APIError as e:
                status_code = _safe_getattr(e, "status_code", HTTPStatus.INTERNAL_SERVER_ERROR)
                error_info = {
                    "error_type": type(e).__name__,
                    "error_message": str(_safe_getattr(e, "message", str(e))),
                    "error_code": _safe_getattr(e, "code"),
                }
                raise
            except Exception as e:
                status_code = HTTPStatus.INTERNAL_SERVER_ERROR
                error_info = {"error_type": type(e).__name__, "error_message": str(e)}
                raise
            finally:
                telemetry_payload_base["latency_ms"] = int(
                    (time.time() - start_time_s) * 1000
                )
                telemetry_payload_base["status_code"] = status_code
                if error_info:
                    telemetry_payload_base["error_info"] = error_info
                _send_telemetry(telemetry_payload_base)
            return response_obj

    return async_wrapper


@dataclass
class PatchConfig:
    """Configuration for patching an OpenAI module method."""
    tele_id: str
    mod_path_str: str
    class_name: str
    method_name: str
    wrapper_type: str

def _apply_patch(openai_module: Any, config: PatchConfig) -> None:
    """Apply a single patch to an OpenAI module method."""
    try:
        module_parts = config.mod_path_str.split(".")
        current_mod_or_class = openai_module
        for part in module_parts[1:]:
            if not hasattr(current_mod_or_class, part):
                logger.info(f"Path '{config.mod_path_str}' component '{part}' not found. Skipping {config.tele_id}.")
                return
            current_mod_or_class = getattr(current_mod_or_class, part)

        target_class = getattr(current_mod_or_class, config.class_name, None)
        if target_class is None:
            logger.info(f"Class '{config.class_name}' not found in '{config.mod_path_str}'. Skipping {config.tele_id}.")
            return

        if not hasattr(target_class, config.method_name):
            return

        original_method = getattr(target_class, config.method_name)
        full_method_path = f"{config.mod_path_str}.{config.class_name}.{config.method_name}"

        if not callable(original_method) or hasattr(original_method, "__is_wiretapp_wrapped__"):
            return

        if full_method_path not in _config.original_methods:
            _config.original_methods[full_method_path] = original_method
            wrapper_func_creator = _create_wrapper if config.wrapper_type == "sync" else _create_async_wrapper
            wrapped_method = wrapper_func_creator(original_method, config.tele_id)
            wrapped_method.__is_wiretapp_wrapped__ = True
            setattr(target_class, config.method_name, wrapped_method)
            logger.info(f"Patched {full_method_path} (Telemetry ID: {config.tele_id})")
        else:
            logger.info(f"INFO - {full_method_path} was already processed (original stored). Skipping re-patch.")

    except Exception as e:
        logger.error(f"Failed to process patch for {config.tele_id} due to an unexpected error: {e}", exc_info=True)

def monitor(openai_module: Any, app_name: str) -> None:
    """Applies monkey-patching to the provided OpenAI module's resource classes."""
    _config.app_name = app_name
    _config.wiretapp_endpoint = os.getenv("WIRETAPP_ENDPOINT", "http://localhost:8000/events")
    _config.include_content = os.getenv("WIRETAPP_INCLUDE_CONTENT", "0") == "1"

    logger.info(f"Initializing monitor for app '{_config.app_name}' (SDK v1.x mode). Endpoint: '{_config.wiretapp_endpoint}'. Include content: {_config.include_content}")

    patch_configs = [
        PatchConfig("ChatCompletions.create", "openai.resources.chat.completions", "Completions", "create", "sync"),
        PatchConfig("AsyncChatCompletions.create", "openai.resources.chat.completions", "AsyncCompletions", "create", "async"),
        PatchConfig("Embeddings.create", "openai.resources.embeddings", "Embeddings", "create", "sync"),
        PatchConfig("AsyncEmbeddings.create", "openai.resources.embeddings", "AsyncEmbeddings", "create", "async"),
        PatchConfig("Completions.create", "openai.resources.completions", "Completions", "create", "sync"),
        PatchConfig("AsyncCompletions.create", "openai.resources.completions", "AsyncCompletions", "create", "async"),
    ]

    for config in patch_configs:
        _apply_patch(openai_module, config)
