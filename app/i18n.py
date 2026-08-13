"""Interface language. Portuguese by default, English by choice.

The source strings stay English — they're the msgid, the same way gettext works
— and `CATALOG` holds the Portuguese. A string with no translation falls back to
the English source, so a missed one shows up as English text rather than a
crash or an empty label.

The active locale lives in a ContextVar rather than being threaded through every
call. User-facing text is produced in templates, in route handlers and in
app/charts.py, and passing a locale to all three would mean touching every
signature between here and there for a value that is per-request anyway.
"""

from contextvars import ContextVar

DEFAULT = "pt"
LOCALES = ("pt", "en")
LOCALE_NAMES = {"pt": "Português", "en": "English"}

_current: ContextVar[str] = ContextVar("locale", default=DEFAULT)


def set_locale(code: str | None) -> str:
    """Set the locale for this request. Unknown codes fall back to the default."""
    code = (code or "").lower()
    if code not in LOCALES:
        code = DEFAULT
    _current.set(code)
    return code


def get_locale() -> str:
    return _current.get()


def t(text: str, **kwargs) -> str:
    """Translate `text` into the active locale.

    `kwargs` are substituted with str.format, so a translation can reorder the
    placeholders — which Portuguese frequently needs.
    """
    translated = CATALOG.get(_current.get(), {}).get(text, text)
    if kwargs:
        try:
            return translated.format(**kwargs)
        except (KeyError, IndexError):
            return translated
    return translated


# ── pt-BR ──────────────────────────────────────────────────────────────────
#
# Keyed by the English source string. Terms kept deliberately consistent:
#   chart -> gráfico | dashboard -> painel | report -> relatório
#   dataset -> conjunto de dados | query -> consulta | role -> perfil
#   filter -> filtro | folder -> pasta | tab -> aba
CATALOG: dict[str, dict[str, str]] = {
    "pt": {
        # ── navigation + shell ──
        "Query": "Consulta",
        "Reports": "Relatórios",
        "Dashboards": "Painéis",
        "Charts": "Gráficos",
        "Datasets": "Conjuntos de dados",
        "Administration": "Administração",
        "Report Hub": "Central de Relatórios",
        "Signed in as {user}": "Conectado como {user}",
        "Log out": "Sair",
        "Log in": "Entrar",
        "Sign in": "Entrar",
        "Username": "Usuário",
        "Password": "Senha",
        "Enter your credentials to access the hub.": "Informe suas credenciais para acessar a central.",
        "Continue with V360 SSO": "Continuar com o SSO V360",
        "Language": "Idioma",
        # ── common actions ──
        "Open": "Abrir",
        "Edit": "Editar",
        "Delete": "Excluir",
        "Save": "Salvar",
        "Add": "Adicionar",
        "Create": "Criar",
        "Run": "Executar",
        "Apply": "Aplicar",
        "Clear": "Limpar",
        "Cancel": "Cancelar",
        "Done": "Concluir",
        "Preview": "Pré-visualizar",
        "Back": "Voltar",
        "select all": "selecionar todos",
        "clear": "limpar",
        "All": "Todos",
        "Title": "Título",
        "Description": "Descrição",
        "Database": "Banco de dados",
        "rows": "linhas",
        "row(s) fetched": "linha(s) obtida(s)",
        "no options": "sem opções",
        # ── charts ──
        "New chart": "Novo gráfico",
        "{n} saved": "{n} salvos",
        "Chart type": "Tipo de gráfico",
        "X axis (labels)": "Eixo X (rótulos)",
        "Measures (numeric columns)": "Medidas (colunas numéricas)",
        "Which column holds the number": "Qual coluna contém o número",
        "Caption (optional)": "Legenda (opcional)",
        "Configure": "Configurar",
        "Save chart": "Salvar gráfico",
        "Open chart": "Abrir gráfico",
        "Edit chart": "Editar gráfico",
        "Show SQL": "Ver SQL",
        "Hide SQL": "Ocultar SQL",
        "Open SQL in Query": "Abrir SQL na Consulta",
        "Chart actions": "Ações do gráfico",
        "Data": "Dados",
        "SQL": "SQL",
        "unfiltered": "sem filtro",
        "Bar": "Barras",
        "Line": "Linha",
        "Area": "Área",
        "Pie": "Pizza",
        "Table": "Tabela",
        "Big number": "Número",
        "Compare magnitude across categories": "Compara grandezas entre categorias",
        "Change over time": "Variação ao longo do tempo",
        "Change over time, emphasising volume": "Variação ao longo do tempo, destacando o volume",
        "Parts of one whole — only for a single measure": "Partes de um todo — apenas para uma única medida",
        "The rows as they come back — no aggregation applied here": "As linhas como vêm — sem agregação aqui",
        "One value, large — a KPI rather than a trend": "Um único valor, grande — um indicador, não uma tendência",
        "No charts yet.": "Nenhum gráfico ainda.",
        "Create one": "Crie um",
        "Changes preview instantly. Run again after editing the SQL.": "A pré-visualização é imediata. Execute novamente após editar o SQL.",
        "No numeric columns in this result — a chart needs at least one.": "Nenhuma coluna numérica neste resultado — um gráfico precisa de ao menos uma.",
        # ── dashboards ──
        "New dashboard": "Novo painel",
        "{n} chart": "{n} gráfico",
        "{n} charts": "{n} gráficos",
        "Add a chart": "Adicionar um gráfico",
        "Width": "Largura",
        "New tab — e.g. Operação": "Nova aba — ex.: Operação",
        "Add tab": "Adicionar aba",
        "Heading or note between charts": "Título ou nota entre os gráficos",
        "Add text": "Adicionar texto",
        "— no tab —": "— sem aba —",
        "Which tab": "Qual aba",
        "Tile width": "Largura do bloco",
        "Remove from dashboard": "Remover do painel",
        "Remove": "Remover",
        "Drag to move": "Arraste para mover",
        "Drag to resize": "Arraste para redimensionar",
        "Nothing on this tab yet.": "Nada nesta aba ainda.",
        "This dashboard is empty.": "Este painel está vazio.",
        "Nothing on this dashboard yet. Add a chart above.": "Nada neste painel ainda. Adicione um gráfico acima.",
        "No dashboards yet. Name one above, then add charts to it.": "Nenhum painel ainda. Dê um nome acima e depois adicione gráficos.",
        "Name a new dashboard…": "Nomeie um novo painel…",
        "This chart's query failed.": "A consulta deste gráfico falhou.",
        "Could not load this chart.": "Não foi possível carregar este gráfico.",
        "editing": "editando",
        "Unsaved changes": "Alterações não salvas",
        "Layout saved": "Layout salvo",
        "Could not save the layout": "Não foi possível salvar o layout",
        # ── filters ──
        "Filters": "Filtros",
        "Add filter": "Adicionar filtro",
        "Delete this filter": "Excluir este filtro",
        "Pick from a list": "Escolher de uma lista",
        "Date range": "Período",
        "Free text": "Texto livre",
        "Values come from a query you supply": "Os valores vêm de uma consulta que você define",
        "A from/to pair on a date column": "Um par de/até sobre uma coluna de data",
        "Matches with ILIKE %value%": "Compara com ILIKE %valor%",
        "Label shown to people": "Rótulo exibido às pessoas",
        "column to filter on": "coluna a filtrar",
        "default (optional)": "padrão (opcional)",
        "all charts": "todos os gráficos",
        "{n} chart(s)": "{n} gráfico(s)",
        # ── reports ──
        "New report": "Novo relatório",
        "{n} available": "{n} disponíveis",
        "in git": "no git",
        "from a SQL query": "a partir de uma consulta SQL",
        "No reports you can reach.": "Nenhum relatório disponível para você.",
        "must return rows — exports run this as-is": "deve retornar linhas — a exportação executa isto como está",
        "Save report": "Salvar relatório",
        "What it contains, and its grain (optional)": "O que contém, e em qual granularidade (opcional)",
        # ── query console ──
        "Run a query": "Executar uma consulta",
        "stopped at the {limit}-row limit; raise it or add your own": "parou no limite de {limit} linhas; aumente-o ou use seu próprio",
        "Page {page} of {pages}": "Página {page} de {pages}",
        "Prev": "Anterior",
        "Next": "Próxima",
        # ── datasets ──
        "All datasets": "Todos os conjuntos",
        "Ungrouped": "Sem grupo",
        "No description yet": "Sem descrição ainda",
        "Columns": "Colunas",
        "Example queries": "Consultas de exemplo",
        "Preview rows": "Amostra de linhas",
        # ── admin ──
        "Users": "Usuários",
        "Roles & permissions": "Perfis e permissões",
        "Folders": "Pastas",
        "New role name…": "Nome do novo perfil…",
        "Create role": "Criar perfil",
        "What it's for (optional)": "Para que serve (opcional)",
        "given to new users": "atribuído a novos usuários",
        "Give to new users": "Atribuir a novos usuários",
        "Stop giving to new users": "Não atribuir a novos usuários",
        "everything": "tudo",
        "{n} member": "{n} membro",
        "{n} members": "{n} membros",
        "Delete role": "Excluir perfil",
        "Databases": "Bancos de dados",
        "Features": "Funcionalidades",
        "admin": "administrador",
        "active": "ativo",
        "inactive": "inativo",
        "never": "nunca",
        "Last seen": "Último acesso",
        "Roles": "Perfis",
        # ── errors + states ──
        "You don't have access to that.": "Você não tem acesso a isso.",
        "You don't have access to that chart.": "Você não tem acesso a esse gráfico.",
        "You don't have access to that dashboard.": "Você não tem acesso a esse painel.",
        "You don't have access to that report.": "Você não tem acesso a esse relatório.",
        "You don't have access to the query console.": "Você não tem acesso ao console de consultas.",
        "You don't have access to the chart builder.": "Você não tem acesso ao construtor de gráficos.",
        "You don't have access to the dashboard builder.": "Você não tem acesso ao construtor de painéis.",
        "You don't have access to the dataset catalog.": "Você não tem acesso ao catálogo de dados.",
        "You don't have access to create reports.": "Você não tem acesso para criar relatórios.",
        "You don't have access to edit that dashboard.": "Você não tem acesso para editar esse painel.",
        "You don't have access to {name!r}.": "Você não tem acesso a {name!r}.",
        "Not found": "Não encontrado",
        "The app database is not configured.": "O banco de dados da aplicação não está configurado.",
        "The query returned no rows.": "A consulta não retornou linhas.",
        "Pick the column holding the number.": "Escolha a coluna que contém o número.",
        "Pick at least one numeric column to plot.": "Escolha ao menos uma coluna numérica para plotar.",
        "A pie shows one measure — charting only the first.": "Um gráfico de pizza mostra uma medida — exibindo apenas a primeira.",
    }
}
