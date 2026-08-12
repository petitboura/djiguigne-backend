-- Système de plugins (export/import de programme), lot 3/5 (12/08/2026).
-- Voir chantier-programme-etudiant.md, partie 1, "Système de plugins", et
-- api/plugins_programme.py pour la logique. Dépend de `programmes`
-- (lot 1), déjà présente en base au moment de cette migration -- pas
-- recréée ici, uniquement référencée en foreign key.

create table if not exists plugins_programme (
  id uuid primary key default gen_random_uuid(),
  programme_source_id uuid not null references programmes(id) on delete cascade,
  auteur_id uuid not null references auth.users(id) on delete cascade,
  niveau text not null,        -- dénormalisé depuis programmes.niveau, pour la recherche
  nom text not null,
  gratuit boolean not null default true,  -- gratuit au lancement, payant possible plus tard (voir doc source)
  telechargements_count int not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists idx_plugins_niveau on plugins_programme(niveau);
create index if not exists idx_plugins_auteur on plugins_programme(auteur_id);

create table if not exists plugin_telechargements (
  id uuid primary key default gen_random_uuid(),
  plugin_id uuid not null references plugins_programme(id) on delete cascade,
  telecharge_par uuid not null references auth.users(id) on delete cascade,
  programme_copie_id uuid references programmes(id),  -- le nouveau programme créé chez le téléchargeur
  created_at timestamptz not null default now(),
  unique (plugin_id, telecharge_par)  -- un téléchargement compte une fois par utilisateur, pas à chaque clic
);
create index if not exists idx_plugin_telechargements_plugin on plugin_telechargements(plugin_id);
