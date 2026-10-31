"""OpenAI 兼容的 chat/completions 客户端（DeepSeek 默认），只用标准库。

不引第三方包：打包版已经带了 ssl/certifi，``urllib`` 足够，PyInstaller 也不用补 hiddenimports。
**阻塞调用**——只能在工作线程里跑（见 ``director.py``），绝不能进 40 TPS 的 tick。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class LLMError(RuntimeError):
    pass


def _ssl_context() -> ssl.SSLContext:
    """优先用 certifi 的根证书（打包版里有），否则用系统证书库。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class ChatClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, timeout: float = 20.0):
        if not api_key:
            raise LLMError("缺少 API Key")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self._ctx = _ssl_context()

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = None
        method = "GET"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            method = "POST"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"网络错误: {exc}") from exc
        except ValueError as exc:
            raise LLMError(f"响应不是 JSON: {exc}") from exc

    def balance(self) -> dict:
        """``GET /user/balance``（DeepSeek 专有）。返回 ``{"available": bool, "items": [(币种, 余额)]}``。"""
        body = self._request("/user/balance")
        items = []
        for info in body.get("balance_infos") or []:
            if isinstance(info, dict):
                items.append((str(info.get("currency", "")), str(info.get("total_balance", "?"))))
        return {"available": bool(body.get("is_available", True)), "items": items}

    def complete(self, messages: list[dict], max_tokens: int = 300,
                 temperature: float = 1.0, json_mode: bool = True, model: str | None = None) -> tuple[str, dict]:
        """发一轮对话，返回 ``(assistant 文本, usage)``。失败抛 ``LLMError``。

        ``model``：这一轮换个模型（读带图的珍珠时用看图模型）。
        """
        payload: dict = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = self._request("/chat/completions", payload)
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应格式不对: {str(body)[:200]}") from exc
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return text, usage

    def complete_tools(self, messages: list[dict], tools: list[dict], max_tokens: int = 2000,
                       temperature: float = 0.3) -> tuple[dict, dict]:
        """带工具的一轮（委托任务执行器用）。返回 ``(assistant message, usage)``。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        body = self._request("/chat/completions", payload)
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应格式不对: {str(body)[:200]}") from exc
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return (message if isinstance(message, dict) else {}), usage
