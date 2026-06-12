# Transfert IRM → NAS

Interface web qui regroupe les deux scripts bash utilisés au CHR pour
transférer les données IRM (DICOM / Bruker) vers le NAS :

| Script bash original | Onglet dans l'app |
|---|---|
| `creation_fichier_animaux.sh` | ② Extraction des animaux |
| `creation_folder_group_dicom.sh` | ③ Transfert vers le NAS |

L'app remplace les binaires Linux `dcmftest` / `dcmdump` par
[`pydicom`](https://pydicom.github.io/), donc elle tourne sur **Linux,
macOS, Windows**, et se déploie facilement dans le cloud.

---

## 🚀 Lancer en local

### Prérequis
- Python 3.10+

### Installation

```bash
cd nas_transfer_app
python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### Démarrage

```bash
streamlit run app.py
```

Streamlit ouvre automatiquement `http://localhost:8501` dans le navigateur.

Le **mode démo** est activé tant que `sample_data/dicom_source/` existe :
les champs sont préremplis avec des séquences d'exemple.

---

## 🌐 Déploiement pour l'audit

### Option 1 — Streamlit Community Cloud (recommandée, gratuit, le plus simple)

1. Pousser ce dossier sur GitHub (repo public ou privé).
2. Aller sur https://share.streamlit.io → **New app**.
3. Sélectionner le repo, branche, et `app.py` comme fichier principal.
4. Cliquer **Deploy**. L'URL publique est générée (`https://<app-name>.streamlit.app`).

L'app sera live en ~2 min. C'est l'URL à partager pour l'audit.

### Option 2 — Railway

1. Créer un compte sur https://railway.app et installer la CLI : `npm i -g @railway/cli`.
2. Depuis le dossier `nas_transfer_app/` :
   ```bash
   railway login
   railway init
   railway up
   ```
3. Railway détecte le `Dockerfile` et déploie. Récupérer l'URL publique :
   ```bash
   railway domain
   ```

### Option 3 — Render / Fly.io / autre PaaS Docker

Le `Dockerfile` est standard, il fonctionne sur n'importe quel hébergeur
qui accepte un container exposant un port HTTP. Configurer le port via la
variable d'environnement `PORT`.

---

## 📁 Structure

```
nas_transfer_app/
├── app.py                         # Interface Streamlit (5 onglets)
├── core/
│   ├── animal_extractor.py        # logique du script 1
│   ├── dicom_transfer.py          # logique du script 2
│   └── dicom_reader.py            # wrapper pydicom (remplace dcmdump)
├── sample_data/
│   ├── dicom_source/              # 3 séquences démo (animal M_4_11)
│   └── nas_target/                # cible du transfert (vide au départ)
├── requirements.txt
├── Dockerfile                     # pour Railway / Fly.io / Render
├── railway.json
└── .streamlit/config.toml
```

---

## ⚙️ Workflow utilisateur

1. **Configuration** : type de données (DICOM/Bruker), dossier source,
   dossier cible NAS, nombre de séparateurs `_` dans les noms d'animaux.
2. **Extraction** : un clic → liste des animaux uniques détectés, éditable,
   exportable en `.txt`.
3. **Transfert** : un clic → reconstruit l'arborescence
   `{NAS}/{StudyDate}_{sujet}/{Time}/IRM/dicom/{séquence}/` et copie les DICOM.
   Mode simulation (dry-run) disponible.
4. **Logs** : visualisation du `.log` avec décompte des warnings / erreurs,
   et arborescence créée sur le NAS.

---

## ⚠️ Notes pour la production

- **Chemins source / cible** : en déploiement cloud, les chemins saisis
  doivent être accessibles par le serveur. Pour utiliser cette app sur les
  vraies données du CHR, la **lancer en local sur le PC d'acquisition** ;
  utiliser le cloud uniquement pour la **démonstration** durant l'audit
  (mode démo avec données embarquées).
- Le cas Bruker `_P{n}` (numéro paravision) du script original simplifie le
  nom de séquence à `ProtocolName` ; à raffiner si besoin.
- Aucune authentification : si l'app est exposée publiquement, la protéger
  via le mot de passe Streamlit Cloud ou un reverse proxy.
