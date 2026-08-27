"""The app's AI features, powered by the Anthropic API over httpx.

Two grounded uses, both off unless ANTHROPIC_API_KEY is set:

  * generate_dataset_doc — a draft title, description and example queries for a
    dataset, from its real columns and (for a view/matview) its definition.
  * answer_data_question — a question in Portuguese turned into an explanation,
    the datasets to use, and a query, grounded in a catalog of real tables.

Everything is grounded: the model is given the actual schema and told to use
only what it is shown, so it can't invent a table or column. The caller does the
retrieval (which datasets to put in front of it); this module only talks to the
API and shapes the request and reply.
"""
import json
import logging

import httpx

from .config import get_settings

logger = logging.getLogger("report_hub")


class AIError(Exception):
    """A user-showable AI failure — missing key, API error, or a bad reply."""


def enabled() -> bool:
    return get_settings().ai_enabled


def _complete(system: str, user: str, max_tokens: int | None = None, temperature: float = 0.2) -> str:
    s = get_settings()
    if not s.ai_enabled:
        raise AIError("A IA não está configurada (defina a chave de API).")
    mt = max_tokens or s.ai_max_tokens
    openai_style = s.ai_provider == "openai"
    try:
        if openai_style:
            # Any OpenAI-compatible API: Groq, Gemini, OpenRouter, etc.
            resp = httpx.post(
                s.ai_base_url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": "Bearer " + s.ai_api_key,
                    "content-type": "application/json",
                },
                json={
                    "model": s.ai_model,
                    "max_tokens": mt,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=90,
            )
        else:
            resp = httpx.post(
                s.anthropic_base_url.rstrip("/") + "/v1/messages",
                headers={
                    "x-api-key": s.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": s.anthropic_model,
                    "max_tokens": mt,
                    "temperature": temperature,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=90,
            )
    except httpx.HTTPError as exc:
        logger.exception("AI request failed")
        raise AIError(f"Não foi possível chamar a IA: {exc}") from exc
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        raise AIError(f"A IA respondeu {resp.status_code}: {detail}")
    data = resp.json()
    if openai_style:
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError("A IA devolveu uma resposta inesperada.") from exc
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()


def _call_agent(agent_key: str, input_text: str) -> str:
    """Call a V360 Assis agent. The prompt lives in the agent (no-code); we send
    the input and get back its `output` — a JSON string for a structured agent.
    Contract mirrors Vportal::AssisService#call_agent."""
    s = get_settings()
    if not agent_key:
        raise AIError("Agente do Assis não configurado (defina o agent_key).")
    db = s.assis_client_name or s.datasets_database or ""
    body = {
        "input": input_text,
        "user": {
            "email": s.assis_user_email,
            "first_name": "Report Hub",
            "last_name": "(API)",
            "role": "v360",
        },
        "portal": {"database": db},
        "agent_source": {"database": db, "client_name": s.assis_client_name, "url": ""},
    }
    try:
        resp = httpx.post(
            s.assis_base_url.rstrip("/") + f"/v1/agents/{agent_key}/",
            headers={
                "Authorization": "Bearer " + s.assis_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
            timeout=120,
        )
    except httpx.HTTPError as exc:
        logger.exception("Assis agent request failed")
        raise AIError(f"Não foi possível chamar o Assis: {exc}") from exc
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error") or resp.text[:200]
        except Exception:
            detail = resp.text[:200]
        raise AIError(f"O Assis respondeu {resp.status_code}: {detail}")
    try:
        return str(resp.json().get("output") or "")
    except Exception as exc:
        raise AIError("O Assis devolveu uma resposta inesperada.") from exc


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a reply, tolerating any prose around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AIError("A IA não devolveu um resultado válido.")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AIError("A IA devolveu um resultado que não deu para ler.") from exc


def _columns_block(columns: list) -> str:
    return "\n".join(f"  - {n} ({t})" for n, t in columns)


def generate_dataset_doc(name: str, kind: str, columns: list, definition: str = "") -> dict:
    """Draft {title, description, examples:[{title, sql}]} for one dataset."""
    system = (
        "Você documenta datasets (tabelas, views e materialized views) de um data "
        "warehouse PostgreSQL de uma empresa brasileira (V360). Escreva em português "
        "do Brasil. Fundamente-se APENAS nas colunas e na definição fornecidas — nunca "
        "invente colunas. As queries de exemplo devem ser PostgreSQL válido usando só as "
        "colunas reais, referenciando a tabela como public.\"<nome>\". "
        "Responda SOMENTE com um objeto JSON no formato: "
        '{"title": "...", "description": "...", '
        '"examples": [{"title": "...", "sql": "..."}]}. '
        "1 a 2 exemplos úteis (um agregado com GROUP BY, e/ou um recorte recente por data)."
    )
    user = f"Dataset: {name}\nTipo: {kind}\nColunas:\n{_columns_block(columns)}"
    if definition:
        user += f"\n\nDefinição (SELECT que a constrói):\n{definition[:3000]}"
    if get_settings().ai_provider == "assis":
        # The Assis agent owns the prompt; send the raw material as its input.
        payload = {"name": name, "kind": kind, "columns": columns, "definition": (definition or "")[:3000]}
        doc = _extract_json(_call_agent(get_settings().assis_doc_agent_key, json.dumps(payload, ensure_ascii=False)))
    else:
        doc = _extract_json(_complete(system, user, max_tokens=1500))
    # Normalise the shape the caller expects. `examples` may come as a list of
    # objects or, from a no-code text field, a JSON string — accept both.
    raw_examples = doc.get("examples") or []
    if isinstance(raw_examples, str):
        try:
            raw_examples = json.loads(raw_examples)
        except Exception:
            raw_examples = []
    examples = []
    for ex in raw_examples if isinstance(raw_examples, list) else []:
        if isinstance(ex, dict) and ex.get("sql"):
            examples.append({"title": str(ex.get("title") or "Exemplo"), "sql": str(ex["sql"])})
    return {
        "title": str(doc.get("title") or name),
        "description": str(doc.get("description") or "").strip(),
        "examples": examples,
    }


def answer_data_question(question: str, catalog: list) -> dict:
    """Turn a plain question into {explanation, datasets:[names], sql}, grounded
    in `catalog` — a list of {name, kind, description, columns:[(n,t)], example}.
    """
    lines = []
    for d in catalog:
        lines.append(f'### {d["name"]} ({d["kind"]})')
        if d.get("description"):
            lines.append(d["description"].strip())
        lines.append("Colunas: " + ", ".join(f"{n} {t}" for n, t in d.get("columns", [])[:40]))
        if d.get("example"):
            lines.append("Exemplo: " + " ".join(d["example"].split())[:300])
        lines.append("")
    context = "\n".join(lines)
    system = (
        "Você é um assistente de dados da V360. O usuário faz uma pergunta em português "
        "e você a traduz para a linguagem do data warehouse (PostgreSQL, schema analytics). "
        "Use SOMENTE as tabelas e colunas do catálogo fornecido — nunca invente tabela ou "
        "coluna. Se a pergunta não puder ser respondida com o catálogo, diga isso na "
        "explicação e deixe sql vazio. Referencie tabelas como public.\"<nome>\". "
        "Responda SOMENTE com JSON: "
        '{"explanation": "explicação em PT do caminho (tabelas, joins, filtros)", '
        '"datasets": ["nomes usados"], "sql": "a query PostgreSQL, ou string vazia"}.'
    )
    user = f"Catálogo disponível:\n\n{context}\n\nPergunta do usuário: {question}"
    if get_settings().ai_provider == "assis":
        # The Assis agent owns the prompt; hand it the catalog and the question.
        payload = {
            "pergunta": question,
            "catalogo": [
                {"name": d["name"], "kind": d["kind"], "description": d.get("description", ""),
                 "columns": d.get("columns", []), "example": d.get("example", "")}
                for d in catalog
            ],
        }
        ans = _extract_json(_call_agent(get_settings().assis_assistant_agent_key, json.dumps(payload, ensure_ascii=False)))
    else:
        ans = _extract_json(_complete(system, user, max_tokens=2000))
    # `datasets` may be a list or, from a no-code text field, comma-separated.
    ds = ans.get("datasets") or []
    if isinstance(ds, str):
        ds = ds.split(",")
    return {
        "explanation": str(ans.get("explanation") or "").strip(),
        "datasets": [str(x).strip() for x in ds if str(x).strip()],
        "sql": str(ans.get("sql") or "").strip(),
    }
