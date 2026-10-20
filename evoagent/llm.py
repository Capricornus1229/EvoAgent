"""Small OpenAI-compatible JSON client with auditable usage accounting."""
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .telemetry import ExecutionLedger
from .runtime import check_execution, execution_timeout


class JsonChatClient:
    def __init__(
        self, base_url: str, api_key: str, model: str,
        provider: str = "openai-compatible", timeout: int = 60,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})

    def complete_json(
        self, role: str, system: str, user: str,
        ledger: Optional[ExecutionLedger] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        check_execution()
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self.extra_headers)
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        started = time.monotonic()
        usage, ok, message = {}, False, ""
        try:
            with urllib.request.urlopen(request, timeout=execution_timeout(self.timeout)) as response:
                body = json.loads(response.read().decode("utf-8"))
            # Usage remains billable when content validation or deadline checks fail.
            usage = body.get("usage") or {} if isinstance(body, dict) else {}
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("model JSON root is not an object")
            check_execution()
            ok = True
            return result
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            message = "%s API returned HTTP %d: %s" % (
                self.provider, exc.code, detail,
            )
            raise RuntimeError(message) from exc
        except (urllib.error.URLError, socket.timeout, ValueError, KeyError,
                IndexError, TypeError, json.JSONDecodeError) as exc:
            message = "%s JSON request failed: %s" % (self.provider, exc)
            raise RuntimeError(message) from exc
        except BaseException as exc:
            message = str(exc)
            raise
        finally:
            if ledger:
                ledger.record_model(role, self.provider, self.model, usage,
                    int((time.monotonic() - started) * 1000), ok, message)
