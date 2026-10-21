"""llm.py — OpenAI-compatible DeepSeek client using urllib only.

Implements the minimum needed to talk to the DeepSeek API
(``/chat/completions``) with no external dependencies. Supports JSON mode
through the API's ``response_format`` parameter.

Public functions:
    chat(messages, temperature, max_tokens, json_mode) -> str
    complete_json(prompt, max_tokens) -> dict
"""

import json
import re
import urllib.error
import urllib.request

from config import load_config


class LLMError(Exception):
    """LLM layer error carrying failure context (HTTP or parsing)."""


def _construir_cliente():
    """Load config and return (base_url, api_key, model).

    Loaded on every call so .env changes apply without restarting the
    process. Raises LLMError when no API key is configured.
    """
    cfg = load_config()
    api_key = cfg.get("DEEPSEEK_API_KEY")
    base_url = (cfg.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
    modelo = cfg.get("DEEPSEEK_MODEL") or "deepseek-flash"
    if not api_key:
        raise LLMError("Falta DEEPSEEK_API_KEY en la configuración (.env).")
    return base_url, api_key, modelo


def chat(messages, temperature=0.7, max_tokens=2000, json_mode=False):
    """Send a message block to DeepSeek and return the response text.

    Args:
        messages: list of dicts in OpenAI format
                  (``{"role": "user", "content": "..."}``).
        temperature: sampling temperature (0.0 to 2.0).
        max_tokens: response token limit.
        json_mode: when True, ask the API to answer as JSON
                   (``response_format={"type": "json_object"}``).

    Returns:
        The ``content`` of the first ``choice`` as a string.

    Raises:
        LLMError with context (HTTP code and error body) when the call fails
        or the response has an unexpected shape.
    """
    base_url, api_key, modelo = _construir_cliente()

    payload = {
        "model": modelo,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    cuerpo = json.dumps(payload).encode("utf-8")
    url = base_url + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=cuerpo,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            datos = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detalle = err.read().decode("utf-8", errors="replace")
        raise LLMError(f"HTTP {err.code} en {url}: {detalle[:500]}") from err
    except urllib.error.URLError as err:
        raise LLMError(f"Error de red al llamar a {url}: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise LLMError(f"Respuesta no-JSON de la API ({url}): {err}") from err

    try:
        eleccion = datos["choices"][0]
        mensaje = eleccion["message"]
        contenido = mensaje.get("content")
        motivo_fin = eleccion.get("finish_reason")
    except (KeyError, IndexError, TypeError) as err:
        raise LLMError(f"Respuesta de API sin 'choices[0].message.content': {datos}") from err

    # Reasoning models (deepseek-flash) emit 'reasoning_content' first and
    # only then 'content'. If max_tokens runs out during reasoning the API
    # returns empty content with finish_reason='length'. Fail here with a
    # clear message instead of handing "" to the caller.
    if not (contenido or "").strip():
        detalle = f"finish_reason={motivo_fin}, max_tokens={max_tokens}"
        if mensaje.get("reasoning_content"):
            detalle += " y tokens consumidos en reasoning_content"
        if motivo_fin == "length":
            detalle += (
                ". Aumenta max_tokens (los modelos de razonamiento gastan parte "
                "del presupuesto antes de responder)"
            )
        raise LLMError(f"DeepSeek devolvió contenido vacío ({detalle}).")

    return contenido


def _limpiar_json(texto):
    """Return the JSON object in the text, tolerating model decoration.

    Strips markdown fences (`` ```json ... ``` ``) and then extracts from the
    first ``{`` to the last ``}``, tolerating stray prose around the JSON
    (the "Extra data" parse error). Without braces the text is returned
    unchanged.
    """
    texto = texto.strip()
    # Case: ```json {...} ``` or ``` {...} ```
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", texto, flags=re.DOTALL)
    if match:
        texto = match.group(1).strip()
    # Extract the JSON object: first '{' to last '}'.
    inicio = texto.find("{")
    fin = texto.rfind("}")
    if inicio != -1 and fin > inicio:
        texto = texto[inicio : fin + 1]
    return texto


def complete_json(prompt, max_tokens=2000):
    """Call ``chat`` in JSON mode and return the parsed result.

    Args:
        prompt: user message text.
        max_tokens: response token limit.

    Returns:
        dict/list from ``json.loads`` over the response.

    Raises:
        LLMError if the API fails or the content is not valid JSON.
    """
    mensajes = [
        {
            "role": "system",
            "content": "Eres un asistente que responde únicamente con JSON válido.",
        },
        {"role": "user", "content": prompt},
    ]
    contenido = chat(mensajes, temperature=0.0, max_tokens=max_tokens, json_mode=True)
    try:
        return json.loads(_limpiar_json(contenido))
    except json.JSONDecodeError as err:
        raise LLMError(
            f"No se pudo parsear JSON de la respuesta del modelo: {err}\n"
            f"Contenido recibido: {contenido[:500]}"
        ) from err


if __name__ == "__main__":
    # Minimal self-check with a real call (never prints the API key).
    respuesta = chat(
        [{"role": "user", "content": "responde exactamente: ok"}],
        max_tokens=10,
    )
    print("Respuesta:", respuesta)
