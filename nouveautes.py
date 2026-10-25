"""Veille hebdomadaire des nouveautés — DakarDiscountMarket (Mac007 25/10).

Sans crédit Serper (Scrapling + curl_cffi uniquement), une fois par semaine :
  1. Jumia SN : derniers articles mis en ligne (tri « nouveautés ») pour chaque famille de produits ;
  2. AliExpress : meilleures ventes (tri par commandes) pour chaque famille, avec le nombre de ventes
     et la note ; prix converti en F CFA à titre indicatif (hors transport et douane) ;
  3. TikTok Sénégal : vidéos « arrivage / nouveauté » de boutiques de Dakar dont la légende donne un prix.

Une ligne par article dans l'onglet « Nouveautés & tendances » du classeur du suivi des prix (mêmes colonnes
que les relevés manuels), sans doublon d'une semaine à l'autre (lien déjà présent = ignoré). Résumé sur Telegram.
Les journaux ne contiennent que des compteurs (dépôt public).
"""
import json
import re
import urllib.parse
from datetime import date

from curl_cffi import requests as cffi
from scrapling.fetchers import StealthySession

from ranking import Classeur
from veille import telegram

ONGLET = 'Nouveautés & tendances'
EN_TETE = ['Date', 'Famille', 'Article / tendance', 'Au catalogue ?', 'Notre prix', 'Prix min marché', 'Prix max marché',
           'Nb offres', 'Vendeurs (prix)', 'Liens', 'Source', 'Signal', 'Action proposée', 'Décision Mac007']
# Famille : (recherches Jumia SN, recherches AliExpress en anglais). Deux recherches par famille au plus : ~25 min en tout.
FAMILLES = {
    'Sport & fitness': (['fitness', 'football entrainement'], ['home gym fitness', 'football training equipment']),
    'Petit électroménager': (['cuisine electrique', 'ventilateur rechargeable'], ['kitchen appliance', 'rechargeable fan']),
    'Téléphonie & accessoires': (['accessoire smartphone', 'ecouteurs bluetooth'], ['phone accessories', 'wireless earbuds']),
    'Création vidéo / selfie': (['ring light', 'micro sans fil'], ['selfie light', 'wireless lavalier microphone']),
    'Maison & déco': (['decoration maison', 'rangement'], ['home decor', 'storage organizer']),
    'Beauté & bien-être': (['massage', 'soin visage appareil'], ['massager', 'facial beauty device']),
    'Enfants & jouets': (['jouet enfant'], ['kids toys']),
    'Bureau & informatique': (['accessoire ordinateur'], ['laptop accessories']),
}
# Recherches orientées produits : « nouvel arrivage » seul remonte surtout des vêtements sans prix (essai 25/10).
TIKTOK = ['nouveau gadget dakar prix', 'arrivage électroménager dakar prix', 'nouveauté accessoire dakar fcfa']
PAR_RECHERCHE = 5          # articles gardés par recherche
VENTES_MIN = 1000          # AliExpress : au moins « 1K+ sold »
TAUX = {'£': 760, '€': 656, '$': 605}  # conversion indicative en F CFA
PRIX_TIKTOK = re.compile(r'(\d{1,3}(?:[ .]\d{3})+|\d{4,6}|\d{1,3}(?:[.,]\d)?\s?k)\s?(?:f\b|fr\b|frs\b|fcfa|f cfa|cfa|mil\b)', re.I)


def jumia(session, recherche):
    page = session.fetch('https://www.jumia.sn/catalog/?q=' + urllib.parse.quote_plus(recherche) + '&sort=newest')
    sortie = []
    for carte in page.css('article.prd')[:PAR_RECHERCHE]:
        nom, prix, lien = carte.css('.name'), carte.css('.prc'), carte.css('a.core')
        montant = re.sub(r'\D', '', prix[0].text) if prix else ''
        if nom and lien and montant:
            sortie.append({'article': nom[0].text.strip()[:120], 'prix': int(montant), 'vendeur': 'Jumia SN',
                           'url': 'https://www.jumia.sn' + lien[0].attrib.get('href', ''), 'signal': 'Nouveau sur Jumia SN'})
    return sortie


def ventes(texte):
    m = re.search(r'([\d.,]+)\s*(K)?\+?\s*(?:sold|vendus?)', texte, re.I)
    if not m:
        return 0
    if m.group(2):  # « 1.5K+ sold »
        return int(float(m.group(1).replace(',', '.')) * 1000)
    return int(re.sub(r'[.,]', '', m.group(1)))  # « 5,000+ sold »


def aliexpress(session, recherche):
    page = session.fetch('https://www.aliexpress.com/w/wholesale-' + urllib.parse.quote(recherche.replace(' ', '-')) +
                         '.html?SortType=total_tranpro_desc', network_idle=True)
    sortie, vus = [], set()
    for a in page.css('a[href*="/item/"]'):
        # Carte : « titre ￡14.67 ￡ 14 . 67 ￡42.93 -65% 4.8 5,000+ sold … » (symboles pleine largeur selon le pays)
        texte = ' '.join(a.get_all_text(separator=' ').split()).translate(str.maketrans('￡＄€', '£$€'))
        ident = re.search(r'/item/(\d+)', a.attrib.get('href', ''))
        n = ventes(texte)
        prix = re.search(r'(US\s?\$|[£€$])\s?(\d[\d,]*(?:\.\d+)?)', texte)
        if not ident or ident.group(1) in vus or n < VENTES_MIN or not prix:
            continue
        vus.add(ident.group(1))
        titre = texte[:prix.start()].strip()[:120]
        note = re.search(r'(\d\.\d)\s+[\d.,]+\s*K?\+?\s*(?:sold|vendus?)', texte, re.I)
        symbole = '$' if '$' in prix.group(1) else prix.group(1)
        f_cfa = round(float(prix.group(2).replace(',', '')) * TAUX[symbole] / 50) * 50
        sortie.append({'article': titre, 'prix': f_cfa, 'vendeur': 'AliExpress',
                       'url': f'https://www.aliexpress.com/item/{ident.group(1)}.html',
                       'signal': f"{n:,}+ ventes".replace(',', ' ') + (f", note {note.group(1)}" if note else '') +
                                 ' ; prix Chine ≈ F CFA hors transport'})
        if len(sortie) >= PAR_RECHERCHE:
            break
    return sortie


def tiktok(session, recherche, http):
    page = session.fetch('https://www.tiktok.com/search?q=' + urllib.parse.quote(recherche), network_idle=True)
    sortie = []
    for url in list(dict.fromkeys(re.findall(r'https://www\.tiktok\.com/@[^"/]+/video/\d+', page.html_content)))[:8]:
        try:
            r = http.get(url, timeout=30)
            m = re.search(r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', r.text, re.S)
            it = json.loads(m.group(1))['__DEFAULT_SCOPE__']['webapp.video-detail']['itemInfo']['itemStruct']
        except Exception:
            continue
        legende = ' '.join((it.get('desc') or '').split())
        prix = PRIX_TIKTOK.search(legende)
        if not prix or it.get('locationCreated') not in (None, '', 'SN'):
            continue
        brut = prix.group(1).lower().replace(' ', '').replace('.', '')
        montant = int(float(brut.rstrip('k').replace(',', '.')) * 1000) if brut.endswith('k') else int(re.sub(r'\D', '', brut))
        if not 500 <= montant <= 5_000_000:
            continue
        sortie.append({'article': re.sub(r'#\S+', '', legende).strip()[:120], 'prix': montant,
                       'vendeur': '@' + it.get('author', {}).get('uniqueId', ''), 'url': url,
                       'signal': 'Arrivage annoncé sur TikTok Sénégal'})
    return sortie


def main():
    classeur = Classeur()
    if ONGLET not in classeur.onglets():
        classeur.creer_onglet(ONGLET)
        classeur.ecrire(f"'{ONGLET}'!A1", [EN_TETE])
    deja = {lien for ligne in classeur.lire(f"'{ONGLET}'!J2:J") for cellule in ligne for lien in cellule.split()}
    aujourdhui = date.today().isoformat()
    compteurs = {'Jumia SN': 0, 'AliExpress': 0, 'TikTok': 0, 'erreurs': 0}

    def ecrire(famille, source, offres):
        for o in offres:
            if o['url'] in deja:
                continue
            deja.add(o['url'])
            classeur.ajouter(f"'{ONGLET}'!A1", [aujourdhui, famille, o['article'], '', '', o['prix'], o['prix'], 1,
                                                 f"{o['prix']} ({o['vendeur']})", o['url'], source, o['signal'],
                                                 "À étudier : coût d'importation, concurrents à Dakar", ''])
            compteurs[source if source in compteurs else 'TikTok'] += 1

    with StealthySession(headless=True, solve_cloudflare=True) as session:
        http = cffi.Session(impersonate='chrome')
        for famille, (recherches_jumia, recherches_ali) in FAMILLES.items():
            for recherche in recherches_jumia:
                try:
                    ecrire(famille, 'Jumia SN', jumia(session, recherche))
                except Exception:
                    compteurs['erreurs'] += 1
            for recherche in recherches_ali:
                try:
                    ecrire(famille, 'AliExpress', aliexpress(session, recherche))
                except Exception:
                    compteurs['erreurs'] += 1
        for recherche in TIKTOK:
            try:
                ecrire('À classer (TikTok)', 'TikTok Sénégal', tiktok(session, recherche, http))
            except Exception:
                compteurs['erreurs'] += 1

    print('Nouveautés ajoutées :', compteurs)
    telegram(f"🔭 Veille nouveautés du {aujourdhui} : {compteurs['Jumia SN']} nouveautés Jumia SN, "
             f"{compteurs['AliExpress']} meilleures ventes AliExpress, {compteurs['TikTok']} arrivages TikTok Sénégal "
             f"(onglet « {ONGLET} »)." + (f" {compteurs['erreurs']} recherche(s) en échec." if compteurs['erreurs'] else ''))


if __name__ == '__main__':
    main()
