"""Lecture des métadonnées DICOM via pydicom.

Remplace les binaires Linux `dcmftest` et `dcmdump` utilisés dans le script
bash original. Permet de faire tourner l'app sur n'importe quel OS et de la
déployer dans le cloud.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pydicom
from pydicom.errors import InvalidDicomError


@dataclass
class DicomMeta:
    patient_name: str
    study_date: str
    study_id: str
    modality: str
    protocol_name: str
    is_valid: bool = True
    error: str | None = None


def _clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip().replace(" ", "_")


def is_dicom(path: Path) -> bool:
    """Équivalent de `dcmftest <file> | cut -c1` → 'y'."""
    try:
        pydicom.dcmread(str(path), stop_before_pixels=True, force=False)
        return True
    except (InvalidDicomError, FileNotFoundError, IsADirectoryError, PermissionError):
        return False
    except Exception:
        return False


def read_meta(path: Path) -> DicomMeta:
    """Équivalent de `dcmdump -M +P "0010,0010" +P "0008,0020" ...`."""
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
    """Premier fichier DICOM trouvé dans le dossier (équivalent `find -iname 'MR*' | head -n 1`)."""
    if not folder.is_dir():
        return None
    candidates = sorted(
        list(folder.glob("MR*")) + list(folder.glob("mr*")) + list(folder.glob("*.dcm")) + list(folder.glob("*.DCM"))
    )
    for c in candidates:
        if c.is_file() and is_dicom(c):
            return c
    for c in sorted(folder.iterdir()):
        if c.is_file() and is_dicom(c):
            return c
    return None


def list_dicoms_in(folder: Path) -> list[Path]:
    """Tous les fichiers DICOM d'un dossier (récursif d'un niveau, comme le script)."""
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.iterdir()) if p.is_file() and is_dicom(p)]
