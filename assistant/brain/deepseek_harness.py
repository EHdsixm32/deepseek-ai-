"""DeepSeek harness：统一的 OpenAI 兼容聊天接口封装。

默认接入 DeepSeek 官方 API（https://api.deepseek.com/v1），模型默认
deepseek-chat / deepseek-reasoner。也兼容任何 OpenAI Chat Completions
格式的服务，用户可在 config.json 中修改 base_url 与 model。

本模块不依赖 openai 包：优先用 requests，缺少时自动退回标准库 urllib。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterator
from urllib import error as urlerror
from urllib import request as urlrequest

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class DeepSeekError(RuntimeError):
    """所有 DeepSeek 调用错误统一包装，便于 UI 友好展示。"""


class DeepSeekHarness:
    def __init__(self, config: Any = None, *, api_key: str | None = None,
                 base_url: str | None = None, model: str | None = None):
        # 保留 config 引用和显式覆盖项，这样在设置界面修改 API Key 后，
        # 无需重建 harness 也能立即生效。
        self._config = config if config is not None else None
        self._api_key_override = api_key
        self._base_url_override = base_url
        self._model_override = model
        self._request_session = None
        self._session = None
        self.refresh_from_config()
        try:
            import requests  # type: ignore
            self._session = requests.Session()
        except Exception:
            self._session = None

    def refresh_from_config(self) -> None:
        """每次调用前从 ConfigManager 重新读取，保证设置界面改动即时生效。"""
        d: dict[str, Any] = {}
        env_name = "DEEPSEEK_API_KEY"
        if self._config is not None and hasattr(self._config, "get"):
            raw = self._config.get("deepseek", {})
            if isinstance(raw, dict):
                d = raw
                env_name = str(d.get("api_key_env", "DEEPSEEK_API_KEY"))
        self.api_key = str(
            self._api_key_override
            or d.get("api_key", "")
            or os.environ.get(env_name, "")
            or ""
        ).strip()
        self.base_url = str(
            self._base_url_override or d.get("base_url", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.model = str(self._model_override or d.get("model", DEFAULT_MODEL))
        self.timeout = float(d.get("timeout_seconds", 90))

    # ---------- 基础状态 ----------
    def is_configured(self) -> bool:
        self.refresh_from_config()
        return bool(self.api_key)

    def describe(self) -> str:
        self.refresh_from_config()
        return f"{self.base_url} | {self.model} | key={'已设置' if self.api_key else '未设置'}"

    # ---------- 核心调用 ----------
    def chat(self, messages: list[dict[str, str]], *, temperature: float | None = None,
             max_tokens: int | None = None, model: str | None = None,
             stream: bool = False, json_mode: bool = False,
             extra_body: dict[str, Any] | None = None) -> str | Iterator[str]:
        """普通/流式对话。

        stream=False 返回完整文本；stream=True 返回增量文本生成器。
        """
        self.refresh_from_config()
        temperature = 0.7 if temperature is None else float(temperature)
        max_tokens = 2048 if max_tokens is None else int(max_tokens)
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": bool(stream),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if extra_body:
            body.update(extra_body)

        if stream:
            return self._stream_chat(body)
        return self._request_chat(body)

    def chat_full(self, messages: list[dict[str, str]], *,
                  temperature: float | None = None, max_tokens: int | None = None,
                  model: str | None = None, tools: list[dict[str, Any]] | None = None,
                  tool_choice: str | None = "auto") -> dict[str, Any]:
        """非流式调用，返回完整的 assistant message。

        返回结构：{"role": "assistant", "content": str,
                   "reasoning_content": str, "tool_calls": [...]}
        用于工具调用循环和“思考过程”展示。
        """
        temperature = 0.7 if temperature is None else float(temperature)
        max_tokens = 2048 if max_tokens is None else int(max_tokens)
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        raw = self._request_raw(body)
        try:
            message = raw["choices"][0]["message"]
        except Exception as exc:
            raise DeepSeekError(f"DeepSeek 响应缺少 message：{raw}") from exc
        tool_calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args_text = fn.get("arguments") or "{}"
                args = json.loads(args_text) if isinstance(args_text, str) else args_text
            except Exception:
                args = {}
            tool_calls.append({
                "id": tc.get("id") or "",
                "type": "function",
                "function": {"name": fn.get("name") or "", "arguments": args},
            })
        return {
            "role": "assistant",
            "content": str(message.get("content") or ""),
            "reasoning_content": str(message.get("reasoning_content") or ""),
            "tool_calls": tool_calls,
        }

    def chat_json(self, messages: list[dict[str, str]], *, temperature: float = 0.1,
                  max_tokens: int = 600, model: str | None = None) -> Any:
        """请求 JSON 对象。优先使用服务端 JSON mode，失败时容错解析。"""
        try:
            text = str(self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                                 model=model, json_mode=True))
            return _extract_json(text)
        except Exception:
            # 某些兼容服务不支持 response_format，降级为普通请求再解析
            text = str(self.chat(messages, temperature=temperature, max_tokens=max_tokens, model=model))
            return _extract_json(text)

    # ---------- 内部实现 ----------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request_raw(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise DeepSeekError("还没有配置 DeepSeek API Key。请在设置中填写，或设置环境变量 DEEPSEEK_API_KEY。")
        if self._session is not None:
            try:
                resp = self._session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(), json=body, timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    raise DeepSeekError(_format_http_error(resp.status_code, resp.text))
                return resp.json()
            except DeepSeekError:
                raise
            except Exception as exc:
                raise DeepSeekError(f"DeepSeek 请求失败：{exc}") from exc

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            f"{self.base_url}/chat/completions", data=data,
            headers=self._headers(), method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            raise DeepSeekError(_format_http_error(exc.code, detail)) from exc
        except Exception as exc:
            raise DeepSeekError(f"DeepSeek 请求失败：{exc}") from exc

    def _request_chat(self, body: dict[str, Any]) -> str:
        return _extract_content(self._request_raw(body))

    def _stream_chat(self, body: dict[str, Any]) -> Iterator[str]:
        if not self.api_key:
            raise DeepSeekError("还没有配置 DeepSeek API Key。请在设置中填写，或设置环境变量 DEEPSEEK_API_KEY。")
        if self._session is not None:
            with self._session.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(), json=body, timeout=self.timeout, stream=True,
            ) as resp:
                if resp.status_code >= 400:
                    raise DeepSeekError(_format_http_error(resp.status_code, resp.text[:800]))
                for raw in resp.iter_lines():
                    token = _parse_sse_line(raw)
                    if token is not None:
                        yield token
            return

        req = urlrequest.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers=self._headers(), method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    try:
                        line = raw.decode("utf-8", errors="replace").strip()
                    except Exception:
                        line = str(raw).strip()
                    token = _parse_sse_line(line)
                    if token is not None:
                        yield token
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            raise DeepSeekError(_format_http_error(exc.code, detail)) from exc
        except Exception as exc:
            raise DeepSeekError(f"DeepSeek 流式请求失败：{exc}") from exc


def _parse_sse_line(line: str | bytes) -> str | None:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8", errors="replace")
        except Exception:
            return None
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line == "data: [DONE]":
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    try:
        data = json.loads(line)
    except Exception:
        return None
    return _extract_content(data)


def _extract_content(data: dict[str, Any]) -> str:
    try:
        choices = data.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            msg = choices[0].get("message") or {}
            return str(delta.get("content") or msg.get("content") or "")
    except Exception:
        pass
    if isinstance(data, dict) and "error" in data:
        raise DeepSeekError(str(data["error"]))
    return ""


def _format_http_error(status: int, text: str) -> str:
    detail = str(text or "")[:500]
    if status == 401:
        return "API Key 无效或未授权（401）。请检查设置中的 Key。"
    if status == 402:
        return "DeepSeek 账户余额不足（402）。"
    if status == 429:
        return "请求太频繁，请稍后再试（429）。"
    return f"DeepSeek 返回错误 {status}：{detail}"


def _extract_json(text: str) -> Any:
    text = str(text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    match = re.search(r"```json\s*(.*?)```", text, re.S)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    raise DeepSeekError(f"无法从模型输出解析 JSON：{text[:200]}")
