# DADA IA

Studio de génération de visuels assistée par l’API OpenAI, avec préparation et export PSD.

## Démarrage
1. Installer Python 3.11+.
2. Copier `.env.example` vers `.env`.
3. Renseigner `OPENAI_API_KEY` avec une clé OpenAI active.
4. Installer les dépendances : `pip install -r requirements.txt`
5. Lancer le serveur : `uvicorn backend.main:app --reload`
6. Ouvrir http://127.0.0.1:8000

### Lancement avec Docker
```bash
docker build -t dada-ia .
docker run --env-file .env -p 8000:8000 -v dada-data:/app/outputs dada-ia
```

Pour un déploiement HTTPS, utiliser `DADA_COOKIE_SECURE=1`.

## Compte utilisateur
La création d’un compte est obligatoire avant l’accès au studio. Les comptes et les sessions sont stockés dans la base locale `dada.db`, créée automatiquement au premier lancement.

## Configuration OpenAI
Les variables sont définies dans `.env` :

```env
OPENAI_API_KEY=sk-...
OPENAI_IMAGE_MODEL=gpt-image-1
DATABASE_URL=
DADA_COOKIE_SECURE=0
DADA_COOKIE_SAMESITE=lax
DADA_FREE_GENERATIONS_PER_DAY=10
DADA_ALLOWED_ORIGINS=capacitor://localhost,https://app.example.com
```

La clé API reste côté serveur et ne doit jamais être ajoutée dans `frontend/index.html`.

## Formats de sortie
Depuis le studio, l’utilisateur peut choisir le format principal à télécharger : `PNG`, `JPEG` ou `PSD`. Le JPEG est un export aplati. Le PNG peut être retouché dans Photoshop comme image. Le PSD sépare le visuel et les textes en calques raster, ce qui permet de les déplacer et de les retoucher comme images, mais pas de modifier le texte comme un calque texte natif. Un aperçu PNG est conservé pour l’affichage dans l’historique.

## Pipeline DADA IA
Le pipeline suit cette structure : prompt utilisateur, génération IA, préparation de la composition, puis PSD Builder. Le PSD produit les calques `Background`, `Objets` et `Text Layer`. Pour le moment, `Objets` contient encore le rendu raster complet ; la segmentation automatique des objets et l’OCR des textes sont les prochaines étapes nécessaires pour obtenir des calques réellement séparés.

## Important
La génération d’image produit une image raster. Le module PSD reconstruit ensuite une composition à calques raster : arrière-plan, visuel généré et textes. Les fichiers produits sont enregistrés dans `outputs/`.

En production HTTPS, définir `DADA_COOKIE_SECURE=1`.

## Application iOS avec Capacitor
Prérequis : macOS, Node.js, Xcode et un compte Apple Developer. Remplacer `https://api.example.com` dans `frontend/mobile-config.js` par l’URL HTTPS publique de l’API, puis exécuter sur macOS :

```bash
npm install
npx cap add ios
npx cap sync ios
npx cap open ios
```

Dans Xcode : sélectionner l’équipe Apple Developer, vérifier le bundle ID `com.dadaia.studio`, choisir un iPhone ou `Any iOS Device`, puis utiliser `Product > Archive`. Envoyer l’archive dans App Store Connect et ajouter les testeurs depuis TestFlight.

L’API doit être déployée avant l’app iOS. Elle doit être accessible en HTTPS, avec `DADA_COOKIE_SECURE=1`, `DADA_COOKIE_SAMESITE=none` et `DADA_ALLOWED_ORIGINS` contenant `capacitor://localhost` et le domaine web autorisé. En local, conserver `DADA_COOKIE_SECURE=0` et `DADA_COOKIE_SAMESITE=lax`.

## APK Android avec Capacitor
Prérequis : Node.js LTS, Android Studio, Android SDK et Java 17. Depuis la racine du projet :

```bash
npm install
npm run android:add
npm run android:sync
npm run android:build
```

L’APK de debug sera généré dans `android/app/build/outputs/apk/debug/app-debug.apk`. Pour ouvrir le projet dans Android Studio : `npm run cap:android`. L’URL HTTPS de l’API doit être configurée dans `frontend/mobile-config.js` avant la compilation.

## Production
Le mode local utilise SQLite avec `DADA_DATABASE=dada.db`. En production, si `DATABASE_URL` est défini, DADA IA utilise PostgreSQL automatiquement. Le fichier `render.yaml` provisionne une base PostgreSQL managée et la relie au service web.

Render fournit automatiquement HTTPS sur l’URL `onrender.com` du service. Après déploiement, ajouter cette URL à `DADA_ALLOWED_ORIGINS`, puis utiliser l’URL HTTPS de l’API dans `frontend/mobile-config.js`. Pour un domaine personnalisé, activer le domaine dans Render et attendre la validation du certificat TLS.

Le quota par défaut est de 10 générations OpenAI par utilisateur et par jour. Il est réglable avec `DADA_FREE_GENERATIONS_PER_DAY`.

Le stockage des images reste local au conteneur. Pour une production durable, monter un disque persistant ou utiliser un stockage objet (S3/R2) pour `outputs/`.
