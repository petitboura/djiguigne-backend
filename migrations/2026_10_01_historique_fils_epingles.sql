-- Migration : historique du chat, fils epingles / renommes + liste paginee.
-- Demande Bourama (01/10/2026) : plein ecran, epingler, renommer, supprimer,
-- et ne plus tout charger d'un coup (defilement progressif).
-- Additive uniquement : rien n'est modifie ni supprime dans les tables existantes.
-- A executer en staging d'abord, puis en prod apres validation.

-- 1. Metadonnees d'un fil. historique_conversations ne garde que des
--    messages (aucune ligne par fil), donc l'etat "epingle" et le titre
--    choisi par l'utilisateur vivent ici. `cle` = conversation_id en texte,
--    ou 'legacy' pour le fil des lignes d'avant l'historique par conversation
--    (conversation_id NULL en base, meme convention que api/historique.py).
create table if not exists historique_fils_meta (
    user_id uuid not null,
    agent_id text not null references agents(id) on delete cascade,
    cle text not null,
    epingle_le timestamptz,
    titre_perso text,
    updated_at timestamptz not null default now(),
    primary key (user_id, agent_id, cle)
);

alter table historique_fils_meta enable row level security;
-- Pas de policy : seul le backend (cle service) lit et ecrit, comme
-- historique_conversations.

create index if not exists idx_historique_fils_meta_epingles
    on historique_fils_meta (user_id, agent_id, epingle_le desc)
    where epingle_le is not null;

-- 2. Index pour regrouper les messages par fil sans relire toute la table.
create index if not exists idx_historique_conversations_fils
    on historique_conversations (user_id, agent_id, conversation_id, created_at);

-- 3. Liste paginee des fils d'un utilisateur pour un agent.
--    p_epingles = true  : tous les fils epingles (peu nombreux), du plus
--                         recemment epingle au plus ancien, sans pagination.
--    p_epingles = false : les fils non epingles, du plus recemment actif au
--                         plus ancien, par paquets de p_limite. La page
--                         suivante se demande avec (p_avant_activite,
--                         p_avant_cle) = derniere ligne de la page precedente.
--    Le premier message de l'utilisateur n'est lu QUE pour les fils
--    renvoyes (jointure laterale apres le tri et la limite).
create or replace function lister_fils_historique(
    p_user uuid,
    p_agent text,
    p_epingles boolean,
    p_limite integer default 20,
    p_avant_activite timestamptz default null,
    p_avant_cle text default null
)
returns table (
    conversation_id uuid,
    cle text,
    derniere_activite timestamptz,
    epingle_le timestamptz,
    titre_perso text,
    premier_message text
)
language sql
stable
as $$
    with fils as (
        select
            h.conversation_id,
            coalesce(h.conversation_id::text, 'legacy') as cle,
            max(h.created_at) as derniere_activite
        from historique_conversations h
        where h.user_id = p_user and h.agent_id = p_agent
        group by h.conversation_id
    ),
    choisis as (
        select
            f.conversation_id,
            f.cle,
            f.derniere_activite,
            m.epingle_le,
            m.titre_perso
        from fils f
        left join historique_fils_meta m
            on m.user_id = p_user and m.agent_id = p_agent and m.cle = f.cle
        where
            case
                when p_epingles then m.epingle_le is not null
                else m.epingle_le is null
                     and (
                        p_avant_activite is null
                        or (f.derniere_activite, f.cle) < (p_avant_activite, p_avant_cle)
                     )
            end
        order by
            case when p_epingles then m.epingle_le end desc nulls last,
            f.derniere_activite desc,
            f.cle desc
        limit case when p_epingles then 1000 else greatest(p_limite, 1) end
    )
    select
        c.conversation_id,
        c.cle,
        c.derniere_activite,
        c.epingle_le,
        c.titre_perso,
        pm.content as premier_message
    from choisis c
    left join lateral (
        select left(h2.content, 200) as content
        from historique_conversations h2
        where h2.user_id = p_user
          and h2.agent_id = p_agent
          and h2.role = 'user'
          and h2.conversation_id is not distinct from c.conversation_id
        order by h2.created_at
        limit 1
    ) pm on true
    order by
        case when p_epingles then c.epingle_le end desc nulls last,
        c.derniere_activite desc,
        c.cle desc;
$$;
