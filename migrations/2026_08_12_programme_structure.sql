-- Structure programme (classe -> matière -> chapitre), 2026-08-12, demande
-- Bourama -- chantier "programme adaptatif étudiant", lot 1/5.
--
-- Un programme (par classe/niveau d'étude) contient des matières, chaque
-- matière contient des chapitres. "Niveau" est un rattachement
-- organisationnel simple (pour organiser le contenu pour l'étudiant et
-- pour l'IA), pas une logique de droits d'accès.
--
-- Point de nommage à noter (pas un conflit technique, juste une précision
-- pour qui relit ce fichier plus tard) : ce dépôt contient déjà DEUX
-- autres notions distinctes de "matière", sans lien avec celle-ci :
--   1. `agents.matiere` (api/agents.py, MATIERES) -- catégorise un AGENT
--      entier parmi 10 matières fixes (une IA par matière sur la
--      plateforme). Rien à voir avec la table `matieres` ci-dessous.
--   2. `contenus_par_matiere` / `rattachements_par_matiere` (migration
--      2026_08_06) -- l'ancien système "code à débloquer" que CETTE
--      migration désactive fonctionnellement (voir plus bas), sans le
--      supprimer.
-- La table `matieres` créée ici est un TROISIÈME concept : une entité
-- structurée (classe -> matière -> chapitre), propre au nouveau chantier.

create table if not exists programmes (
  id uuid primary key default gen_random_uuid(),
  proprietaire_id uuid not null references auth.users(id) on delete cascade,
  niveau text not null,  -- ex: "3ème", "Terminale S" -- texte libre, pas de liste fermée
  nom text,              -- label optionnel donné par l'utilisateur
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_programmes_proprietaire on programmes(proprietaire_id);

create table if not exists matieres (
  id uuid primary key default gen_random_uuid(),
  programme_id uuid not null references programmes(id) on delete cascade,
  nom text not null,
  limites text,  -- description du cadre officiel (pour ne pas dépasser le programme, "hors programme")
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_matieres_programme on matieres(programme_id);

create table if not exists chapitres (
  id uuid primary key default gen_random_uuid(),
  matiere_id uuid not null references matieres(id) on delete cascade,
  nom text not null,
  ordre int not null default 0,
  limites text,  -- même logique que matieres.limites, au niveau chapitre
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_chapitres_matiere on chapitres(matiere_id);

-- Désactivation fonctionnelle de l'ancien système "matière" (code à
-- débloquer, voir migration 2026_08_06_contenu_dynamique_par_matiere.sql).
-- Décision Bourama (11/08) : remplacé par la structure ci-dessus, mais
-- gardé dans le code (tables, fichiers api/core) SANS suppression -- ce
-- flag est la seule chose qu'on touche ici. Comme
-- api/contenu_dynamique_matiere.py:lister_agents_contenu_dynamique filtre
-- déjà sur ce flag, l'entrée "Matières" disparaît d'elle-même côté
-- frontend une fois ce flag repassé à false -- aucune modification
-- frontend requise pour ce point précis.
update agents set contenu_dynamique_par_matiere = false where contenu_dynamique_par_matiere = true;
