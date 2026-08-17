"""本地文件工具：让 AI 助手像 DeepSeek harness 一样直接读写/修改文件。

安全边界：
1. 所有路径默认被限制在 （默认为本程序目录）；
2. 读 / 写 / 改 / 删分别有独立权限开关；
3. 写类操作默认需要用户逐次批准（GUI 弹窗，CLI 询问）；
4. 路径统一 resolve，拒绝目录穿越与符号链接逃逸；
5. 超过  的文件拒绝读入或写出。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable

from ..config import APP_DIR

Approver = Callable[[str, dict[str, Any], str, str], tuple[bool, str]]

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内的文本文件。返回带行号的文本片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于工作区根目录的路径，例如 src/main.py"},
                    "offset": {"type": "integer", "description": "从第几个字符开始读取，默认 0"},
                    "limit": {"type": "integer", "description": "最多读取字符数，默认 4000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建或覆盖写入工作区内的 UTF-8 文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "对工作区文件做精确文本替换。old_text 必须唯一出现一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "向工作区文件末尾追加文本，文件不存在时创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出工作区内目录的文件和子目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于工作区根目录的目录，默认 ."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除工作区内的单个文件（不可恢复，默认权限关闭）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
]

_READ_TOOLS = {"read_file", "list_directory"}
_WRITE_TOOLS = {"write_file", "append_file"}
_RISK_TOOLS = {"write_file", "append_file", "edit_file", "delete_file"}
_SENSITIVE_NAMES = {"config.json", "config_mark.json", ".env", ".git-credentials", "credentials", "secrets", "目录.md"}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
# 助手自己的记忆系统只允许通过目录文件 -> 目的文件的两段式检索访问，
# 不允许文件工具直接翻看目的文件正文。
_SENSITIVE_DIRS = {"目的"}


class PermissionDenied(RuntimeError):
    pass


class FileToolExecutor:
    def __init__(self, config: Any, approver: Approver | None = None):
        self.config = config
        self.approver = approver

    # ---------- 对外接口 ----------
    def tool_definitions(self) -> list[dict[str, Any]]:
        if not self._cfg("enabled", True):
            return []
        return [t for t in TOOL_DEFINITIONS
                if self._permission_for(t["function"]["name"])]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments or {})
        try:
            if not self._cfg("enabled", True):
                return {"ok": False, "error": "文件工具已被用户关闭（tools.enabled=false）。"}
            allowed = self._permission_for(name)
            if not allowed:
                return {"ok": False, "error": f"当前权限不允许调用工具 {name}。"}
            path, display = self._resolve_path(str(args.get("path", "") if name != "list_directory" else args.get("path", ".")))
            self._check_size_pre(name, args, path)
            risk = "写/删" if name in _RISK_TOOLS else "读"
            if name in _RISK_TOOLS and not self._cfg("auto_approve", False):
                if self.approver is None:
                    return {"ok": False, "error": f"需要用户批准才能执行 {name}，但当前没有批准通道。"}
                approved, reason = self.approver(name, args, risk, display)
                if not approved:
                    return {"ok": False, "error": f"用户拒绝执行 {name}：{reason or '未说明原因'}"}
            result = self._dispatch(name, args, path)
            result.setdefault("path", display)
            return result
        except PermissionDenied as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---------- 权限与路径 ----------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(f"tools.{key}", default)
        except Exception:
            return default

    def _permission_for(self, name: str) -> bool:
        if name in _READ_TOOLS:
            return bool(self._cfg("allow_read", True)) and (name != "list_directory" or bool(self._cfg("allow_list", True)))
        if name in _WRITE_TOOLS:
            return bool(self._cfg("allow_write", True))
        if name == "edit_file":
            return bool(self._cfg("allow_edit", True))
        if name == "delete_file":
            return bool(self._cfg("allow_delete", False))
        return False

    def _resolve_path(self, raw: str) -> tuple[Path, str]:
        root_raw = str(self._cfg("workspace_root", ".")).strip() or "."
        root = Path(root_raw)
        if not root.is_absolute():
            root = (APP_DIR / root).resolve()
        else:
            root = root.resolve()
        raw_path = raw.strip()
        if not raw_path:
            raise PermissionDenied("路径不能为空。")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        allow_outside = bool(self._cfg("allow_outside_workspace", False))
        if not allow_outside:
            try:
                candidate.relative_to(root)
            except ValueError:
                raise PermissionDenied(
                    f"路径超出工作区范围：{candidate}。当前工作区：{root}。"
                    "如需访问，请在设置中开启 tools.allow_outside_workspace。"
                )
        if bool(self._cfg("deny_sensitive_files", True)) and self._is_sensitive(candidate, root):
            raise PermissionDenied(
                f"路径包含敏感文件（配置/密钥/凭据）：{candidate.name}。"
                "如需让助手操作，请在设置中关闭 tools.deny_sensitive_files。"
            )
        return candidate, str(candidate)

    @staticmethod
    def _is_sensitive(path: Path, root: Path) -> bool:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        if path.name.lower() in _SENSITIVE_NAMES or path.suffix.lower() in _SENSITIVE_SUFFIXES:
            return True
        if any(part.lower() in _SENSITIVE_DIRS for part in rel.parts):
            return True
        return any(part.lower() in _SENSITIVE_NAMES for part in rel.parts)

    def _check_size_pre(self, name: str, args: dict[str, Any], path: Path) -> None:
        max_bytes = int(self._cfg("max_file_bytes", 500000))
        if name in ("read_file",) and path.is_file():
            size = path.stat().st_size
            if size > max_bytes:
                raise PermissionDenied(f"文件 {path.name} 大小为 {size} 字节，超过上限 {max_bytes}。")
        if name in ("write_file", "append_file"):
            content = str(args.get("content", ""))
            if len(content.encode("utf-8")) > max_bytes:
                raise PermissionDenied(f"写入内容超过单文件上限 {max_bytes} 字节。")
        if name == "edit_file":
            new_text = str(args.get("new_text", ""))
            if len(new_text.encode("utf-8")) > max_bytes:
                raise PermissionDenied(f"修改后的文本超过单文件上限 {max_bytes} 字节。")

    # ---------- 具体工具 ----------
    def _dispatch(self, name: str, args: dict[str, Any], path: Path) -> dict[str, Any]:
        if name == "read_file":
            return self._read_file(path, int(args.get("offset", 0) or 0), int(args.get("limit", 4000) or 4000))
        if name == "list_directory":
            return self._list_directory(path)
        if name == "write_file":
            return self._write_file(path, str(args.get("content", "")), append=False)
        if name == "append_file":
            return self._write_file(path, str(args.get("content", "")), append=True)
        if name == "edit_file":
            return self._edit_file(path, str(args.get("old_text", "")), str(args.get("new_text", "")))
        if name == "delete_file":
            return self._delete_file(path)
        return {"ok": False, "error": f"未知工具 {name}"}

    @staticmethod
    def _read_file(path: Path, offset: int, limit: int) -> dict[str, Any]:
        if not path.exists():
            return {"ok": False, "error": f"文件不存在：{path.name}"}
        if not path.is_file():
            return {"ok": False, "error": f"不是文件：{path.name}"}
        text = path.read_text(encoding="utf-8", errors="replace")
        offset = max(0, min(offset, len(text)))
        limit = max(1, min(limit, 50000))
        chunk = text[offset:offset + limit]
        numbered = []
        start_line = text[:offset].count("\n") + 1
        for i, line in enumerate(chunk.splitlines(), start=start_line):
            numbered.append(f"{i:5d} | {line}")
        return {
            "ok": True,
            "content": "\n".join(numbered),
            "start_line": start_line,
            "total_chars": len(text),
            "truncated": offset + limit < len(text),
        }

    @staticmethod
    def _list_directory(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"ok": False, "error": f"目录不存在：{path.name}"}
        if not path.is_dir():
            return {"ok": False, "error": f"不是目录：{path.name}"}
        items = []
        for child in sorted(path.iterdir()):
            try:
                if child.is_dir():
                    items.append(f"[DIR]  {child.name}/")
                elif child.is_file():
                    items.append(f"[FILE] {child.name}  ({child.stat().st_size} B)")
            except Exception:
                continue
        return {"ok": True, "items": items[:300], "total": len(items)}

    @staticmethod
    def _write_file(path: Path, content: str, append: bool) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(content)
        return {"ok": True, "bytes": len(content.encode("utf-8")), "action": "append" if append else "write"}

    @staticmethod
    def _edit_file(path: Path, old_text: str, new_text: str) -> dict[str, Any]:
        if not path.is_file():
            return {"ok": False, "error": f"文件不存在：{path.name}"}
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if old_text == "" or count == 0:
            return {"ok": False, "error": "old_text 在文件中不存在。"}
        if count > 1:
            return {"ok": False, "error": f"old_text 出现了 {count} 次，请提供更长的上下文使其唯一。"}
        path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return {"ok": True, "replaced_chars": len(old_text), "action": "edit"}

    @staticmethod
    def _delete_file(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {"ok": False, "error": f"文件不存在或不是普通文件：{path.name}"}
        path.unlink()
        return {"ok": True, "action": "delete"}


__all__ = ["FileToolExecutor", "PermissionDenied", "TOOL_DEFINITIONS"]
