#!/usr/bin/env python3
"""Minimal MCP (Model Context Protocol) server for jobautomat, stdlib only.

The server speaks JSON-RPC 2.0 over stdio: one JSON message per line on
stdin, one JSON response per line on stdout (flushed immediately). It does
not import the ``mcp`` SDK on purpose — no third-party dependency is needed
to expose the panel to an agent.

Every tool is a thin wrapper around the Flask panel API (``service/web.py``),
so the server must run on a machine that can reach the panel.

Environment
-----------
``JOBAUTOMAT_URL``       Panel base URL (default ``http://localhost:8085``).
``JOBAUTOMAT_USER``      Basic auth user (default ``casa``).
``JOBAUTOMAT_PASSWORD``  Basic auth password; when unset no auth header is sent.
``JOBAUTOMAT_TIMEOUT``   HTTP timeout per call in seconds (default ``120``;
                         approving an offer runs several LLM calls).

Registering it
--------------
Claude Desktop (``claude_desktop_config.json``)::

    {
      "mcpServers": {
        "jobautomat": {
          "command": "python3",
          "args": ["/abs/path/to/jobautomat/mcp/server.py"],
          "env": {
            "JOBAUTOMAT_URL": "http://localhost:8085",
            "JOBAUTOMAT_PASSWORD": "the-panel-password"
          }
        }
      }
    }

Cursor (``.cursor/mcp.json``): same shape. The interpreter only needs the
standard library, so the project virtualenv is optional.

Smoke test::

    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 mcp/server.py
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "jobautomat"
SERVER_VERSION = "1.0.0"

BASE_URL = (os.environ.get("JOBAUTOMAT_URL") or "http://localhost:8085").rstrip("/")
AUTH_USER = (os.environ.get("JOBAUTOMAT_USER") or "casa").strip()
AUTH_PASSWORD = (os.environ.get("JOBAUTOMAT_PASSWORD") or "").strip()
try:
    TIMEOUT = int(os.environ.get("JOBAUTOMAT_TIMEOUT") or 120)
except ValueError:
    TIMEOUT = 120


class ToolError(Exception):
    """Raised when the panel call fails; the message reaches the agent."""


def _url(path):
    """Build an absolute panel URL.

    Args:
        path: Endpoint path starting with ``/``.

    Returns:
        The full URL.
    """
    return "%s%s" % (BASE_URL, path)


def _quote(value):
    """Quote a value for use inside a URL path.

    Args:
        value: Raw value (offer id, for instance).

    Returns:
        The URL-encoded value.
    """
    return urllib.parse.quote(str(value), safe="")


def _request(path, method="GET", payload=None):
    """Call the panel API and return the decoded JSON body.

    Args:
        path: Endpoint path (with query string when needed).
        method: HTTP method.
        payload: Optional dict sent as the JSON request body.

    Returns:
        The parsed JSON response, or the raw text when it is not JSON.

    Raises:
        ToolError: On network failure, HTTP error or invalid JSON response.
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if AUTH_PASSWORD:
        token = base64.b64encode(
            ("%s:%s" % (AUTH_USER, AUTH_PASSWORD)).encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = "Basic %s" % token

    request = urllib.request.Request(_url(path), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — the status alone is still useful.
            detail = ""
        raise ToolError(
            "panel answered HTTP %s: %s" % (err.code, (detail or err.reason or "").strip()[:800])
        )
    except urllib.error.URLError as err:
        raise ToolError("cannot reach %s: %s" % (BASE_URL, err.reason))
    except Exception as err:  # noqa: BLE001 — surface any transport problem.
        raise ToolError("request failed: %s" % err)

    try:
        return json.loads(raw)
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _tool_listar_ofertas(args):
    """List offers filtered by state.

    Args:
        args: Tool arguments with an optional ``estado``.

    Returns:
        The panel response body.
    """
    estado = str(args.get("estado") or "pendientes").strip() or "pendientes"
    return _request("/api/ofertas?estado=%s" % _quote(estado))


def _tool_detalle_oferta(args):
    """Return one offer with its generated material.

    Args:
        args: Tool arguments with the offer ``id``.

    Returns:
        The panel response body.
    """
    return _request("/api/oferta/%s" % _quote(args["id"]))


def _tool_aprobar_oferta(args):
    """Approve an offer and generate the material pack.

    Args:
        args: Tool arguments with the offer ``id``.

    Returns:
        The panel response body (cover letter, CV, message, links).
    """
    return _request("/api/oferta/%s/aprobar" % _quote(args["id"]), method="POST", payload={})


def _tool_descartar_oferta(args):
    """Mark an offer as discarded.

    Args:
        args: Tool arguments with the offer ``id``.

    Returns:
        The panel response body.
    """
    return _request("/api/oferta/%s/descartar" % _quote(args["id"]), method="POST", payload={})


def _tool_buscar_ahora(args):
    """Launch the search pipeline on demand.

    Args:
        args: Unused tool arguments.

    Returns:
        The panel response body (202 accepted, or 409 when a run is active).
    """
    return _request("/api/buscar", method="POST", payload={})


def _tool_leer_config(args):
    """Return the effective configuration with secrets masked.

    Args:
        args: Unused tool arguments.

    Returns:
        The panel response body.
    """
    return _request("/api/config")


def _tool_leer_perfil(args):
    """Return the candidate profile (data/perfil.json).

    Args:
        args: Unused tool arguments.

    Returns:
        The panel response body.
    """
    return _request("/api/perfil")


def _tool_preguntar(args):
    """Answer an application question using the profile.

    Args:
        args: Tool arguments with ``texto`` and an optional ``contexto``.

    Returns:
        The panel response body with one answer per line of text.
    """
    payload = {"pregunta": args["texto"]}
    contexto = (args.get("contexto") or "").strip()
    if contexto:
        payload["contexto"] = contexto
    return _request("/api/preguntar", method="POST", payload=payload)


def _tool_salud(args):
    """Run the self-host diagnostics.

    Args:
        args: Unused tool arguments.

    Returns:
        The panel response body.
    """
    return _request("/api/salud")


def _tool_leer_log(args):
    """Tail the pipeline log.

    Args:
        args: Tool arguments with an optional ``n`` line count.

    Returns:
        The panel response body.
    """
    try:
        n = int(args.get("n") or 50)
    except (TypeError, ValueError):
        n = 50
    n = max(1, min(n, 3000))
    return _request("/api/log?n=%d" % n)


# name -> (handler, description, input schema)
TOOLS = {
    "listar_ofertas": (
        _tool_listar_ofertas,
        "List job offers: pendientes (default), aprobadas, descartadas or todas.",
        {
            "type": "object",
            "properties": {
                "estado": {
                    "type": "string",
                    "enum": ["pendientes", "aprobadas", "descartadas", "todas"],
                    "description": "State filter; defaults to pendientes.",
                }
            },
        },
    ),
    "detalle_oferta": (
        _tool_detalle_oferta,
        "Full detail of one offer: score breakdown, requirements and material.",
        {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Offer id."}},
            "required": ["id"],
        },
    ),
    "aprobar_oferta": (
        _tool_aprobar_oferta,
        "Approve an offer and generate its CV, cover letter and message.",
        {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Offer id."}},
            "required": ["id"],
        },
    ),
    "descartar_oferta": (
        _tool_descartar_oferta,
        "Discard an offer so it stops showing up as pending.",
        {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Offer id."}},
            "required": ["id"],
        },
    ),
    "buscar_ahora": (
        _tool_buscar_ahora,
        "Run the search pipeline now (same as the Search now button).",
        {"type": "object", "properties": {}},
    ),
    "leer_config": (
        _tool_leer_config,
        "Effective configuration with secrets masked.",
        {"type": "object", "properties": {}},
    ),
    "leer_perfil": (
        _tool_leer_perfil,
        "Candidate profile used for scoring and writing.",
        {"type": "object", "properties": {}},
    ),
    "preguntar": (
        _tool_preguntar,
        "Answer application questions with the LLM, grounded on the profile.",
        {
            "type": "object",
            "properties": {
                "texto": {
                    "type": "string",
                    "description": "Question(s) to answer, one per line.",
                },
                "contexto": {
                    "type": "string",
                    "description": "Optional offer or company text for context.",
                },
            },
            "required": ["texto"],
        },
    ),
    "salud": (
        _tool_salud,
        "Self-host diagnostics: LLM, Telegram, LinkedIn session, /data, Chromium.",
        {"type": "object", "properties": {}},
    ),
    "leer_log": (
        _tool_leer_log,
        "Tail the pipeline log to follow a running search.",
        {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of lines (1-3000, default 50).",
                    "minimum": 1,
                    "maximum": 3000,
                }
            },
        },
    ),
}


def _tools_list():
    """Build the ``tools/list`` result.

    Returns:
        A dict with the tool descriptors in declaration order.
    """
    return {
        "tools": [
            {
                "name": name,
                "description": description,
                "inputSchema": schema,
            }
            for name, (_handler, description, schema) in TOOLS.items()
        ]
    }


def _tools_call(params):
    """Run one tool and wrap its output as MCP content.

    Args:
        params: JSON-RPC params with ``name`` and ``arguments``.

    Returns:
        The ``result`` object with ``content`` and ``isError``.
    """
    name = (params or {}).get("name") or ""
    arguments = (params or {}).get("arguments") or {}
    entry = TOOLS.get(name)
    if entry is None:
        return {
            "content": [{"type": "text", "text": "unknown tool: %s" % name}],
            "isError": True,
        }
    if not isinstance(arguments, dict):
        return {
            "content": [{"type": "text", "text": "'arguments' must be a JSON object"}],
            "isError": True,
        }

    handler = entry[0]
    try:
        result = handler(arguments)
    except ToolError as err:
        return {"content": [{"type": "text", "text": str(err)}], "isError": True}
    except KeyError as err:
        return {
            "content": [{"type": "text", "text": "missing argument: %s" % err}],
            "isError": True,
        }
    except Exception as err:  # noqa: BLE001 — never kill the server on one tool.
        return {
            "content": [{"type": "text", "text": "tool failed: %s" % err}],
            "isError": True,
        }

    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _handle(message):
    """Dispatch one JSON-RPC message.

    Args:
        message: Decoded JSON-RPC request, notification or response.

    Returns:
        The response dict, or ``None`` for notifications (no reply allowed).
    """
    method = message.get("method") or ""
    msg_id = message.get("id")
    params = message.get("params")

    # Notifications carry no id and must not be answered.
    if msg_id is None and method:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": _tools_list()}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": msg_id, "result": _tools_call(params)}
    if method in ("notifications/initialized", "initialized"):
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": "method not found: %s" % method},
    }


def _send(payload):
    """Write one JSON-RPC message to stdout and flush it.

    Args:
        payload: JSON-serializable response.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    """Serve JSON-RPC over stdin/stdout until EOF."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            })
            continue
        if not isinstance(message, dict):
            _send({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "invalid request"},
            })
            continue
        response = _handle(message)
        if response is not None:
            _send(response)


if __name__ == "__main__":
    main()
