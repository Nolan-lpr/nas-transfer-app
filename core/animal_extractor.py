"""Extraction des noms d'animaux à partir d'une arborescence de séquences.

Réplique creation_fichier_animaux.sh :
  - DICOM : on prend les `nb_separateur + 1` premiers segments séparés par '_'
  - Bruker : on prend les `nb_separateur + 3` premiers segments
  - On déduplique et on écrit la liste dans un .txt
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Correspond au "first" du script bash : DICOM commence au champ 1, Bruker au champ 3
FIRST_FIELD = {"dicom": 1, "bruker": 3}


@dataclass
class ExtractionResult:
    animals: list[str]
    inspected_folders: int
    skipped_folders: list[str]


def extract_animal_name(folder_name: str, source_type: str, nb_separateurs: int) -> str | None:
    """Reproduit la logique :
        nb_separateur_tot = nb_separateur + first
        name = cut -d'_' -f${first}-${nb_separateur_tot}
    Le `cut` bash sur les champs `first..nb_separateur_tot` est inclusif des deux côtés,
    donc on garde `(nb_separateur_tot - first + 1) = nb_separateur + 1` champs.
    """
    if source_type not in FIRST_FIELD:
        raise ValueError(f"Type inconnu : {source_type}")

    parts = folder_name.split("_")
    separateurs = folder_name.count("_")
    if separateurs < nb_separateurs:
        return None

    first = FIRST_FIELD[source_type]
    last = nb_separateurs + first  # inclusif
    # bash `cut -f1-N` indexe à partir de 1
    selected = parts[first - 1:last]
    if not selected:
        return None
    return "_".join(selected)


def extract_animals(source_dir: Path, source_type: str, nb_separateurs: int) -> ExtractionResult:
    """Parcourt les sous-dossiers de `source_dir`, extrait les noms uniques d'animaux."""
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Dossier source introuvable : {source_dir}")

    seen: set[str] = set()
    ordered: list[str] = []
    skipped: list[str] = []
    inspected = 0

    for entry in sorted(source_dir.iterdir()):
        if not entry.is_dir():
            continue
        inspected += 1
        name = extract_animal_name(entry.name, source_type, nb_separateurs)
        if name is None:
            skipped.append(entry.name)
            continue
        if name not in seen:
            seen.add(name)
            ordered.append(name)

    return ExtractionResult(animals=ordered, inspected_folders=inspected, skipped_folders=skipped)


def write_animals_file(animals: list[str], output_path: Path) -> Path:
    output_path = Path(output_path)
    if output_path.suffix != ".txt":
        output_path = output_path.with_suffix(".txt")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(animals) + ("\n" if animals else ""))
    return output_path
