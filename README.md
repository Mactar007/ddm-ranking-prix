# Classement Google + prix concurrents — DakarDiscountMarket

Chaque jour, pour ~70 produits en ligne sur dakardiscountmarket.com :
recherche Google (Serper), notre position, 2 concurrents sénégalais (Gemini),
lecture de leur fiche produit (Scrapling), correspondance et prix, puis une
ligne ajoutée dans le classeur Google Sheets de suivi.

- `ranking.py` : le script (détails en tête de fichier).
- `.github/workflows/ranking.yml` : lancement quotidien à 04h00 UTC, ou à la main
  (Actions → « Classement Google + prix concurrents » → Run workflow).
- Le curseur est dans l'onglet « Etat » du classeur : il n'avance qu'après chaque
  ligne écrite, un lancement interrompu ne saute aucun produit.

Secrets du dépôt : `SERPER_API_KEY`, `GEMINI_API_KEY`, `WIX_NEW_SITE_API_KEY`,
`WIX_NEW_SITE_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`.

Coût mesuré (29/09/2026) : Serper 1 crédit/produit ; Gemini 3.1 Flash-Lite gratuit
(~2,5 appels/produit, plafond 500/jour) ; GitHub Actions gratuit (dépôt public).
