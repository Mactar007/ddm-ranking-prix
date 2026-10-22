"""Suivi du classement Google et des prix concurrents — DakarDiscountMarket.

Pour chaque produit visible du site (Wix Stores V3, par ordre de création), hors catégories
exclues (EXCLURE_CATEGORIES) et, si INCLURE_CATEGORIES est rempli, seulement dans ces catégories :
  1. recherche Google via Serper (« <nom> Sénégal », Google Sénégal, en français) ;
  2. notre position calculée directement dans les résultats (sans IA) ;
  3. Gemini choisit au plus 2 concurrents opérant au Sénégal (lien exact d'une fiche produit) ;
  4. Scrapling lit la page de chaque concurrent (mode furtif si nécessaire) ;
  5. Gemini juge la correspondance (identique / similaire / aucun_rapport) ; le prix est lu
     en priorité dans les données structurées de la page ;
  6. une ligne est ajoutée dans Google Sheets (colonnes A→O).

Le curseur (date de création du dernier produit traité) est gardé dans l'onglet « Etat »
du classeur, une ligne par onglet de destination, et n'avance qu'après l'écriture de la
ligne du produit : un lancement interrompu ne saute aucun produit. Si Serper ou Gemini
ne répondent plus (quota du jour atteint), le lancement s'arrête proprement et le
suivant reprend au même endroit.

Les journaux ne contiennent que des compteurs (dépôt public) : les résultats sont dans
le classeur.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

SHEET_ID = os.environ.get('SHEET_ID', '1cl1UpzIIBRMjOZ9nOxXQ2FoNAZNzTPYxsVHjrGGBQiA')
ONGLET = os.environ.get('ONGLET') or 'Feuille 1'
ONGLET_ETAT = 'Etat'
MODELE = os.environ.get('GEMINI_MODEL') or 'gemini-3.1-flash-lite'
LIMITE = int(os.environ.get('LIMITE') or 70)
# Gemini gratuit : 15 requêtes/minute sur le projet → au plus une toutes les 4,5 s.
PAUSE_GEMINI = float(os.environ.get('PAUSE_GEMINI') or 4.5)
DEBUT = '2000-01-01T00:00:00Z'
# Catégories Wix dont les produits sont sautés (ids séparés par des virgules) — le gros
# électroménager (772 fiches sur 2157) occupait presque tous les lancements, demande
# Mac007 du 02/10 : il garde son onglet « Feuille 1 », les autres catégories vont ailleurs.
EXCLURE = {i.strip() for i in (os.environ.get('EXCLURE_CATEGORIES') or '').split(',') if i.strip()}
# Catégories suivies seules (vide = toutes) — Mac007 13/10 : rayons prioritaires seulement
# (Sport), pour économiser les crédits Serper.
INCLURE = {i.strip() for i in (os.environ.get('INCLURE_CATEGORIES') or '').split(',') if i.strip()}
# Lancements automatiques (Mac007 22/10) : on alterne Sport → gros électroménager → Sport…
# Chaque rayon garde son onglet et son curseur ; on change de rayon quand son tour du
# catalogue est terminé. Le rayon en cours est noté sur la ligne « Phase » de l'onglet « Etat ».
ALTERNANCE = os.environ.get('ALTERNANCE') == '1'
PHASES = {
    'Sport': ('Autres catégories', os.environ.get('SPORT_IDS') or ''),
    'Gros électroménager': ('Feuille 1', os.environ.get('GROS_IDS') or ''),
}
PHASE_SUIVANTE = {'Sport': 'Gros électroménager', 'Gros électroménager': 'Sport'}

NOS_DOMAINES = ('dakardiscountmarket.com', 'dakardiscount.com')
RESEAUX = ('facebook.', 'instagram.', 'tiktok.', 'youtube.', 'twitter.', 'x.com', 'pinterest.', 'linkedin.')
# Sites qui bloquent la lecture simple (mesuré le 29/09/2026 : Jumia répond 403).
DOMAINES_FURTIFS = ('jumia.sn',)
# Sites qui n'ont pas répondu (délai dépassé) pendant ce lancement : on ne les relit plus
# jusqu'au lancement suivant (tech-access-dakar.com, 21/10 : ~4 min perdues par page).
INJOIGNABLES = set()
EN_TETES = ['Produit', 'Notre position', 'Notre prix',
            'Concurrent1 lien', 'Concurrent1 position', 'Concurrent1 prix', 'Concurrent1 correspondance', 'Concurrent1 justification',
            'Concurrent2 lien', 'Concurrent2 position', 'Concurrent2 prix', 'Concurrent2 correspondance', 'Concurrent2 justification',
            'Date', 'Notre lien']

PROMPT_CONCURRENTS = """Voici les resultats organiques d'une recherche Google (JSON) pour le produit "{{NOM}}" : {{RESULTATS}}

Retourne UNIQUEMENT un objet JSON de cette forme : {"concurrents": [{"url": "...", "position": <entier>}, {"url": "...", "position": <entier>}]}

Regles : exclure dakardiscountmarket.com et dakardiscount.com (c'est notre site). Ne considerer que des sites qui semblent reellement operer/livrer au Senegal : domaine en .sn, ou mention explicite de Dakar/Senegal/livraison locale/FCFA dans le titre ou l'extrait. Exclure les sites generiques internationaux sans lien apparent avec le Senegal (Amazon, AliExpress, eBay, sites europeens, etc.). Le champ "url" doit etre EXACTEMENT le champ "lien" du resultat, et ce lien doit etre une PAGE PRODUIT (la fiche d'un article precis). Exclure : pages d'accueil, pages categorie/rayon/collection, pages de recherche ou de listing, articles de blog/actualites, reseaux sociaux, annuaires et petites annonces generalistes. Garder au maximum les 2 meilleurs concurrents, dans l'ordre de position ; tableau plus court ou vide s'il n'y en a pas assez. Ne jamais inventer ni reconstruire une URL."""

PROMPT_COMPARAISON = """Voici le contenu texte d'une page web, a comparer au produit "{{NOM}}" de DakarDiscountMarket : {{TEXTE}}

Retourne UNIQUEMENT un objet JSON de cette forme : {"prix": <nombre en FCFA sans espace ni symbole, ou null si introuvable>, "correspondance": "identique" ou "similaire" ou "aucun_rapport", "justification": "..."}

Criteres de correspondance :
- "identique" : meme produit, meme marque et modele (ou variante triviale : couleur/taille), directement comparable en prix.
- "similaire" : meme categorie de produit et fonction proche, mais marque ou modele different -- comparaison de prix seulement indicative.
- "aucun_rapport" : produit different, page qui ne vend pas ce type de produit, OU page qui n'est pas une fiche produit unique (page d'accueil, categorie, listing, blog, reseau social, page indisponible).

"justification" : une phrase courte en francais expliquant le choix."""


class ArretLancement(Exception):
    """Serper ou Gemini indisponible (quota du jour) : on s'arrête sans avancer le curseur."""


def http_json(url, corps=None, en_tetes=None, methode=None, essais=5):
    donnees = json.dumps(corps).encode() if corps is not None else None
    for essai in range(essais):
        requete = urllib.request.Request(url, data=donnees, method=methode or ('POST' if donnees else 'GET'),
                                         headers={'Content-Type': 'application/json', **(en_tetes or {})})
        try:
            with urllib.request.urlopen(requete, timeout=60) as reponse:
                texte = reponse.read()
                return json.loads(texte) if texte else {}
        except urllib.error.HTTPError as erreur:
            if erreur.code in (429, 500, 502, 503, 504) and essai < essais - 1:
                time.sleep(15 * (essai + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if essai < essais - 1:
                time.sleep(10 * (essai + 1))
                continue
            raise


class Classeur:
    def __init__(self):
        infos = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
        self.identifiants = service_account.Credentials.from_service_account_info(
            infos, scopes=['https://www.googleapis.com/auth/spreadsheets'])

    def _en_tetes(self):
        if not self.identifiants.valid:
            self.identifiants.refresh(GoogleRequest())
        return {'Authorization': f'Bearer {self.identifiants.token}'}

    def _url(self, suite):
        return f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{suite}'

    def onglets(self):
        reponse = http_json(self._url('?fields=sheets.properties.title'), en_tetes=self._en_tetes())
        return [s['properties']['title'] for s in reponse['sheets']]

    def creer_onglet(self, titre):
        http_json(self._url(':batchUpdate'), {'requests': [{'addSheet': {'properties': {'title': titre}}}]}, self._en_tetes())

    def lire(self, plage):
        return http_json(self._url(f'/values/{urllib.parse.quote(plage, safe="")}'), en_tetes=self._en_tetes()).get('values', [])

    def ecrire(self, plage, valeurs):
        http_json(self._url(f'/values/{urllib.parse.quote(plage, safe="")}?valueInputOption=RAW'),
                  {'values': valeurs}, self._en_tetes(), methode='PUT')

    def ajouter(self, plage, ligne):
        http_json(self._url(f'/values/{urllib.parse.quote(plage, safe="")}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS'),
                  {'values': [ligne]}, self._en_tetes())


class Curseur:
    """Une ligne par onglet de destination dans l'onglet « Etat » : A = onglet, B = curseur, C = mise à jour."""

    def __init__(self, classeur):
        self.classeur = classeur
        if ONGLET_ETAT not in classeur.onglets():
            classeur.creer_onglet(ONGLET_ETAT)
            classeur.ecrire(f'{ONGLET_ETAT}!A1:C1', [['Onglet', 'Dernière date de création traitée', 'Mis à jour le']])
        lignes = classeur.lire(f'{ONGLET_ETAT}!A:C')
        self.numero = next((i + 1 for i, l in enumerate(lignes) if l and l[0] == ONGLET), len(lignes) + 1)
        existante = lignes[self.numero - 1] if self.numero <= len(lignes) else []
        self.valeur = existante[1] if len(existante) > 1 and existante[1] else DEBUT

    def avancer(self, valeur):
        """Avance sans écrire dans le classeur (produits sautés) ; écrit au prochain enregistrer()."""
        self.valeur = valeur

    def enregistrer(self, valeur):
        self.valeur = valeur
        maintenant = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        self.classeur.ecrire(f'{ONGLET_ETAT}!A{self.numero}:C{self.numero}', [[ONGLET, valeur, maintenant]])


def domaine(url):
    return urllib.parse.urlparse(url).netloc.lower().removeprefix('www.')


def est_notre_site(url):
    d = domaine(url)
    return any(d == nom or d.endswith('.' + nom) for nom in NOS_DOMAINES)


def produits(apres, nombre):
    reponse = http_json('https://www.wixapis.com/stores/v3/products/query',
                        {'query': {'filter': {'visible': True, 'createdDate': {'$gt': apres}},
                                   'sort': [{'fieldName': 'createdDate', 'order': 'ASC'}],
                                   'cursorPaging': {'limit': nombre}},
                         'fields': ['ALL_CATEGORIES_INFO']},
                        {'Authorization': os.environ['WIX_NEW_SITE_API_KEY'], 'wix-site-id': os.environ['WIX_NEW_SITE_ID']})
    return reponse.get('products', [])


def recherche_google(nom):
    try:
        reponse = http_json('https://google.serper.dev/search', {'q': f'{nom} Sénégal', 'gl': 'sn', 'hl': 'fr'},
                            {'X-API-KEY': os.environ['SERPER_API_KEY']})
    except Exception as erreur:
        raise ArretLancement(f'Serper indisponible ({erreur})')
    return [{'position': r.get('position'), 'titre': r.get('title'), 'lien': r.get('link'), 'extrait': r.get('snippet')}
            for r in reponse.get('organic', [])]


_dernier_appel_gemini = [0.0]


def _gemini(invite):
    attente = PAUSE_GEMINI - (time.time() - _dernier_appel_gemini[0])
    if attente > 0:
        time.sleep(attente)
    _dernier_appel_gemini[0] = time.time()
    reponse = http_json(f'https://generativelanguage.googleapis.com/v1beta/models/{MODELE}:generateContent?key={os.environ["GEMINI_API_KEY"]}',
                        {'contents': [{'role': 'user', 'parts': [{'text': invite}]}],
                         'generationConfig': {'responseMimeType': 'application/json'}})
    return json.loads(reponse['candidates'][0]['content']['parts'][0]['text'])


def _deepseek(invite):
    """Secours payant à l'usage (quasi gratuit, ~0,001 $ par appel) : même modèle et
    réglages que la lecture des photos fournisseur (validé le 10/10)."""
    reponse = http_json('https://api.deepseek.com/chat/completions',
                        {'model': 'deepseek-flash', 'temperature': 0, 'reasoning_effort': 'low',
                         'response_format': {'type': 'json_object'},
                         'messages': [{'role': 'user', 'content': invite + '\n\nRéponds uniquement avec le JSON demandé.'}]},
                        {'Authorization': f'Bearer {os.environ["DEEPSEEK_API_KEY"]}'})
    texte = reponse['choices'][0]['message']['content']
    return json.loads(texte[texte.index('{'):texte.rindex('}') + 1])


# Gemini (quota gratuit) d'abord ; s'il échoue (quota du jour, 503 persistant…),
# DeepSeek prend le relais pour le reste du lancement (Mac007 12/10).
_secours = {'actif': False, 'appels': 0}


def gemini(invite):
    if not _secours['actif']:
        try:
            return _gemini(invite)
        except Exception as erreur:
            if not os.environ.get('DEEPSEEK_API_KEY'):
                raise
            print(f'Gemini indisponible ({erreur}) → bascule sur DeepSeek', flush=True)
            _secours['actif'] = True
    _secours['appels'] += 1
    return _deepseek(invite)


def _offres(donnees):
    if isinstance(donnees, list):
        for element in donnees:
            yield from _offres(element)
    elif isinstance(donnees, dict):
        if 'price' in donnees or 'lowPrice' in donnees:
            yield donnees
        for valeur in donnees.values():
            if isinstance(valeur, (dict, list)):
                yield from _offres(valeur)


def prix_structure(page):
    """Prix des données structurées (JSON-LD), seulement s'il est en FCFA (ou sans devise)."""
    for bloc in page.css('script[type="application/ld+json"]::text').getall():
        try:
            donnees = json.loads(str(bloc))
        except ValueError:
            continue
        for offre in _offres(donnees):
            devise = str(offre.get('priceCurrency') or '').upper()
            if devise not in ('', 'XOF', 'CFA', 'FCFA'):
                continue
            try:
                valeur = float(str(offre.get('price') or offre.get('lowPrice')).replace(' ', '').replace(',', '.'))
            except ValueError:
                continue
            if valeur >= 500:
                return int(valeur)
    return None


def lire_page(url):
    from scrapling.fetchers import Fetcher, StealthyFetcher
    page = None
    if not domaine(url).endswith(DOMAINES_FURTIFS):
        try:
            page = Fetcher.get(url, stealthy_headers=True, timeout=25)
            if page.status >= 400 or len(str(page.get_all_text(ignore_tags=('script', 'style')))) < 500:
                page = None
        except Exception:
            page = None
    if page is None:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=60000)
    texte = str(page.get_all_text(ignore_tags=('script', 'style')))
    # Beaucoup de boutiques placent un long menu avant la fiche (nova.sn : ~6 000
    # caractères) : on commence le texte au titre du produit (H1), sinon le prix
    # et la description tombent au-delà de ce qu'on envoie à Gemini.
    titre = str((page.css('h1::text').getall() or [''])[0]).strip()
    if len(titre) >= 8:
        position = texte.find(titre[:40], 100)
        if position > 0:
            texte = texte[position:]
    return texte, prix_structure(page)


def semble_senegalais(url, texte):
    return domaine(url).endswith('.sn') or re.search(r'F\s?CFA|XOF|Dakar|S[ée]n[ée]gal', texte, re.I) is not None


def traiter(produit, compteurs):
    organiques = recherche_google(produit['name'])
    notre_position = next((r['position'] for r in organiques if est_notre_site(r.get('lien') or '')), '')
    liens = {r['lien']: r['position'] for r in organiques if r.get('lien')}
    try:
        choix = gemini(PROMPT_CONCURRENTS.replace('{{NOM}}', produit['name'])
                       .replace('{{RESULTATS}}', json.dumps(organiques, ensure_ascii=False)))
    except Exception as erreur:
        raise ArretLancement(f'IA indisponible, Gemini puis DeepSeek ({erreur})')

    candidats = []
    for choisi in choix.get('concurrents') or []:
        url = str(choisi.get('url') or '').strip()
        if (url not in liens or est_notre_site(url) or not re.match(r'^https?://[^/]+/.+', url)
                or any(r in domaine(url) for r in RESEAUX) or url in candidats):
            continue
        candidats.append(url)
        if len(candidats) == 2:
            break

    concurrents = []
    for url in candidats:
        if domaine(url) in INJOIGNABLES:
            compteurs['sites injoignables sautés'] += 1
            continue
        try:
            texte, prix_page = lire_page(url)
        except Exception as erreur:
            compteurs['pages illisibles'] += 1
            if re.search(r'timed out|timeout', str(erreur), re.I):
                INJOIGNABLES.add(domaine(url))
            continue
        if not semble_senegalais(url, texte):
            compteurs['hors Sénégal écartés'] += 1
            continue
        try:
            avis = gemini(PROMPT_COMPARAISON.replace('{{NOM}}', produit['name']).replace('{{TEXTE}}', texte[:6000]))
        except Exception:
            compteurs['comparaisons échouées'] += 1
            continue
        if avis.get('correspondance') not in ('identique', 'similaire'):
            compteurs['sans rapport écartés'] += 1
            continue
        concurrents.append([url, liens[url], prix_page or avis.get('prix') or '', avis['correspondance'], avis.get('justification') or ''])

    compteurs['concurrents trouvés'] += len(concurrents)
    while len(concurrents) < 2:
        concurrents.append([''] * 5)
    notre_prix = (produit.get('actualPriceRange') or {}).get('minValue', {}).get('amount', '')
    return ([produit['name'], notre_position, notre_prix] + concurrents[0] + concurrents[1]
            + [date.today().isoformat(), f"https://dakardiscountmarket.com/product/{produit['slug']}"])


def ligne_phase(classeur):
    lignes = classeur.lire(f'{ONGLET_ETAT}!A:C')
    numero = next((i + 1 for i, l in enumerate(lignes) if l and l[0] == 'Phase'), len(lignes) + 1)
    existante = lignes[numero - 1] if numero <= len(lignes) else []
    phase = existante[1] if len(existante) > 1 and existante[1] in PHASES else 'Sport'
    return numero, phase


def choisir_phase(classeur):
    global ONGLET, INCLURE, EXCLURE
    _, phase = ligne_phase(classeur)
    ONGLET, ids = PHASES[phase]
    INCLURE = {i.strip() for i in ids.split(',') if i.strip()}
    EXCLURE = set()
    print(f'Rayon en cours : {phase} (onglet « {ONGLET} »)')
    return phase


def passer_a_la_phase_suivante(classeur, phase):
    numero, _ = ligne_phase(classeur)
    suivante = PHASE_SUIVANTE[phase]
    maintenant = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    classeur.ecrire(f'{ONGLET_ETAT}!A{numero}:C{numero}', [['Phase', suivante, maintenant]])
    print(f'Tour du rayon {phase} terminé : prochain lancement sur {suivante}')


def main():
    from collections import Counter
    compteurs = Counter()
    classeur = Classeur()
    phase = choisir_phase(classeur) if ALTERNANCE else None
    if ONGLET not in classeur.onglets():
        classeur.creer_onglet(ONGLET)
        classeur.ecrire(f"'{ONGLET}'!A1:O1", [EN_TETES])
    curseur = Curseur(classeur)
    debut = time.time()
    try:
        while compteurs['produits'] < LIMITE:
            demande = 100
            lot = produits(curseur.valeur, demande)
            complet = True
            for produit in lot:
                if compteurs['produits'] >= LIMITE:
                    complet = False
                    break
                categories = {c.get('id') for c in (produit.get('allCategoriesInfo') or {}).get('categories', [])}
                if categories & EXCLURE or (INCLURE and not categories & INCLURE):
                    curseur.avancer(produit['createdDate'])
                    compteurs['produits sautés (catégorie exclue)'] += 1
                    continue
                ligne = traiter(produit, compteurs)
                classeur.ajouter(f"'{ONGLET}'!A:O", ligne)
                curseur.enregistrer(produit['createdDate'])
                compteurs['produits'] += 1
                compteurs['notre site dans le top 10'] += ligne[1] != ''
            if complet and len(lot) < demande:
                compteurs['tours du catalogue terminés'] += 1
                curseur.enregistrer(DEBUT)
                break
        if compteurs['produits sautés (catégorie exclue)']:
            curseur.enregistrer(curseur.valeur)
    except ArretLancement as raison:
        print(f'Arrêt anticipé, reprise au prochain lancement : {raison}')
    if phase and compteurs['tours du catalogue terminés']:
        passer_a_la_phase_suivante(classeur, phase)
    duree = time.time() - debut
    print(f"Onglet « {ONGLET} », modèle {MODELE} : {compteurs['produits']} produit(s) en {duree / 60:.1f} min"
          f" ({duree / max(compteurs['produits'], 1):.1f} s/produit)")
    for cle, valeur in sorted(compteurs.items()):
        print(f'  {cle} : {valeur}')
    if _secours['appels']:
        print(f"  appels DeepSeek (secours) : {_secours['appels']}")


if __name__ == '__main__':
    main()
