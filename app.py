"""Interface graphique pour le workflow de transfert IRM → NAS.

Regroupe les deux scripts bash (`creation_fichier_animaux.sh` et
`creation_folder_group_dicom.sh`) en une UI web unifiée.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from core.animal_extractor import extract_animals, write_animals_file
from core.dicom_reader import first_dicom_in, read_meta
from core.dicom_transfer import transfer_to_nas


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
            results.append({"Animal": a, "Premier dossier": "(introuvable)", "PatientName DICOM": "—", "OK ?": "❌"})
            continue
        dcm = first_dicom_in(match)
        if dcm is None:
            results.append({"Animal": a, "Premier dossier": match.name, "PatientName DICOM": "(pas de DICOM)", "OK ?": "❌"})
            continue
        meta = read_meta(dcm)
        ok = meta.patient_name == a
        results.append({
            "Animal": a,
            "Premier dossier": match.name,
            "PatientName DICOM": meta.patient_name,
            "OK ?": "✅" if ok else "❌",
        })
    return results


# ---------- Setup ----------
st.set_page_config(
    page_title="Transfert IRM → NAS",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_ROOT = Path(__file__).parent.resolve()
DEMO_SOURCE = APP_ROOT / "sample_data" / "dicom_source"
DEMO_NAS = APP_ROOT / "sample_data" / "nas_target"
WORK_DIR = APP_ROOT / "workspace"
WORK_DIR.mkdir(exist_ok=True)


# ---------- Session state ----------
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


# ---------- Sidebar ----------
with st.sidebar:
    st.title("🧠 Transfert IRM → NAS")
    st.caption("Interface unifiée des scripts `animaux` & `copie_nas`")
    page = st.radio(
        "Étapes",
        [
            "① Configuration",
            "② Extraction des animaux",
            "③ Transfert vers le NAS",
            "④ Logs & vérification",
            "⌨️ Mode terminal",
            "ⓘ À propos",
        ],
        label_visibility="collapsed",
    )
    st.divider()
    if st.session_state.demo_mode:
        st.success("Mode démo actif", icon="🧪")
        st.caption(f"Source : `{DEMO_SOURCE.name}`")
        st.caption(f"Cible : `{DEMO_NAS.name}`")
    st.caption("Build interne CHR · MVP audit")


# ---------- Pages ----------
def page_config():
    st.header("① Configuration")
    st.markdown(
        "Définissez ici le **type de données**, le **dossier source** contenant les "
        "séquences IRM, et le **dossier cible** sur le NAS. Ces valeurs sont réutilisées "
        "dans les étapes suivantes."
    )

    col1, col2 = st.columns(2)
    with col1:
        st.session_state.source_type = st.radio(
            "Type de données",
            options=["dicom", "bruker"],
            index=0 if st.session_state.source_type == "dicom" else 1,
            horizontal=True,
            help="DICOM : noms commencent au champ 1. Bruker : au champ 3 (préfixe technique).",
        )
        st.session_state.nb_separateurs = st.number_input(
            "Nombre de séparateurs `_` dans le nom des animaux",
            min_value=0, max_value=10,
            value=st.session_state.nb_separateurs,
            help="Exemple : pour `F_2_4`, indiquer 2.",
        )

    with col2:
        st.session_state.source_dir = st.text_input(
            "Dossier source des séquences",
            value=st.session_state.source_dir,
            help="Sur le PC d'acquisition : ex. `/opt/PV6.0.1/DICOM-Laurent`",
        )
        st.session_state.nas_dir = st.text_input(
            "Dossier cible sur le NAS",
            value=st.session_state.nas_dir,
            help="Ex. `/opt/NASIRM/copitch`",
        )

    src = Path(st.session_state.source_dir) if st.session_state.source_dir else None
    nas = Path(st.session_state.nas_dir) if st.session_state.nas_dir else None

    with st.expander("Vérification des chemins", expanded=True):
        cols = st.columns(2)
        with cols[0]:
            if src and src.is_dir():
                n = sum(1 for _ in src.iterdir() if _.is_dir())
                st.success(f"Source OK — {n} sous-dossiers détectés")
            else:
                st.error("Source introuvable")
        with cols[1]:
            if nas and nas.is_dir():
                st.success("Cible accessible")
            elif nas:
                st.warning("Cible inexistante (sera créée lors du transfert)")
            else:
                st.error("Cible non renseignée")


def page_extract():
    st.header("② Extraction des noms d'animaux")
    st.markdown(
        "Équivalent du script `animaux`. Parcourt le dossier source, identifie les "
        "animaux uniques d'après le préfixe de chaque sous-dossier, et génère un `.txt`."
    )

    st.session_state.animals_file_name = st.text_input(
        "Nom du fichier de sortie (sans extension)",
        value=st.session_state.animals_file_name,
    )

    if st.button("🔍 Lancer l'extraction", type="primary"):
        try:
            res = extract_animals(
                Path(st.session_state.source_dir),
                st.session_state.source_type,
                int(st.session_state.nb_separateurs),
            )
        except Exception as e:
            st.error(f"Erreur : {e}")
            return

        st.session_state.extracted_animals = res.animals
        st.success(
            f"{len(res.animals)} animaux uniques extraits "
            f"sur {res.inspected_folders} dossiers inspectés."
        )
        if res.skipped_folders:
            with st.expander(f"⚠️ {len(res.skipped_folders)} dossiers ignorés (séparateurs insuffisants)"):
                st.code("\n".join(res.skipped_folders))

    if st.session_state.extracted_animals:
        st.subheader("Animaux détectés")
        df = pd.DataFrame({"#": range(1, len(st.session_state.extracted_animals) + 1),
                           "Animal": st.session_state.extracted_animals})
        st.dataframe(df, hide_index=True, use_container_width=True)

        # 🆕 Vérification PatientName — évite l'erreur "n'est pas le meme que le PatientID"
        with st.expander("🔍 Vérifier la correspondance PatientName (recommandé)", expanded=True):
            st.caption(
                "On lit le PatientName du premier DICOM de chaque animal et on compare "
                "au nom extrait. Si tout n'est pas ✅, ajustez `nb_separateurs` dans l'onglet ①."
            )
            if st.button("Lancer la vérification"):
                try:
                    src = Path(st.session_state.source_dir)
                    rows = check_patient_names(src, st.session_state.extracted_animals)
                    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                    n_ko = sum(1 for r in rows if r["OK ?"] == "❌")
                    if n_ko:
                        st.error(
                            f"{n_ko} animal(aux) ne correspondent pas au PatientName DICOM. "
                            "Le transfert affichera des erreurs pour ceux-là. "
                            "👉 Diminuez `nb_separateurs` dans l'onglet ① puis relancez l'extraction."
                        )
                    else:
                        st.success("Toutes les correspondances sont OK 🎉")
                except Exception as e:
                    st.error(f"Erreur : {e}")

        # Edition libre + écriture sur disque + téléchargement
        edited = st.text_area(
            "Vous pouvez éditer la liste avant export",
            value="\n".join(st.session_state.extracted_animals),
            height=200,
        )
        animals_clean = [l.strip() for l in edited.splitlines() if l.strip()]

        c1, c2 = st.columns(2)
        with c1:
            if st.button("💾 Sauvegarder sur disque"):
                out = WORK_DIR / f"{st.session_state.animals_file_name}.txt"
                write_animals_file(animals_clean, out)
                st.success(f"Fichier enregistré : `{out}`")
        with c2:
            st.download_button(
                "⬇️ Télécharger le .txt",
                data="\n".join(animals_clean) + "\n",
                file_name=f"{st.session_state.animals_file_name}.txt",
                mime="text/plain",
            )

        st.session_state.extracted_animals = animals_clean


def page_transfer():
    st.header("③ Transfert vers le NAS")
    st.markdown(
        "Équivalent du script `copie_nas`. Lit les métadonnées DICOM, "
        "reconstruit l'arborescence `{NAS}/{StudyDate}_{sujet}/{Time}/IRM/dicom/{sequence}/` "
        "et copie les images. Écrit un fichier `.log` dans le dossier cible."
    )

    # Source des animaux
    src_mode = st.radio(
        "Liste des animaux à traiter",
        ["Utiliser la liste extraite à l'étape ②", "Uploader un .txt", "Saisir manuellement"],
        horizontal=False,
    )
    animals: list[str] = []
    if src_mode.startswith("Utiliser"):
        animals = list(st.session_state.extracted_animals)
        if not animals:
            st.warning("Aucune liste en session — passez d'abord par l'étape ②.")
    elif src_mode.startswith("Uploader"):
        up = st.file_uploader("Fichier .txt (un animal par ligne)", type=["txt"])
        if up is not None:
            animals = [l.strip() for l in up.read().decode().splitlines() if l.strip()]
    else:
        txt = st.text_area("Un nom d'animal par ligne", height=160)
        animals = [l.strip() for l in txt.splitlines() if l.strip()]

    if animals:
        with st.expander(f"📋 {len(animals)} animaux dans la file", expanded=False):
            st.code("\n".join(animals))

    st.divider()
    dry_run = st.toggle(
        "Mode simulation (dry-run) — analyse sans copier les fichiers",
        value=False,
        help="Si activé, on lit les DICOM et on affiche ce qui serait fait, sans rien écrire.",
    )

    if st.button("🚀 Lancer le transfert", type="primary", disabled=not animals):
        progress = st.progress(0.0, text="Lecture des séquences...")
        logs_box = st.empty()
        lines: list[str] = []

        def cb(line: str):
            lines.append(line)
            # n'affiche que les 30 dernières lignes pour la fluidité
            logs_box.code("\n".join(lines[-30:]))

        try:
            if dry_run:
                # Simulation : on lit juste les 1ers DICOM de chaque dossier sujet
                source = Path(st.session_state.source_dir)
                for i, subject in enumerate(animals, 1):
                    progress.progress(i / len(animals), text=f"Analyse de {subject}...")
                    seqs = [p for p in source.iterdir()
                            if p.is_dir() and p.name.lower().startswith(subject.lower() + "_")]
                    cb(f"[DRY-RUN] {subject} → {len(seqs)} séquences détectées")
                    for s in seqs:
                        dcm = first_dicom_in(s)
                        if dcm:
                            m = read_meta(dcm)
                            cb(f"    └ {s.name} | StudyDate={m.study_date} "
                               f"Protocol={m.protocol_name} Modality={m.modality}")
                st.success("Simulation terminée. Aucune écriture effectuée.")
            else:
                stats = transfer_to_nas(
                    Path(st.session_state.source_dir),
                    Path(st.session_state.nas_dir),
                    animals,
                    log_callback=cb,
                )
                progress.progress(1.0, text="Terminé")
                st.session_state.transfer_stats = stats
                st.session_state.transfer_logs = lines

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Séquences traitées", stats.sequences_total)
                c2.metric("Séquences copiées", stats.sequences_copied)
                c3.metric("Fichiers DICOM copiés", stats.files_copied)
                c4.metric("Erreurs / Warnings", f"{len(stats.errors)} / {len(stats.warnings)}")

                if stats.new_animals:
                    st.info("Nouveaux animaux créés sur le NAS : " + ", ".join(stats.new_animals))
                if stats.new_timepoints:
                    st.info("Nouveaux temps d'acquisition : " + ", ".join(stats.new_timepoints))
                if stats.errors:
                    with st.expander(f"❌ {len(stats.errors)} erreurs"):
                        st.code("\n".join(stats.errors))
                if stats.warnings:
                    with st.expander(f"⚠️ {len(stats.warnings)} warnings"):
                        st.code("\n".join(stats.warnings))

                st.success("Transfert terminé.")
        except Exception as e:
            st.error(f"Erreur lors du transfert : {e}")


def page_logs():
    st.header("④ Logs & vérification")
    nas = Path(st.session_state.nas_dir) if st.session_state.nas_dir else None
    if not nas or not nas.exists():
        st.info("Aucun dossier NAS configuré ou inexistant.")
        return

    log_file = nas / f"{nas.name}.log"
    if log_file.exists():
        st.subheader(f"📄 `{log_file.name}`")
        content = log_file.read_text()
        warnings = [l for l in content.splitlines() if "[WARNING]" in l]
        errors = [l for l in content.splitlines() if "[ERROR]" in l]
        c1, c2 = st.columns(2)
        c1.metric("Warnings", len(warnings))
        c2.metric("Errors", len(errors))
        st.code(content or "(vide)", language="log")
        st.download_button("⬇️ Télécharger le .log", data=content,
                           file_name=log_file.name, mime="text/plain")
    else:
        st.info("Aucun fichier .log encore présent.")

    st.divider()
    st.subheader("🗂️ Arborescence créée")
    if nas.exists():
        rows = []
        for p in sorted(nas.rglob("*")):
            if p.is_dir():
                depth = len(p.relative_to(nas).parts)
                if depth <= 5:
                    rows.append({"Chemin": str(p.relative_to(nas)),
                                 "Profondeur": depth,
                                 "Fichiers": sum(1 for f in p.iterdir() if f.is_file())})
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.caption("Dossier vide.")


def page_cli():
    st.header("⌨️ Mode terminal — simulation des scripts bash")
    st.markdown(
        "Cette vue reproduit fidèlement l'expérience en ligne de commande "
        "telle que les chercheurs l'avaient avant cette interface : prompts identiques "
        "aux scripts `animaux` et `copie_nas`, exécutés depuis la **Konsole**. "
        "Pratique pour la formation, la transition, ou la démo audit."
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

    # Définition des prompts (identiques aux scripts bash)
    if is_script1:
        steps = [
            {"prompt": "Les données a copier sont-elles des données dicom ou bruker ?",
             "field": "type", "default": "dicom", "hint": "dicom / bruker"},
            {"prompt": "Dans quel sous dossier se trouve les noms des animaux a recuperer ? /opt/PV6.0.1/",
             "field": "sous_dossier", "default": "DICOM-Laurent",
             "hint": "ex. DICOM-Laurent"},
            {"prompt": "Combien de '_' comprend le nom des animaux ?",
             "field": "nb_sep", "default": "2", "hint": "ex. 2 pour F_2_4"},
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

    # Reconstruction de l'affichage type "Konsole"
    PROMPT = "[MATISSE@CZC6177SYC ~]$"
    lines = [f"{PROMPT} {cmd_name}"]
    for i, s in enumerate(steps):
        if i < state["step"]:
            ans = state["answers"].get(s["field"], "")
            lines.append(f"{s['prompt']} {ans}")
        elif i == state["step"] and not state["done"]:
            lines.append(s["prompt"])

    if state["done"]:
        # On termine par le retour du prompt et l'output éventuel
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
        st.success(f"✅ Script `{cmd_name}` terminé. Voir résultat ci-dessous.")
        if state["result"] is not None:
            if cmd_name == "animaux":
                st.subheader("Contenu du fichier .txt généré")
                st.code("\n".join(state["result"]) or "(vide)", language="text")
            else:
                stats = state["result"]
                c1, c2, c3 = st.columns(3)
                c1.metric("Séquences", stats.sequences_total)
                c2.metric("Fichiers copiés", stats.files_copied)
                c3.metric("Errors / Warnings", f"{len(stats.errors)} / {len(stats.warnings)}")
        if st.button("🔄 Recommencer", key=f"reset_{cmd_name}"):
            st.session_state[state_key] = {"step": 0, "answers": {}, "done": False, "result": None}
            st.rerun()
        return

    # Saisie en cours
    current = steps[state["step"]]
    with st.form(key=f"form_{cmd_name}_{state['step']}", clear_on_submit=True):
        val = st.text_input(
            f"Saisie ({current['hint']})" if current["hint"] else "Saisie",
            value="",
            placeholder=current["default"],
        )
        submitted = st.form_submit_button("⏎ Entrée")
        if submitted:
            val = val.strip() or current["default"]
            state["answers"][current["field"]] = val
            state["step"] += 1
            if state["step"] >= len(steps):
                # Exécution réelle — utilise les chemins config de l'onglet ①
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
                        # Met à jour la session pour le reste de l'app
                        st.session_state.extracted_animals = res.animals
                    else:
                        src = Path(st.session_state.source_dir or str(DEMO_SOURCE))
                        nas = Path(st.session_state.nas_dir or str(DEMO_NAS))
                        animals = list(st.session_state.extracted_animals or [])
                        if not animals:
                            state["result_lines"] = ["  → liste d'animaux vide, passez d'abord par 'animaux'"]
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


def page_about():
    st.header("ⓘ À propos")
    st.markdown(
        """
        **Transfert IRM → NAS** regroupe en une seule interface :
        - `creation_fichier_animaux.sh` → onglet ②
        - `creation_folder_group_dicom.sh` → onglet ③

        ### Différences avec les scripts originaux
        - **Multi-OS** : utilise `pydicom` au lieu de `dcmftest`/`dcmdump` (binaires Linux uniquement).
        - **Mode simulation** : on peut vérifier ce qui sera fait avant de copier.
        - **Édition libre** de la liste d'animaux avant transfert.
        - **Logs en temps réel** dans l'UI et écrits dans le fichier `.log` standard.
        - **Téléchargement** des `.txt` et `.log` directement depuis le navigateur.

        ### Limites connues
        - Le cas spécifique Bruker avec suffixe `_P{n}` (numéro de paravision) n'est pas
          implémenté à 100 % : on prend `ProtocolName` comme nom de séquence. À adapter si besoin.
        - Sur le déploiement cloud, les chemins source/cible doivent être accessibles
          par le serveur Streamlit (utilisable pour démo avec données embarquées).
        """
    )


PAGES = {
    "① Configuration": page_config,
    "② Extraction des animaux": page_extract,
    "③ Transfert vers le NAS": page_transfer,
    "④ Logs & vérification": page_logs,
    "⌨️ Mode terminal": page_cli,
    "ⓘ À propos": page_about,
}

PAGES[page]()
