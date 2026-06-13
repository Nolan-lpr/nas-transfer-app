"""Interface graphique pour le workflow de transfert IRM → NAS.

Regroupe les deux scripts bash (`creation_fichier_animaux.sh` et
`creation_folder_group_dicom.sh`) en une UI web unifiée.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import pydicom
import streamlit as st

from core.animal_extractor import extract_animals, write_animals_file
from core.dicom_reader import first_dicom_in, read_meta
from core.dicom_transfer import transfer_to_nas


def _extract_zip_to_temp(uploaded_file) -> Path:
    """Décompresse un ZIP uploadé dans un dossier temporaire et renvoie son chemin.

    Si le ZIP contient un unique dossier racine, on retourne ce dossier
    directement (UX plus naturelle).
    """
    tmp = Path(tempfile.mkdtemp(prefix="dicom_src_"))
    with zipfile.ZipFile(uploaded_file) as z:
        z.extractall(tmp)
    entries = [e for e in tmp.iterdir() if not e.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tmp


def _animal_names_from_dicom_files(files) -> tuple[list[str], int, int]:
    """Lit chaque fichier uploadé et extrait PatientName.

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


# ---------- Setup ----------
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


def check_patient_names(source_dir: Path, animals: list[str]) -> list[dict]:
    """Pour chaque animal, lit le PatientName du premier DICOM trouvé et compare."""
    results = []
    for a in animals:
        match = next(
            (p for p in source_dir.iterdir()
             if p.is_dir() and p.name.lower().startswith(a.lower() + "_")),
            None,
        )
        if match is None:
            results.append({"Animal": a, "Dossier": "(introuvable)", "PatientName": "—", "Match": "❌"})
            continue
        dcm = first_dicom_in(match)
        if dcm is None:
            results.append({"Animal": a, "Dossier": match.name, "PatientName": "(pas de DICOM)", "Match": "❌"})
            continue
        meta = read_meta(dcm)
        ok = meta.patient_name == a
        results.append({
            "Animal": a,
            "Dossier": match.name,
            "PatientName": meta.patient_name,
            "Match": "✅" if ok else "❌",
        })
    return results


# ---------- Sidebar ----------
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


# ============================================================
#  PAGE 1 — WORKFLOW UNIFIÉ
# ============================================================
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
                     help="Restaurer le dossier d'exemple embarqué"):
            st.session_state.source_dir = str(DEMO_SOURCE)
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
        "Trois façons d'alimenter la liste — équivalent au script `animaux` "
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

    # Édition / export — pratiques si on vient d'extraire
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
        log_area = st.empty()
        lines: list[str] = []

        def cb(line: str):
            lines.append(line)
            log_area.code("\n".join(lines[-30:]), language="log")

        try:
            if dry_run:
                for i, subject in enumerate(animals, 1):
                    progress.progress(i / len(animals), text=f"Analyse de {subject}…")
                    seqs = [p for p in src.iterdir()
                            if p.is_dir() and p.name.lower().startswith(subject.lower() + "_")]
                    cb(f"[DRY-RUN] {subject} → {len(seqs)} séquences")
                    for s in seqs:
                        dcm = first_dicom_in(s)
                        if dcm:
                            m = read_meta(dcm)
                            cb(f"    └ {s.name} | StudyDate={m.study_date} Protocol={m.protocol_name}")
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
                    with st.expander(f"❌ {len(stats.errors)} erreurs", expanded=True):
                        st.code("\n".join(stats.errors))
                if stats.warnings:
                    with st.expander(f"⚠️ {len(stats.warnings)} warnings"):
                        st.code("\n".join(stats.warnings))
                st.success("Transfert terminé.")
        except Exception as e:
            st.error(f"Erreur lors du transfert : {e}")

    st.divider()

    # ----- 4. LOGS & ARBORESCENCE -----
    st.subheader("4 · Vérification du résultat")
    if nas.exists():
        log_file = nas / f"{nas.name}.log"
        lc1, lc2 = st.columns(2)
        with lc1:
            st.markdown("**📄 Fichier `.log`**")
            if log_file.exists():
                content = log_file.read_text()
                st.code(content or "(vide)", language="log")
                st.download_button("⬇️ Télécharger .log", data=content,
                                   file_name=log_file.name, mime="text/plain")
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


# ============================================================
#  PAGE 2 — MODE TERMINAL (CLI)
# ============================================================
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


# ============================================================
#  PAGE 3 — À PROPOS
# ============================================================
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

        ### Limites connues
        - Cas Bruker `_P{n}` (numéro paravision) simplifié à `ProtocolName`.
        - Pas d'authentification : protéger derrière un mot de passe Streamlit
          si exposé publiquement.
        """
    )


PAGES = {
    "⌨️ Ancienne interface": page_cli,
    "📋 Nouvelle interface": page_workflow,
    "ⓘ À propos": page_about,
}

PAGES[page]()
