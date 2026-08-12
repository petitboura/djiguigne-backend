-- Lot 2/5 -- contenu pratique (documents/exercices/examens) rattaché au
-- squelette classe->matière->chapitre (créé par le lot 1, tables
-- `programmes`/`matieres`/`chapitres` -- NON recréées ici) + classement
-- transversal (semestre/année/section libre) superposé à ce squelette.
-- Voir chantier-programme-etudiant.md (partie 1) pour le contexte complet.

-- Documents rattachés à un chapitre (cours, ressources).
create table if not exists documents_programme (
  id uuid primary key default gen_random_uuid(),
  chapitre_id uuid not null references chapitres(id) on delete cascade,
  titre text not null,
  url_ou_contenu text not null,  -- URL (fichier déjà stocké ailleurs / lien) ou texte direct
  created_at timestamptz not null default now()
);
create index if not exists idx_documents_programme_chapitre on documents_programme(chapitre_id);

-- Exercices/problèmes : rattachés à UN SEUL chapitre.
create table if not exists exercices_programme (
  id uuid primary key default gen_random_uuid(),
  chapitre_id uuid not null references chapitres(id) on delete cascade,
  enonce text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_exercices_programme_chapitre on exercices_programme(chapitre_id);

-- Examens/devoirs/problèmes composites : peuvent couvrir PLUSIEURS chapitres.
create table if not exists examens_programme (
  id uuid primary key default gen_random_uuid(),
  proprietaire_id uuid not null references auth.users(id) on delete cascade,
  titre text not null,
  type text not null check (type in ('examen', 'devoir', 'probleme_composite')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Jointure many-to-many examens <-> chapitres.
create table if not exists examen_chapitres (
  examen_id uuid not null references examens_programme(id) on delete cascade,
  chapitre_id uuid not null references chapitres(id) on delete cascade,
  primary key (examen_id, chapitre_id)
);

-- Classement transversal (semestre / année / section libre), superposé au
-- squelette officiel -- peut s'appliquer à une matière, un chapitre, un
-- document, un exercice ou un examen (voir doc source, partie 1).
create table if not exists classements_transversaux (
  id uuid primary key default gen_random_uuid(),
  proprietaire_id uuid not null references auth.users(id) on delete cascade,
  type text not null check (type in ('semestre', 'annee', 'section')),
  label text not null,  -- ex: "Semestre 1", "2026-2027", "Révisions bac"
  created_at timestamptz not null default now()
);

-- Référence polymorphe volontaire (pas de vraie FK SQL possible sur
-- plusieurs tables à la fois -- confirmé avec Bourama le 12/08 : approche
-- gardée telle quelle, cohérente avec le reste du projet qui ne s'appuie
-- jamais sur la base pour l'intégrité/la sécurité (pas de RLS, tout est
-- vérifié côté API -- voir core/erreurs.py, api/roles.py). Le nettoyage
-- des lignes orphelines (quand un document/exercice/examen est supprimé)
-- est fait explicitement côté API, voir api/contenu_programme.py.
create table if not exists classement_transversal_items (
  id uuid primary key default gen_random_uuid(),
  classement_id uuid not null references classements_transversaux(id) on delete cascade,
  cible_type text not null check (cible_type in ('matiere', 'chapitre', 'document', 'exercice', 'examen')),
  cible_id uuid not null,
  created_at timestamptz not null default now()
);
create index if not exists idx_classement_items_classement on classement_transversal_items(classement_id);
create index if not exists idx_classement_items_cible on classement_transversal_items(cible_type, cible_id);
