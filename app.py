"""Transfert IRM → NAS — application Streamlit autonome (un seul fichier).

Cette application regroupe en une seule interface graphique les deux scripts
bash utilisés au CHR pour transférer les données IRM vers le NAS :

  - `creation_fichier_animaux.sh`  → extraction des noms d'animaux uniques
  - `creation_folder_group_dicom.sh` → copie des séquences DICOM vers le NAS
    dans l'arborescence `{NAS}/{StudyDate}_{sujet}/{Time}/IRM/dicom/{séquence}/`

Tout le code Python est volontairement regroupé dans ce fichier pour
faciliter la maintenance et la revue (un seul endroit à lire / patcher).

╔════════════════════════════════════════════════════════════════════════╗
║  SOMMAIRE                                                              ║
╠════════════════════════════════════════════════════════════════════════╣
║  1. IMPORTS                                                            ║
║  2. CONFIGURATION STREAMLIT & CONSTANTES                               ║
║  3. LECTURE DICOM         (remplace dcmftest/dcmdump du script bash)   ║
║  4. EXTRACTION D'ANIMAUX  (équivalent script `animaux`)                ║
║  5. TRANSFERT VERS LE NAS (équivalent script `copie_nas`)              ║
║  6. UTILITAIRES UI        (ZIP, file manager, lecture multi-DICOM…)    ║
║  7. ÉTAT DE SESSION & SIDEBAR                                          ║
║  8. PAGES                 (Workflow, Ancienne interface, À propos)     ║
║  9. ROUTAGE FINAL                                                      ║
╚════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# ════════════════════════════════════════════════════════════════════════
# 1. IMPORTS
# ════════════════════════════════════════════════════════════════════════
import filecmp
import io
import platform
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import pydicom
import streamlit as st
from pydicom.errors import InvalidDicomError


# ════════════════════════════════════════════════════════════════════════
# 2. CONFIGURATION STREAMLIT & CONSTANTES
# ════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Transfert IRM → NAS",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_ROOT = Path(__file__).parent.resolve()
DEMO_SOURCE = APP_ROOT / "sample_data" / "dicom_source"
DEMO_NAS = APP_ROOT / "sample_data" / "nas_target"
WORK_DIR = APP_ROOT / "workspace"
WORK_DIR.mkdir(exist_ok=True)

# Correspond au "first" du script bash : DICOM commence au champ 1, Bruker au 3
FIRST_FIELD = {"dicom": 1, "bruker": 3}


# ════════════════════════════════════════════════════════════════════════
# 3. LECTURE DICOM
#    Remplace les binaires Linux `dcmftest` et `dcmdump` du script bash
#    par pydicom → portable Linux / macOS / Windows et déployable cloud.
# ════════════════════════════════════════════════════════════════════════
@dataclass
class DicomMeta:
    """Champs DICOM utiles au workflow."""
    patient_name: str
    study_date: str
    study_id: str
    modality: str
    protocol_name: str
    is_valid: bool = True
    error: str | None = None


def _clean(value) -> str:
    """Nettoie une valeur DICOM (None → '', espaces → '_')."""
    if value is None:
        return ""
    return str(value).strip().replace(" ", "_")


def is_dicom(path: Path) -> bool:
    """Vrai si `path` est un fichier DICOM lisible. Équivalent `dcmftest`."""
    try:
        pydicom.dcmread(str(path), stop_before_pixels=True, force=False)
        return True
    except (InvalidDicomError, FileNotFoundError, IsADirectoryError, PermissionError):
        return False
    except Exception:
        return False


def read_meta(path: Path) -> DicomMeta:
    """Lit les tags PatientName/StudyDate/StudyID/Modality/ProtocolName.

    Équivalent de `dcmdump -M +P "0010,0010" +P "0008,0020" ...`.
    """
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=False)
    except Exception as e:
        return DicomMeta("", "", "", "", "", is_valid=False, error=str(e))
    return DicomMeta(
        patient_name=_clean(ds.get("PatientName", "")),
        study_date=_clean(ds.get("StudyDate", "")),
        study_id=_clean(ds.get("StudyID", "")),
        modality=_clean(ds.get("Modality", "")),
        protocol_name=_clean(ds.get("ProtocolName", "")),
    )


def first_dicom_in(folder: Path) -> Path | None:
    """Premier fichier DICOM trouvé dans le dossier.

    Équivalent `find -iname 'MR*' | head -n 1` mais plus tolérant
    (cherche aussi *.dcm).
    """
    if not folder.is_dir():
        return None
    candidates = sorted(
        list(folder.glob("MR*"))
        + list(folder.glob("mr*"))
        + list(folder.glob("*.dcm"))
        + list(folder.glob("*.DCM"))
    )
    for c in candidates:
        if c.is_file() and is_dicom(c):
            return c
    # Fallback : on essaie tout fichier du dossier
    for c in sorted(folder.iterdir()):
        if c.is_file() and is_dicom(c):
            return c
    return None


# ════════════════════════════════════════════════════════════════════════
# 4. EXTRACTION DES NOMS D'ANIMAUX
#    Équivalent du script bash `creation_fichier_animaux.sh` :
#    - DICOM  : on prend les `nb_separateur + 1` premiers segments séparés par '_'
#    - Bruker : on saute les 2 premiers segments (préfixe paravision technique)
#               puis on prend `nb_separateur + 1` segments
#    - On déduplique en préservant l'ordre, et on écrit dans un .txt
# ════════════════════════════════════════════════════════════════════════
@dataclass
class ExtractionResult:
    animals: list[str]
    inspected_folders: int
    skipped_folders: list[str]


def extract_animal_name(folder_name: str, source_type: str, nb_separateurs: int) -> str | None:
    """Reproduit la logique bash :
        nb_separateur_tot = nb_separateur + first
        name = cut -d'_' -f${first}-${nb_separateur_tot}

    Le `cut` bash est inclusif aux deux bornes → on garde
    `(nb_separateur_tot - first + 1) = nb_separateur + 1` champs.
    """
    if source_type not in FIRST_FIELD:
        raise ValueError(f"Type inconnu : {source_type}")

    parts = folder_name.split("_")
    separateurs = folder_name.count("_")
    if separateurs < nb_separateurs:
        return None

    first = FIRST_FIELD[source_type]
    last = nb_separateurs + first  # inclusif
    # bash `cut -f1-N` indexe à partir de 1 → conversion en 0-based
    selected = parts[first - 1:last]
    if not selected:
        return None
    return "_".join(selected)


def extract_animals(source_dir: Path, source_type: str, nb_separateurs: int) -> ExtractionResult:
    """Parcourt les sous-dossiers et extrait la liste unique des animaux."""
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
    """Sérialise la liste dans un .txt (un animal par ligne)."""
    output_path = Path(output_path)
    if output_path.suffix != ".txt":
        output_path = output_path.with_suffix(".txt")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(animals) + ("\n" if animals else ""))
    return output_path


# ════════════════════════════════════════════════════════════════════════
# 5. TRANSFERT VERS LE NAS
#    Équivalent du script bash `creation_folder_group_dicom.sh`.
#    Structure cible : {NAS}/{StudyDate}_{sujet}/{Time}/IRM/dicom/{séquence}/
# ════════════════════════════════════════════════════════════════════════
LogCallback = Callable[[str], None]


@dataclass
class TransferStats:
    sequences_total: int = 0
    sequences_copied: int = 0
    files_copied: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    new_animals: list[str] = field(default_factory=list)
    new_timepoints: list[str] = field(default_factory=list)


def _today() -> str:
    return datetime.now().strftime("%y_%m_%d")


def _emit(log: LogCallback | None, line: str, log_file: Path | None):
    """Envoie une ligne au callback UI et l'append au .log sur disque."""
    if log:
        log(line)
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def _copy_dicoms(src_seq: Path, dst_seq: Path) -> int:
    """Copie tous les DICOM valides de src vers dst. Retourne le nombre copié."""
    dst_seq.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(src_seq.iterdir()):
        if f.is_file() and is_dicom(f):
            shutil.copy2(f, dst_seq / f.name)
            n += 1
    return n


def _folders_differ(a: Path, b: Path) -> bool:
    """Équivalent de `diff folder1/ folder2/`."""
    cmp = filecmp.dircmp(str(a), str(b))
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return True
    return False


def _next_dedup_index(parent: Path, base_name: str, max_index: int = 2) -> int:
    """Prochaine valeur i telle que {base_name}_{i} n'existe pas (1..max_index)."""
    i = 1
    while (parent / f"{base_name}_{i}").exists() and i <= max_index:
        i += 1
    return i


def _find_animal_parent(nas_dir: Path, subject: str) -> list[Path]:
    """Cherche les dossiers du NAS dont le nom se termine par `subject`."""
    return [p for p in nas_dir.iterdir() if p.is_dir() and p.name.endswith(subject)]


def transfer_to_nas(
    source_dicom_dir: Path,
    nas_target_dir: Path,
    animals: Iterable[str],
    log_callback: LogCallback | None = None,
) -> TransferStats:
    """Boucle principale : pour chaque animal, repère ses séquences, lit les
    métadonnées DICOM, crée l'arborescence cible et copie les images.

    Gère les cas :
      - nouvel animal              → créé
      - animal connu, même date    → ajoute / ignore / dédoublonne séquences
      - animal connu, autre date   → ajoute un nouveau "Time" (T0, T1, …)
      - PatientName != sujet       → erreur loguée, séquence sautée
      - plusieurs dossiers parents → erreur loguée
    """
    source_dicom_dir = Path(source_dicom_dir)
    nas_target_dir = Path(nas_target_dir)
    nas_target_dir.mkdir(parents=True, exist_ok=True)

    log_file = nas_target_dir / f"{nas_target_dir.name}.log"
    date = _today()
    stats = TransferStats()

    for subject in animals:
        subject = subject.strip()
        if not subject:
            continue

        # Toutes les séquences du sujet
        seq_dirs = sorted(
            p for p in source_dicom_dir.iterdir()
            if p.is_dir() and p.name.lower().startswith(subject.lower() + "_")
        )
        if not seq_dirs:
            msg = f"[ERROR] l'animal {subject} n'existe pas"
            stats.errors.append(msg)
            _emit(log_callback, f"{date} {msg}", log_file)
            continue

        temp_new_timepoint_logged = False
        name_mismatch_logged = False

        for seq in seq_dirs:
            stats.sequences_total += 1

            dcm = first_dicom_in(seq)
            if dcm is None:
                msg = f"[ERROR] [DICOM] probleme dans le dossier sequence {seq.name}"
                stats.errors.append(msg)
                _emit(log_callback, f"{date} {msg}", log_file)
                continue

            meta = read_meta(dcm)
            if not meta.is_valid:
                msg = f"[ERROR] [DICOM] lecture impossible {seq.name}: {meta.error}"
                stats.errors.append(msg)
                _emit(log_callback, f"{date} {msg}", log_file)
                continue

            # Vérification nom animal vs PatientName DICOM
            if meta.patient_name != subject:
                if not name_mismatch_logged:
                    msg = (f"[ERROR] Le nom de l'animal {subject} n'est pas le meme "
                           f"que le PatientID: {meta.patient_name}")
                    stats.errors.append(msg)
                    _emit(log_callback, f"{date} {msg}", log_file)
                    name_mismatch_logged = True
                continue

            # Time : T0 si PatientName == StudyID, sinon dernier segment de StudyID
            if meta.patient_name == meta.study_id:
                time = "T0"
            else:
                time = meta.study_id.split("_")[-1] if meta.study_id else "T0"

            # Modalité
            if meta.modality == "MR":
                name_modalite = "IRM"
            else:
                msg = f"[WARNING] modalite n'est pas IRM pour {seq.name} ({meta.modality})"
                stats.warnings.append(msg)
                _emit(log_callback, f"{date} {msg}", log_file)
                name_modalite = meta.modality or "INCONNU"

            # Nom de séquence (le script bash gère aussi _P{n} pour Bruker —
            # ici simplifié à `ProtocolName`)
            name_sequence = meta.protocol_name or "sequence"

            # Chemin de destination
            study_dir = nas_target_dir / f"{meta.study_date}_{subject}"
            time_dir = study_dir / time / name_modalite / "dicom"
            seq_dst = time_dir / name_sequence

            parents = _find_animal_parent(nas_target_dir, subject)

            # ---- Cas 1 : nouvel animal ----
            if not parents:
                seq_dst.mkdir(parents=True, exist_ok=True)
                n = _copy_dicoms(seq, seq_dst)
                stats.files_copied += n
                stats.sequences_copied += 1
                if subject not in stats.new_animals:
                    stats.new_animals.append(subject)
                    _emit(log_callback,
                          f"{date} creation d'un nouvel animal {subject}", log_file)
                if _folders_differ(seq, seq_dst):
                    msg = f"[ERROR] [DICOM] mauvaise copie de la sequence {name_sequence}"
                    stats.errors.append(msg)
                    _emit(log_callback, f"{date} {msg}", log_file)
                continue

            # ---- Cas dégénéré : plusieurs dossiers parents ----
            if len(parents) > 1:
                msg = (f"[ERROR] Il existe plusieurs dossiers parents: {len(parents)} "
                       f"pour l'animal {subject}")
                stats.errors.append(msg)
                _emit(log_callback, f"{date} {msg}", log_file)
                continue

            existing_parent = parents[0]
            expected_parent = nas_target_dir / f"{meta.study_date}_{subject}"

            # ---- Cas 2 : animal connu, même date d'étude ----
            if existing_parent.resolve() == expected_parent.resolve():
                if seq_dst.is_dir() and any(seq_dst.iterdir()):
                    # Le dossier de séquence existe déjà
                    if not _folders_differ(seq, seq_dst):
                        # Contenu identique → déjà copié
                        msg = f"[WARNING] [DICOM] folder {seq_dst} exist"
                        stats.warnings.append(msg)
                        _emit(log_callback, f"{date} {msg}", log_file)
                    else:
                        # Contenu différent → on dédoublonne avec un suffixe _N
                        msg = (f"[WARNING] [DICOM] plusieurs sequences ont le meme nom "
                               f"{name_sequence} pour {subject}")
                        stats.warnings.append(msg)
                        _emit(log_callback, f"{date} {msg}", log_file)
                        idx = _next_dedup_index(time_dir, name_sequence, max_index=2)
                        if idx <= 2:
                            dedup_dst = time_dir / f"{name_sequence}_{idx}"
                            n = _copy_dicoms(seq, dedup_dst)
                            stats.files_copied += n
                            stats.sequences_copied += 1
                else:
                    # Première copie de cette séquence pour cet animal/cette date
                    seq_dst.mkdir(parents=True, exist_ok=True)
                    n = _copy_dicoms(seq, seq_dst)
                    stats.files_copied += n
                    stats.sequences_copied += 1
                    if _folders_differ(seq, seq_dst):
                        msg = f"[ERROR] [DICOM] mauvaise copie de la sequence {name_sequence}"
                        stats.errors.append(msg)
                        _emit(log_callback, f"{date} {msg}", log_file)
            # ---- Cas 3 : animal connu mais autre date → nouveau "Time" ----
            else:
                newtime = existing_parent / time / name_modalite / "dicom"
                if not temp_new_timepoint_logged:
                    stats.new_timepoints.append(f"{subject} @ {time}")
                    _emit(
                        log_callback,
                        f"{date} ajout d'un nouveau temps d'acquisition {time} "
                        f"pour un animal {subject} existant sur le NAS",
                        log_file,
                    )
                    temp_new_timepoint_logged = True
                newtime.mkdir(parents=True, exist_ok=True)
                final_dst = newtime / name_sequence
                if final_dst.is_dir() and any(final_dst.iterdir()):
                    if not _folders_differ(seq, final_dst):
                        msg = f"[WARNING] [DICOM] folder {final_dst} exist"
                        stats.warnings.append(msg)
                        _emit(log_callback, f"{date} {msg}", log_file)
                    else:
                        idx = _next_dedup_index(newtime, name_sequence, max_index=2)
                        if idx <= 2:
                            dedup_dst = newtime / f"{name_sequence}_{idx}"
                            n = _copy_dicoms(seq, dedup_dst)
                            stats.files_copied += n
                            stats.sequences_copied += 1
                else:
                    n = _copy_dicoms(seq, final_dst)
                    stats.files_copied += n
                    stats.sequences_copied += 1

    return stats


# ════════════════════════════════════════════════════════════════════════
# 6. UTILITAIRES UI
# ════════════════════════════════════════════════════════════════════════
def _extract_zip_to_temp(uploaded_file) -> Path:
    """Décompresse un ZIP uploadé dans un dossier temporaire.

    Si le ZIP contient un unique dossier racine, retourne ce dossier
    directement (UX plus naturelle).
    """
    tmp = Path(tempfile.mkdtemp(prefix="dicom_src_"))
    with zipfile.ZipFile(uploaded_file) as z:
        z.extractall(tmp)
    entries = [e for e in tmp.iterdir() if not e.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tmp


def open_in_file_manager(path: Path) -> tuple[bool, str]:
    """Ouvre `path` dans Finder / Nautilus / Explorer selon l'OS.

    Ne fonctionne qu'en local — le serveur Streamlit Cloud n'a pas de desktop.
    Retourne (succès, message).
    """
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", str(path)])
        elif system == "Windows":
            subprocess.Popen(["explorer", str(path)])
        else:
            return False, f"OS non supporté : {system}"
        return True, f"Ouvert dans l'explorateur ({system})"
    except FileNotFoundError as e:
        return False, f"Outil système indisponible (mode cloud ?) — {e}"
    except Exception as e:
        return False, str(e)


def zip_directory_to_bytes(path: Path) -> bytes:
    """Crée un ZIP en mémoire contenant toute l'arborescence de `path`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in path.rglob("*"):
            if f.is_file():
                zf.write(f, arcname=str(f.relative_to(path)))
    buf.seek(0)
    return buf.read()


def _animal_names_from_dicom_files(files) -> tuple[list[str], int, int]:
    """Lit chaque fichier uploadé et extrait son tag PatientName.

    Retourne (animaux_uniques, nb_lus, nb_erreurs).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    n_err = 0
    for f in files:
        try:
            ds = pydicom.dcmread(f, stop_before_pixels=True, force=True)
            pn = str(ds.get("PatientName", "")).strip().replace(" ", "_")
            if pn and pn not in seen:
                seen.add(pn)
                ordered.append(pn)
            elif not pn:
                n_err += 1
        except Exception:
            n_err += 1
    return ordered, len(files), n_err


def check_patient_names(source_dir: Path, animals: list[str]) -> list[dict]:
    """Pour chaque animal, lit le PatientName du premier DICOM trouvé
    et compare. Sert à valider le réglage `nb_separateurs`."""
    results = []
    for a in animals:
        match = next(
            (p for p in source_dir.iterdir()
             if p.is_dir() and p.name.lower().startswith(a.lower() + "_")),
            None,
        )
        if match is None:
            results.append({"Animal": a, "Dossier": "(introuvable)",
                            "PatientName": "—", "Match": "❌"})
            continue
        dcm = first_dicom_in(match)
        if dcm is None:
            results.append({"Animal": a, "Dossier": match.name,
                            "PatientName": "(pas de DICOM)", "Match": "❌"})
            continue
        meta = read_meta(dcm)
        results.append({
            "Animal": a,
            "Dossier": match.name,
            "PatientName": meta.patient_name,
            "Match": "✅" if meta.patient_name == a else "❌",
        })
    return results


# ════════════════════════════════════════════════════════════════════════
# 7. ÉTAT DE SESSION & SIDEBAR
# ════════════════════════════════════════════════════════════════════════
def _init_state():
    defaults = {
        "source_type": "dicom",
        "source_dir": str(DEMO_SOURCE) if DEMO_SOURCE.exists() else "",
        "nas_dir": str(DEMO_NAS) if DEMO_NAS.exists() else "",
        "nb_separateurs": 2,
        "animals_file_name": "animaux",
        "extracted_animals": [],
        "transfer_logs": [],
        "transfer_stats": None,
        "demo_mode": DEMO_SOURCE.exists(),
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


_init_state()

with st.sidebar:
    st.title("Transfert IRM → NAS")
    st.caption("Interface unifiée des scripts `animaux` & `copie_nas`")
    page = st.radio(
        "Navigation",
        ["⌨️ Ancienne interface", "📋 Nouvelle interface", "ⓘ À propos"],
        label_visibility="collapsed",
    )
    st.divider()
    if st.session_state.demo_mode:
        st.success("Mode démo actif", icon="🧪")
    st.caption("CHR · MVP audit")


# ════════════════════════════════════════════════════════════════════════
# 8. PAGES
# ════════════════════════════════════════════════════════════════════════

# ---------- PAGE : Nouvelle interface (workflow unifié) ----------
def page_workflow():
    st.title("Transfert IRM → NAS")
    st.caption(
        "Toute la procédure sur une page : configurer → extraire les animaux → "
        "transférer vers le NAS → vérifier les logs."
    )

    # ----- 1. CONFIGURATION -----
    st.subheader("1 · Configuration")
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        st.session_state.source_type = st.radio(
            "Type de données",
            ["dicom", "bruker"],
            index=0 if st.session_state.source_type == "dicom" else 1,
            horizontal=True,
        )
    with c2:
        st.session_state.nb_separateurs = st.number_input(
            "Séparateurs `_` dans le nom d'animal",
            min_value=0, max_value=10,
            value=int(st.session_state.nb_separateurs),
            help="Ex. `M_4_11` → 2 séparateurs",
        )
    with c3:
        st.session_state.animals_file_name = st.text_input(
            "Nom du fichier d'animaux",
            value=st.session_state.animals_file_name,
            help="Sans extension `.txt`",
        )

    # --- Dossier source : champ texte + boutons démo / upload ZIP ---
    sc1, sc2, sc3 = st.columns([4, 1, 1.4])
    with sc1:
        st.session_state.source_dir = st.text_input(
            "📁 Dossier source des séquences",
            value=st.session_state.source_dir,
            help=(
                "En **local** : tapez n'importe quel chemin (ex. `/opt/PV6.0.1/DICOM-Laurent`).\n\n"
                "En **cloud** : utilisez 🧪 Démo ou 📂 Importer un ZIP — le serveur "
                "ne peut pas voir votre disque."
            ),
        )
    with sc2:
        st.write("")
        st.write("")
        if st.button("🧪 Démo", use_container_width=True,
                     help="Restaurer le dossier d'exemple embarqué + la config qui va avec"):
            st.session_state.source_dir = str(DEMO_SOURCE)
            st.session_state.nas_dir = str(DEMO_NAS)
            st.session_state.source_type = "dicom"
            st.session_state.nb_separateurs = 2
            st.rerun()
    with sc3:
        st.write("")
        st.write("")
        with st.popover("📂 Importer ZIP", use_container_width=True):
            st.caption(
                "Zippez votre dossier DICOM sur votre PC, puis uploadez-le ici. "
                "L'app décompresse côté serveur et utilise le résultat comme source."
            )
            zf = st.file_uploader("Fichier .zip", type=["zip"], key="src_zip_uploader")
            if zf is not None:
                try:
                    p = _extract_zip_to_temp(zf)
                    st.session_state.source_dir = str(p)
                    st.success(f"Importé dans `{p}`")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur : {e}")

    st.session_state.nas_dir = st.text_input(
        "💾 Dossier cible sur le NAS",
        value=st.session_state.nas_dir,
        help="Ex. `/opt/NASIRM/copitch`",
    )

    src = Path(st.session_state.source_dir) if st.session_state.source_dir else None
    nas = Path(st.session_state.nas_dir) if st.session_state.nas_dir else None
    cols = st.columns(2)
    with cols[0]:
        if src and src.is_dir():
            n = sum(1 for _ in src.iterdir() if _.is_dir())
            st.success(f"Source OK · {n} sous-dossiers détectés")
        elif src:
            st.error("Dossier source introuvable")
        else:
            st.warning("Dossier source non renseigné")
    with cols[1]:
        if nas and nas.is_dir():
            st.success("Dossier NAS accessible")
        elif nas:
            st.info("Cible inexistante (sera créée lors du transfert)")
        else:
            st.warning("Dossier NAS non renseigné")

    if not (src and src.is_dir() and nas):
        st.stop()

    st.divider()

    # ----- 2. LISTE DES ANIMAUX -----
    st.subheader("2 · Liste des animaux à traiter")
    st.caption(
        "Quatre façons d'alimenter la liste — équivalent au script `animaux` "
        "ou à n'importe quel `.txt` existant utilisé par `copie_nas`."
    )

    method = st.radio(
        "Méthode",
        ["🔍 Extraire depuis le dossier source",
         "📂 Sélectionner des DICOM depuis l'ordinateur",
         "📤 Importer un fichier .txt",
         "✏️ Saisir manuellement"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if method.startswith("🔍"):
        if st.button("Extraire les animaux", type="primary"):
            try:
                res = extract_animals(src, st.session_state.source_type,
                                      int(st.session_state.nb_separateurs))
                st.session_state.extracted_animals = res.animals
                if res.skipped_folders:
                    st.toast(f"{len(res.skipped_folders)} dossiers ignorés", icon="⚠️")
            except Exception as e:
                st.error(f"Erreur : {e}")
    elif method.startswith("📂"):
        st.caption(
            "Sélectionnez plusieurs fichiers DICOM depuis votre ordinateur. "
            "Le nom de chaque animal sera lu directement depuis le tag DICOM "
            "`PatientName` — méthode la plus fiable, indépendante du nom des dossiers."
        )
        files = st.file_uploader(
            "Fichiers DICOM (multi-sélection avec Cmd/Ctrl)",
            type=["dcm", "DCM"],
            accept_multiple_files=True,
            key="dicom_files_uploader",
        )
        if files:
            animals_list, n_read, n_err = _animal_names_from_dicom_files(files)
            st.session_state.extracted_animals = animals_list
            if n_err:
                st.warning(f"{n_err} fichier(s) ignoré(s) (non-DICOM ou PatientName vide)")
            st.success(f"{len(animals_list)} animaux uniques lus depuis {n_read} fichiers")
    elif method.startswith("📤"):
        up = st.file_uploader("Fichier `.txt` (un animal par ligne)", type=["txt"])
        if up is not None:
            loaded = [l.strip() for l in up.read().decode().splitlines() if l.strip()]
            st.session_state.extracted_animals = loaded
            st.success(f"{len(loaded)} animaux chargés")
    else:
        txt = st.text_area(
            "Un nom d'animal par ligne",
            value="\n".join(st.session_state.extracted_animals),
            height=160,
        )
        st.session_state.extracted_animals = [
            l.strip() for l in txt.splitlines() if l.strip()
        ]

    animals = st.session_state.extracted_animals

    if not animals:
        st.info("Choisissez une méthode ci-dessus pour constituer la liste.")
        st.stop()

    st.metric("Animaux dans la liste", len(animals))

    # Vérification PatientName — disponible quelle que soit la source
    if src and src.is_dir():
        rows = check_patient_names(src, animals)
        n_ko = sum(1 for r in rows if r["Match"] == "❌")
        with st.expander(
            f"🔍 Vérification PatientName ({len(animals) - n_ko}/{len(animals)} OK)",
            expanded=n_ko > 0,
        ):
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            if n_ko:
                st.warning(
                    f"⚠️ **{n_ko} animal(aux) ne correspondent pas au PatientName DICOM.** "
                    f"Soit le `.txt` ne correspond pas au dossier source, "
                    f"soit `Séparateurs _` est mal réglé (souvent `2` pour `M_4_11`). "
                    f"Le transfert affichera des erreurs pour ces animaux."
                )

    with st.expander("✏️ Éditer ou télécharger la liste"):
        edited = st.text_area(
            "Un animal par ligne",
            value="\n".join(animals),
            height=160,
            key="edit_animals_textarea",
        )
        animals = [l.strip() for l in edited.splitlines() if l.strip()]
        st.session_state.extracted_animals = animals
        ec1, ec2 = st.columns(2)
        with ec1:
            if st.button("💾 Sauvegarder en .txt"):
                out = WORK_DIR / f"{st.session_state.animals_file_name}.txt"
                write_animals_file(animals, out)
                st.success(f"Enregistré : `{out}`")
        with ec2:
            st.download_button(
                "⬇️ Télécharger .txt",
                data="\n".join(animals) + "\n",
                file_name=f"{st.session_state.animals_file_name}.txt",
                mime="text/plain",
                use_container_width=True,
            )

    st.divider()

    # ----- 3. TRANSFERT -----
    st.subheader("3 · Transfert vers le NAS")
    st.caption(
        "Reconstruit l'arborescence "
        "`{NAS}/{StudyDate}_{sujet}/{Time}/IRM/dicom/{séquence}/` et copie les DICOM."
    )

    tc1, tc2 = st.columns([1, 2])
    with tc1:
        dry_run = st.toggle("Mode simulation (dry-run)", value=False,
                            help="Analyse sans rien copier")
    with tc2:
        run_btn = st.button("🚀 Lancer le transfert", type="primary",
                            disabled=not animals, use_container_width=True)

    if run_btn:
        progress = st.progress(0.0, text="Préparation…")
        lines: list[str] = []

        def cb(line: str):
            # On collecte silencieusement pour le .log sur disque,
            # sans afficher à l'écran (cf. section 4 pour le téléchargement)
            lines.append(line)

        try:
            if dry_run:
                for i, subject in enumerate(animals, 1):
                    progress.progress(i / len(animals), text=f"Analyse de {subject}…")
                    seqs = [p for p in src.iterdir()
                            if p.is_dir() and p.name.lower().startswith(subject.lower() + "_")]
                    cb(f"[DRY-RUN] {subject} → {len(seqs)} séquences")
                progress.progress(1.0, text="Terminé")
                st.success("Simulation terminée — aucun fichier écrit.")
            else:
                stats = transfer_to_nas(src, nas, animals, log_callback=cb)
                progress.progress(1.0, text="Terminé")
                st.session_state.transfer_stats = stats
                st.session_state.transfer_logs = lines

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Séquences", stats.sequences_total)
                m2.metric("Copiées", stats.sequences_copied)
                m3.metric("Fichiers DICOM", stats.files_copied)
                m4.metric("Err / Warn", f"{len(stats.errors)} / {len(stats.warnings)}")

                if stats.errors:
                    st.error(
                        f"{len(stats.errors)} erreur(s) — voir détails dans le .log "
                        f"téléchargeable section 4."
                    )
                if stats.warnings:
                    st.warning(
                        f"{len(stats.warnings)} warning(s) — voir détails dans le .log "
                        f"téléchargeable section 4."
                    )
                if not stats.errors and not stats.warnings:
                    st.success("Transfert terminé sans erreur ni warning.")
        except Exception as e:
            st.error(f"Erreur lors du transfert : {e}")

    st.divider()

    # ----- 4. LOGS & ARBORESCENCE -----
    st.subheader("4 · Vérification du résultat")
    if nas.exists():
        bc1, bc2, bc3 = st.columns([1, 1, 2])
        with bc1:
            if st.button("📂 Ouvrir l'explorateur", use_container_width=True,
                         help="Ouvre Finder / Nautilus / Explorer sur le dossier NAS. "
                              "Ne fonctionne que si l'app tourne en local."):
                ok, msg = open_in_file_manager(nas)
                if ok:
                    st.success(msg)
                else:
                    st.warning(
                        f"{msg}\n\nVous êtes probablement en mode cloud — "
                        f"utilisez le téléchargement ZIP à droite pour récupérer "
                        f"l'arborescence."
                    )
        with bc2:
            try:
                zip_bytes = zip_directory_to_bytes(nas)
                st.download_button(
                    "⬇️ Télécharger en ZIP",
                    data=zip_bytes,
                    file_name=f"{nas.name}.zip",
                    mime="application/zip",
                    use_container_width=True,
                    help="Télécharge toute l'arborescence créée sur votre ordinateur",
                )
            except Exception as e:
                st.caption(f"ZIP indisponible : {e}")
        with bc3:
            st.caption(f"📍 Chemin du dossier NAS : `{nas}`")

        log_file = nas / f"{nas.name}.log"
        lc1, lc2 = st.columns([1, 2])
        with lc1:
            st.markdown("**📄 Fichier `.log`**")
            if log_file.exists():
                content = log_file.read_text()
                size = len(content.encode())
                st.caption(f"{len(content.splitlines())} lignes · {size} octets")
                st.download_button("⬇️ Télécharger .log", data=content,
                                   file_name=log_file.name, mime="text/plain",
                                   use_container_width=True)
            else:
                st.info("Pas encore de fichier de log.")
        with lc2:
            st.markdown("**🗂️ Arborescence créée sur le NAS**")
            rows = []
            for p in sorted(nas.rglob("*")):
                if p.is_dir():
                    depth = len(p.relative_to(nas).parts)
                    if depth <= 5:
                        rows.append({
                            "Chemin": str(p.relative_to(nas)),
                            "Niveau": depth,
                            "Fichiers": sum(1 for f in p.iterdir() if f.is_file()),
                        })
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True,
                             use_container_width=True, height=300)
            else:
                st.caption("Dossier vide.")
    else:
        st.info("Le dossier NAS n'existe pas encore.")


# ---------- PAGE : Ancienne interface (simulation Konsole) ----------
def page_cli():
    st.header("⌨️ Ancienne interface — simulation des scripts bash")
    st.markdown(
        "Reproduit fidèlement l'expérience en ligne de commande des scripts "
        "`animaux` et `copie_nas` tels qu'utilisés avant cette interface. "
        "Utile pour la formation et la transition."
    )

    script_choice = st.radio(
        "Script à simuler",
        ["animaux (extraction des animaux)", "copie_nas (transfert vers NAS)"],
        horizontal=True,
    )
    is_script1 = script_choice.startswith("animaux")
    cmd_name = "animaux" if is_script1 else "copie_nas"

    state_key = f"cli_state_{cmd_name}"
    if state_key not in st.session_state:
        st.session_state[state_key] = {"step": 0, "answers": {}, "done": False, "result": None}
    state = st.session_state[state_key]

    if is_script1:
        steps = [
            {"prompt": "Les données a copier sont-elles des données dicom ou bruker ?",
             "field": "type", "default": "dicom", "hint": "dicom / bruker"},
            {"prompt": "Dans quel sous dossier se trouve les noms des animaux a recuperer ? /opt/PV6.0.1/",
             "field": "sous_dossier", "default": "DICOM-Laurent", "hint": ""},
            {"prompt": "Combien de '_' comprend le nom des animaux ?",
             "field": "nb_sep", "default": "2", "hint": "ex. 2 pour M_4_11"},
            {"prompt": "Quel nom souhaitez-vous pour le .txt comprenant le nom des animaux : /opt/code_nas/",
             "field": "name", "default": "animaux_copitch", "hint": "sans .txt"},
        ]
    else:
        steps = [
            {"prompt": "Les données a copier sont-elles des données dicom ou bruker ?",
             "field": "type", "default": "dicom", "hint": "dicom / bruker"},
            {"prompt": "Dans quel sous dossier se trouve les sequences dicom à copier sur le NAS ? /opt/PV6.0.1/",
             "field": "sous_dossier_dicom", "default": "DICOM-Laurent", "hint": ""},
            {"prompt": "Dans quel dossier faut-il copier ces images ? /opt/NASIRM/",
             "field": "sous_dossier_NAS", "default": "copitch", "hint": ""},
            {"prompt": "fichier comprenant les animaux a traiter : /opt/code_nas/",
             "field": "file_animaux", "default": "animaux_copitch.txt", "hint": ""},
        ]

    PROMPT = "[MATISSE@CZC6177SYC ~]$"
    lines = [f"{PROMPT} {cmd_name}"]
    for i, s in enumerate(steps):
        if i < state["step"]:
            ans = state["answers"].get(s["field"], "")
            lines.append(f"{s['prompt']} {ans}")
        elif i == state["step"] and not state["done"]:
            lines.append(s["prompt"])

    if state["done"]:
        if state.get("result_lines"):
            lines.extend(state["result_lines"])
        lines.append(f"{PROMPT} ")

    st.markdown(
        f"""
        <div style="background:#1e1e1e;color:#d4d4d4;font-family:monospace;
                    padding:14px;border-radius:6px;font-size:13px;
                    white-space:pre-wrap;line-height:1.5;border:1px solid #444;">
{chr(10).join(lines)}
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write("")

    if state["done"]:
        st.success(f"Script `{cmd_name}` terminé.")
        if state["result"] is not None and cmd_name == "animaux":
            st.code("\n".join(state["result"]) or "(vide)", language="text")
        elif state["result"] is not None:
            stats = state["result"]
            c1, c2, c3 = st.columns(3)
            c1.metric("Séquences", stats.sequences_total)
            c2.metric("Fichiers copiés", stats.files_copied)
            c3.metric("Err / Warn", f"{len(stats.errors)} / {len(stats.warnings)}")
        if st.button("🔄 Recommencer", key=f"reset_{cmd_name}"):
            st.session_state[state_key] = {"step": 0, "answers": {}, "done": False, "result": None}
            st.rerun()
        return

    current = steps[state["step"]]
    with st.form(key=f"form_{cmd_name}_{state['step']}", clear_on_submit=True):
        val = st.text_input(
            f"Saisie ({current['hint']})" if current["hint"] else "Saisie",
            value="", placeholder=current["default"],
        )
        if st.form_submit_button("⏎ Entrée"):
            val = val.strip() or current["default"]
            state["answers"][current["field"]] = val
            state["step"] += 1
            if state["step"] >= len(steps):
                try:
                    if is_script1:
                        src = Path(st.session_state.source_dir or str(DEMO_SOURCE))
                        res = extract_animals(
                            src,
                            state["answers"]["type"],
                            int(state["answers"]["nb_sep"]),
                        )
                        out_path = WORK_DIR / f"{state['answers']['name']}.txt"
                        write_animals_file(res.animals, out_path)
                        state["result"] = res.animals
                        state["result_lines"] = [
                            f"  → {len(res.animals)} animaux extraits dans {out_path}",
                        ]
                        st.session_state.extracted_animals = res.animals
                    else:
                        src = Path(st.session_state.source_dir or str(DEMO_SOURCE))
                        nas = Path(st.session_state.nas_dir or str(DEMO_NAS))
                        animals = list(st.session_state.extracted_animals or [])
                        if not animals:
                            state["result_lines"] = [
                                "  → liste d'animaux vide, passez d'abord par 'animaux'"
                            ]
                            state["result"] = None
                        else:
                            stats = transfer_to_nas(src, nas, animals)
                            state["result"] = stats
                            state["result_lines"] = [
                                f"  → {stats.files_copied} fichiers copiés, "
                                f"{len(stats.errors)} erreurs, {len(stats.warnings)} warnings",
                            ]
                    state["done"] = True
                except Exception as e:
                    state["result_lines"] = [f"  → erreur: {e}"]
                    state["done"] = True
            st.rerun()


# ---------- PAGE : À propos ----------
def page_about():
    st.header("ⓘ À propos")
    st.markdown(
        """
        **Transfert IRM → NAS** regroupe en une seule interface :
        - `creation_fichier_animaux.sh` (le script `animaux`)
        - `creation_folder_group_dicom.sh` (le script `copie_nas`)

        ### Différences avec les scripts bash
        - **Multi-OS** : utilise `pydicom` au lieu de `dcmftest` / `dcmdump`
          (binaires Linux uniquement).
        - **Vérification PatientName** automatique pour éviter les erreurs de
          paramétrage `nb_separateurs`.
        - **Mode simulation** (dry-run) : on peut voir ce qui sera fait avant
          de copier.
        - **Ancienne interface** : reproduit fidèlement l'expérience bash
          (Konsole) pour la transition et la formation.
        - **Logs en temps réel** et téléchargement direct des `.txt` et `.log`.

        ### Pour utiliser cette app sur les vraies données
        Lancer l'app **en local** sur le PC d'acquisition IRM. Le déploiement
        cloud sert uniquement de **démonstration** avec les données embarquées.

        ### Architecture du code
        Tout le code Python tient dans **un seul fichier `app.py`** organisé
        en 9 sections numérotées (voir le sommaire en tête du fichier).
        Cette monolite volontaire simplifie la revue et la maintenance.

        ### Limites connues
        - Cas Bruker `_P{n}` (numéro paravision) simplifié à `ProtocolName`.
        - Pas d'authentification : protéger derrière un mot de passe Streamlit
          si exposé publiquement.
        """
    )


# ════════════════════════════════════════════════════════════════════════
# 9. ROUTAGE FINAL
# ════════════════════════════════════════════════════════════════════════
PAGES = {
    "⌨️ Ancienne interface": page_cli,
    "📋 Nouvelle interface": page_workflow,
    "ⓘ À propos": page_about,
}

PAGES[page]()
