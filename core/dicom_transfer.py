"""Transfert et restructuration des séquences DICOM vers le NAS.

Réplique creation_folder_group_dicom.sh en Python. Structure cible :
    {NAS}/{StudyDate}_{sujet}/{Time}/IRM/dicom/{name_sequence}/
"""

from __future__ import annotations

import filecmp
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .dicom_reader import DicomMeta, first_dicom_in, is_dicom, read_meta


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
    """Copie tous les vrais DICOM de src_seq vers dst_seq, retourne le nombre copié."""
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
    """Cherche la prochaine valeur i telle que {base_name}_{i} n'existe pas (1..max_index)."""
    i = 1
    while (parent / f"{base_name}_{i}").exists() and i <= max_index:
        i += 1
    return i


def _find_animal_parent(nas_dir: Path, subject: str) -> list[Path]:
    return [p for p in nas_dir.iterdir() if p.is_dir() and p.name.endswith(subject)]


def transfer_to_nas(
    source_dicom_dir: Path,
    nas_target_dir: Path,
    animals: Iterable[str],
    log_callback: LogCallback | None = None,
) -> TransferStats:
    """Boucle principale : pour chaque animal, repère ses séquences, lit les métadonnées
    DICOM, crée l'arborescence cible sur le NAS et copie les images."""
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

        # Toutes les séquences du sujet (find ${dossier_dicom} -maxdepth 1 -iname "${sujet}_*")
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

            meta: DicomMeta = read_meta(dcm)
            if not meta.is_valid:
                msg = f"[ERROR] [DICOM] lecture impossible {seq.name}: {meta.error}"
                stats.errors.append(msg)
                _emit(log_callback, f"{date} {msg}", log_file)
                continue

            # check nom animal vs PatientName
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

            # Nom de séquence : protocol_name (le script gère aussi _P{n} mais on
            # simplifie ici car ce cas dépend de la convention Bruker→DICOM, hors champ MVP)
            name_sequence = meta.protocol_name or "sequence"

            # Chemin de destination
            study_dir = nas_target_dir / f"{meta.study_date}_{subject}"
            time_dir = study_dir / time / name_modalite / "dicom"
            seq_dst = time_dir / name_sequence

            parents = _find_animal_parent(nas_target_dir, subject)

            if not parents:
                # nouvel animal
                seq_dst.mkdir(parents=True, exist_ok=True)
                n = _copy_dicoms(seq, seq_dst)
                stats.files_copied += n
                stats.sequences_copied += 1
                if subject not in stats.new_animals:
                    stats.new_animals.append(subject)
                    _emit(log_callback, f"{date} creation d'un nouvel animal {subject}", log_file)
                if _folders_differ(seq, seq_dst):
                    msg = f"[ERROR] [DICOM] mauvaise copie de la sequence {name_sequence}"
                    stats.errors.append(msg)
                    _emit(log_callback, f"{date} {msg}", log_file)
                continue

            if len(parents) > 1:
                msg = (f"[ERROR] Il existe plusieurs dossiers parents: {len(parents)} "
                       f"pour l'animal {subject}")
                stats.errors.append(msg)
                _emit(log_callback, f"{date} {msg}", log_file)
                continue

            existing_parent = parents[0]
            expected_parent = nas_target_dir / f"{meta.study_date}_{subject}"

            if existing_parent.resolve() == expected_parent.resolve():
                # même date → on tombe peut-être sur une séquence déjà copiée
                if seq_dst.is_dir() and any(seq_dst.iterdir()):
                    if not _folders_differ(seq, seq_dst):
                        msg = f"[WARNING] [DICOM] folder {seq_dst} exist"
                        stats.warnings.append(msg)
                        _emit(log_callback, f"{date} {msg}", log_file)
                    else:
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
                    seq_dst.mkdir(parents=True, exist_ok=True)
                    n = _copy_dicoms(seq, seq_dst)
                    stats.files_copied += n
                    stats.sequences_copied += 1
                    if _folders_differ(seq, seq_dst):
                        msg = f"[ERROR] [DICOM] mauvaise copie de la sequence {name_sequence}"
                        stats.errors.append(msg)
                        _emit(log_callback, f"{date} {msg}", log_file)
            else:
                # même animal, autre date → nouveau "Time"
                newtime = existing_parent / time / name_modalite / "dicom"
                if not temp_new_timepoint_logged:
                    label = f"{subject} @ {time}"
                    stats.new_timepoints.append(label)
                    _emit(
                        log_callback,
                        f"{date} ajout d'un nouveau temps d'acquisition {time} pour un animal {subject} existant sur le NAS",
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
