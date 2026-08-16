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

AUTHENTIFICATION (résolue -- voir Partie 2 du chantier connecteur externe) :
Ce serveur agit comme "Resource Server" au sens MCP/OAuth 2.1 (RFC 9728),
et délègue tout le travail de "Authorization Server" à Supabase Auth
(fonctionnalité "OAuth 2.1 Server", à activer manuellement une fois dans
le tableau de bord Supabase -- Authentication > OAuth Server -- ce fichier
ne peut pas l'activer lui-même). Concrètement :
- Un client externe (Claude) découvre automatiquement, via les métadonnées
  exposées par ce serveur (RFC 9728, générées automatiquement par la
  librairie mcp à partir du paramètre `auth` ci-dessous), qu'il doit
  s'authentifier auprès de Supabase (`issuer_url`), pas auprès de nous.
- Supabase gère l'écran de consentement (voir classgpt-frontend
  app/oauth/consent/page.tsx), l'émission des jetons, leur rafraîchissement.
- Ce fichier ne fait que VÉRIFIER le jeton reçu à chaque appel d'outil
  (`_VerificateurJetonSupabase` ci-dessous), exactement de la même façon
  que `api/auth.py:utilisateur_courant` vérifie déjà un jeton de session
  classique -- volontairement dupliqué ici plutôt qu'importé, même
  convention que mcp_programme et mcp_generation (voir leurs en-têtes).
- Comme pour mcp_programme, l'identité de l'appelant n'est JAMAIS un
  paramètre que le modèle choisit : elle vient uniquement du jeton vérifié
  par la librairie MCP elle-même (ctx.request_context, via
  `_user_id_depuis_contexte` ci-dessous), avant même que l'outil ne
  s'exécute.

À FAIRE PAR LA SUITE, dans des chantiers séparés :
- Outils métier réels (bibliothèque, mémoire, comportements, etc.) : à
  ajouter ici même, un par un, chacun sous la forme d'une simple fonction
  décorée @mcp_public.tool(), en dupliquant la logique nécessaire depuis
  les fichiers api/*.py correspondants plutôt qu'en les important
  directement -- même convention que mcp_programme et mcp_generation
  (voir leurs en-têtes). Chaque outil qui touche à des données
  utilisateur doit récupérer l'id via `_user_id_depuis_contexte(ctx)`,
  jamais via un paramètre.
- Identité visuelle (title, description, icons, website_url) : à
  compléter sur l'objet FastMCP ci-dessous une fois l'icône Clovis prête.

RÉGLAGES À FAIRE UNE FOIS, HORS CODE, DANS LE TABLEAU DE BORD SUPABASE
(Authentication > OAuth Server) -- personne d'autre que Bourama ne peut
les activer, aucun outil disponible ici ne le permet :
1. Activer "OAuth 2.1 Server" (bêta, gratuit).
2. Activer "Dynamic Client Registration" (pour que Claude s'enregistre
   automatiquement, sans client_id créé à la main).
3. Renseigner le chemin d'autorisation "/oauth/consent" (combiné à la
   Site URL déjà configurée pour classgpt-frontend).
Tant que ce n'est pas fait, Supabase ne sert pas les points de découverte
OAuth et un client externe ne pourra pas s'authentifier -- le squelette
ci-dessous est prêt à fonctionner dès que ces 3 réglages sont faits, sans
modification de code supplémentaire.

Nom technique interne "clovis_public" : jamais vu par l'utilisateur (ce
n'est pas le `title`, qui lui est affiché). Attention -- ce serveur étant
public et destiné à représenter CLOVIS auprès de l'utilisateur final, ne
jamais y faire apparaître le mot "Djiguignè" dans un texte visible
(title, description, url) : Clovis est un produit qui ne doit jamais
laisser transparaître son lien avec l'écosystème Djiguignè (voir
README de classgpt-frontend).
"""

import asyncio
import logging
import os

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import Context, MCPServer as FastMCP
from supabase import create_client

logging.basicConfig(level=logging.INFO)


def get_secret(key: str) -> str | None:
    return os.environ.get(key)


# Même paire de variables d'environnement que api/auth.py -- un seul projet
# Supabase, pas de config séparée pour ce serveur.
SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_SECRET = get_secret("SUPABASE_SECRET")

if not SUPABASE_URL or not SUPABASE_SECRET:
    logging.error(
        "SUPABASE_URL ou SUPABASE_SECRET manquant : la verification des "
        "jetons OAuth du serveur MCP public sera toujours en echec."
    )

_supabase = create_client(SUPABASE_URL, SUPABASE_SECRET)

# URL publique de production du backend (Railway, service clovis-backend).
# Sert de "resource_server_url" -- l'identifiant de CE serveur pour les
# metadonnees RFC 9728, distinct de l'URL de Supabase (l'issuer).
URL_RESOURCE_SERVER_PUBLIC = get_secret("URL_RESOURCE_SERVER_PUBLIC") or (
    "https://clovis-backend-production.up.railway.app/mcp/public"
)


class _VerificateurJetonSupabase(TokenVerifier):
    """Vérifie un jeton d'accès OAuth émis par Supabase Auth.

    Les jetons OAuth émis par la fonctionnalité "OAuth 2.1 Server" de
    Supabase sont des JWT Supabase standards (mêmes claims user_id/role
    qu'un jeton de session classique, voir doc Supabase "OAuth 2.1
    Flows") -- `supabase.auth.get_user(token)` les valide donc exactement
    comme le fait déjà `api/auth.py:utilisateur_courant` pour une session
    classique. `get_user` est un appel bloquant (réseau) : on le sort de
    la boucle asyncio via `asyncio.to_thread` pour ne pas geler le serveur
    pendant la vérification.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            reponse = await asyncio.to_thread(_supabase.auth.get_user, token)
        except Exception as e:
            logging.error(f"ERREUR verification jeton OAuth public : {e}")
            return None

        if not reponse or not reponse.user:
            return None

        return AccessToken(
            token=token,
            client_id=reponse.user.id,
            scopes=[],
            subject=reponse.user.id,
        )


def _user_id_depuis_contexte(ctx: Context) -> str | None:
    """Id utilisateur du jeton déjà vérifié par la librairie MCP pour cette
    requête -- jamais un paramètre fourni par le modèle (même principe que
    core/serveur_mcp_programme.py:_user_id_depuis_contexte).
    """
    acces = ctx.request_context.request.auth
    if acces is None or not hasattr(acces, "subject"):
        return None
    return acces.subject


mcp_public = FastMCP(
    name="clovis_public",
    token_verifier=_VerificateurJetonSupabase(),
    auth=AuthSettings(
        issuer_url=SUPABASE_URL,
        resource_server_url=URL_RESOURCE_SERVER_PUBLIC,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    ),
)


@mcp_public.tool()
def ping() -> str:
    """Outil de test : confirme que le serveur MCP public de Clovis répond.

    Aucune donnée utilisateur -- sert uniquement à valider que la connexion
    (client MCP externe -> ce serveur) fonctionne de bout en bout, avant
    authentification. Reste utile après la Partie 2 comme sonde de santé.
    """
    return "pong depuis Clovis"


@mcp_public.tool()
def qui_suis_je(ctx: Context) -> str:
    """Outil de test AUTHENTIFIÉ : confirme que la vérification du jeton
    OAuth fonctionne de bout en bout (Claude connecté -> Supabase ->
    jeton vérifié ici -> identité récupérée), sans toucher à aucune
    donnée métier. À retirer ou remplacer une fois de vrais outils
    authentifiés ajoutés (Partie 3) : il ne sert que de preuve de
    fonctionnement pour la Partie 2.
    """
    user_id = _user_id_depuis_contexte(ctx)
    if not user_id:
        return "Aucune identité vérifiée -- authentification manquante ou invalide."
    return f"Authentifié avec succès sur Clovis (id utilisateur : {user_id})."
