"""
Serveur MCP PUBLIC de Clovis -- contrairement à core/serveur_mcp_generation.py,
core/serveur_mcp_github.py et core/serveur_mcp_programme.py (qui ne sont
joignables qu'en interne, via localhost, pour l'IA propre de la plateforme),
ce serveur est destiné à être appelé depuis l'EXTÉRIEUR, par un client MCP
tiers comme Claude (claude.ai, Claude Desktop, Claude Code), pour qu'un
utilisateur puisse retrouver les capacités de Clovis directement depuis son
propre client.

Monté de la même façon que les 3 serveurs internes (voir api/main.py :
import, session_manager.run() dans le lifespan, app.mount()), mais sur un
chemin séparé ("/mcp/public") pour bien distinguer ce qui est interne
(jamais authentifié depuis l'extérieur, appelé uniquement par notre propre
backend) de ce qui est public (appelé par un tiers, donc à authentifier).

ÉTAT ACTUEL (première étape du chantier connecteur externe) : ce fichier ne
contient volontairement AUCUN outil métier et AUCUNE authentification.
C'est un squelette minimal, avec un seul outil factice (`ping`) pour
valider que la connexion fonctionne de bout en bout (Claude peut se
connecter, lister les outils, en appeler un, recevoir une réponse).

À FAIRE PAR LA SUITE, dans des chantiers séparés :
- Authentification : aujourd'hui, contrairement à mcp_programme (qui lit
  l'identité de l'appelant depuis l'URL, elle-même construite par notre
  propre backend qui connaît déjà le user_id), ce serveur public n'a AUCUN
  moyen fiable de savoir qui l'appelle -- un client externe ne peut pas
  fournir un user_id de confiance. Ne pas ajouter d'outil qui touche à des
  données utilisateur tant que ce point n'est pas résolu.
- Outils métier réels (bibliothèque, mémoire, comportements, etc.) : à
  ajouter ici même, dans le registre TOOLS_PUBLIC ci-dessous, un par un,
  chacun sous la forme d'une simple fonction décorée @mcp_public.tool(),
  en dupliquant la logique nécessaire depuis les fichiers api/*.py
  correspondants plutôt qu'en les important directement -- même convention
  que mcp_programme et mcp_generation (voir leurs en-têtes).
- Identité visuelle (title, description, icons, website_url) : à
  compléter sur l'objet FastMCP ci-dessous une fois l'icône Clovis prête.

Nom technique interne "clovis_public" : jamais vu par l'utilisateur (ce
n'est pas le `title`, qui lui est affiché). Attention -- ce serveur étant
public et destiné à représenter CLOVIS auprès de l'utilisateur final, ne
jamais y faire apparaître le mot "Djiguignè" dans un texte visible
(title, description, url) : Clovis est un produit qui ne doit jamais
laisser transparaître son lien avec l'écosystème Djiguignè (voir
README de classgpt-frontend).
"""

import logging

from mcp.server.mcpserver import MCPServer as FastMCP

logging.basicConfig(level=logging.INFO)

mcp_public = FastMCP(name="clovis_public")


@mcp_public.tool()
def ping() -> str:
    """Outil de test : confirme que le serveur MCP public de Clovis répond.

    Aucune donnée utilisateur, aucune authentification -- sert uniquement à
    valider que la connexion (client MCP externe -> ce serveur) fonctionne
    de bout en bout avant d'ajouter de vrais outils.
    """
    return "pong depuis Clovis"
