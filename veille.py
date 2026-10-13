"""Veille hebdomadaire Serper — DakarDiscountMarket (Mac007 13/10).

Une fois par semaine, ~25 crédits Serper (1 crédit par requête, mesuré le 13/10) :
  1. Avis Google Maps : note et nombre d'avis de nos 2 fiches et de 3 concurrents,
     plus les derniers avis laissés sur nos fiches (pour y répondre vite) ;
  2. Suggestions de recherche Google : ce que Google propose quand on tape nos mots de
     départ (vraies expressions des clients, pour Ads, titres de fiches et blog) ;
  3. Actualités de la semaine : coupures d'électricité, délestages, chaleur — les sujets
     qui font vendre ventilateurs rechargeables, climatiseurs, onduleurs.

Résultats ajoutés dans le classeur du suivi des prix (onglets « Veille … »), et un résumé
envoyé sur Telegram si TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID sont définis.
Les journaux ne contiennent que des compteurs (dépôt public).
"""
import os
import re
import urllib.parse
import urllib.request
from datetime import date

from ranking import Classeur, http_json

ETABLISSEMENTS = [  # (nom affiché, recherche Google Maps, est-ce nous ?)
    ('DakarDiscountMarket (Grand Yoff)', 'DakarDiscountMarket Grand Yoff Dakar', True),
    ('DakarDiscount (Médina)', 'DakarDiscount Médina Dakar', True),
    ('Kabirex', 'Kabirex Dakar', False),
    # Rond-point JVC, Liberté 6 (Mac007 13/10) = fiche « Electronic Corp Service Apres Vente ». Autres fiches :
    # Sacré Coeur 3 (~1 km, 4★/161 au 13/10), Point E, av. Lamine Gueye.
    ('Electronic Corp (Liberté 6, rond-point JVC)', 'Electronic Corp Liberté 6 Dakar', False),
    # 13/10 : aucune fiche Maps « Fadel Digital » ; « Fadel Digital Dakar » renvoyait FADEL'SHOP (rue Rafenel), autre boutique.
    ('Fadel Digital', 'Fadel Digital', False),
]
MOTS_DE_DEPART = [
    'ventilateur rechargeable', 'climatiseur', 'réfrigérateur', 'machine à laver', 'congélateur',
    'panier de basket', 'piscine', 'haltères', 'tapis de course', 'whey protéine', 'gants de boxe',
    'ballon de foot', 'maillot sénégal', 'valise',
]
ACTUALITES = ['coupure électricité Sénégal', 'délestage Senelec', 'canicule Dakar', 'Senelec perturbations']
# Suggestions qui visent un autre pays (Google mélange les pays francophones).
AUTRES_PAYS = ('haiti', 'haïti', 'tunisie', 'maroc', 'algérie', 'algerie', 'cameroun', 'ivoire', 'abidjan',
               'france', 'belgique', 'canada', 'suisse', 'madagascar', 'gabon', 'congo', 'mali', 'guinée',
               'burkina', 'bénin', 'togo', 'niger', 'mauritanie', 'action', 'leclerc', 'carrefour', 'lidl',
               'amazon', 'decathlon', 'gifi', 'castorama', 'darty', 'boulanger', 'cdiscount')
# Google Actualités renvoie aussi des coupures au Portugal, en Mauritanie… : on ne garde que le Sénégal.
SENEGAL = ('sénégal', 'senegal', 'senelec', 'dakar', '.sn/')
MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre',
        'novembre', 'décembre']


def perime(titre, aujourdhui):
    """Vrai si le titre annonce une date déjà passée (« … jusqu'au lundi 5 octobre » lu le 13/10) :
    Google date parfois de « cette semaine » un vieil article republié."""
    for jour, mois in re.findall(r'\b(\d{1,2})(?:er)? (' + '|'.join(MOIS) + r')\b', titre.lower()):
        try:
            if date(aujourdhui.year, MOIS.index(mois) + 1, int(jour)) < aujourdhui:
                return True
        except ValueError:
            pass
    return False


ONGLETS = {
    'avis': ('Veille avis', ['Date', 'Établissement', 'Note', "Nombre d'avis", 'Nous ?']),
    'avis_clients': ('Veille avis clients', ['Relevé le', 'Établissement', 'Note', "Date de l'avis", 'Avis']),
    'recherches': ('Veille recherches', ['Date', 'Mot de départ', 'Suggestions Google']),
    'actualites': ('Veille actualités', ['Date', 'Sujet', 'Titre', 'Source', 'Publié', 'Lien']),
}


def serper(service, corps):
    return http_json(f'https://google.serper.dev/{service}', {'gl': 'sn', 'hl': 'fr', **corps},
                     {'X-API-KEY': os.environ['SERPER_API_KEY']})


def telegram(texte):
    if not os.environ.get('TELEGRAM_BOT_TOKEN'):
        return
    urllib.request.urlopen(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                           urllib.parse.urlencode({'chat_id': os.environ['TELEGRAM_CHAT_ID'],
                                                   'text': texte[:4000]}).encode(), timeout=30)


def main():
    jour = date.today()
    aujourdhui = jour.isoformat()
    classeur = Classeur()
    existants = classeur.onglets()
    for titre, en_tetes in ONGLETS.values():
        if titre not in existants:
            classeur.creer_onglet(titre)
            classeur.ecrire(f"'{titre}'!A1", [en_tetes])
    credits = 0
    resume = [f"🔎 Veille de la semaine ({jour.strftime('%d/%m/%Y')})", '', '⭐ Avis Google Maps']

    for nom, recherche, nous in ETABLISSEMENTS:
        lieu = (serper('places', {'q': recherche}).get('places') or [{}])[0]
        credits += 1
        note, nombre = lieu.get('rating', ''), lieu.get('ratingCount', 0)
        classeur.ajouter(f"'{ONGLETS['avis'][0]}'!A:E", [aujourdhui, nom, note, nombre, 'oui' if nous else ''])
        resume.append(f"- {nom} : {note or 'pas de note'} ({nombre or 0} avis)" if lieu else f'- {nom} : fiche introuvable sur Google Maps')
        if nous and lieu.get('cid'):
            avis = serper('reviews', {'cid': lieu['cid'], 'sortBy': 'newest'}).get('reviews', [])[:5]
            credits += 1
            for a in avis:
                classeur.ajouter(f"'{ONGLETS['avis_clients'][0]}'!A:E",
                                 [aujourdhui, nom, a.get('rating', ''), a.get('date', ''), (a.get('snippet') or '')[:500]])
            recents = [a for a in avis if 'semaine' in (a.get('date') or '') or 'jour' in (a.get('date') or '')]
            if recents:
                resume.append(f'  → {len(recents)} avis récent(s) à lire et à répondre')

    resume += ['', '🔤 Suggestions de recherche (nouvelles idées de mots-clés dans le classeur)']
    for mot in MOTS_DE_DEPART:
        suggestions = [s.get('value', '') for s in serper('autocomplete', {'q': mot}).get('suggestions', [])]
        credits += 1
        gardees = [s for s in suggestions if s.lower() != mot and not any(p in s.lower() for p in AUTRES_PAYS)]
        classeur.ajouter(f"'{ONGLETS['recherches'][0]}'!A:C", [aujourdhui, mot, ' | '.join(gardees)])
    resume.append(f'- {len(MOTS_DE_DEPART)} mots de départ relevés')

    resume += ['', '📰 Actualités de la semaine']
    vus = set()
    for sujet in ACTUALITES:
        articles = serper('news', {'q': sujet, 'tbs': 'qdr:w'}).get('news', [])
        credits += 1
        for n in articles[:5]:
            texte = f"{n.get('title', '')} {n.get('snippet', '')} {n.get('source', '')} {n.get('link', '')}".lower()
            if n.get('link') in vus or not any(m in texte for m in SENEGAL) or perime(n.get('title', ''), jour):
                continue
            vus.add(n.get('link'))
            classeur.ajouter(f"'{ONGLETS['actualites'][0]}'!A:F",
                             [aujourdhui, sujet, n.get('title', ''), n.get('source', ''), n.get('date', ''), n.get('link', '')])
            if len(resume) < 40:
                resume.append(f"- {n.get('title', '')[:110]} ({n.get('source', '')}, {(n.get('date') or '').lower()})")

    resume += ['', f'{credits} crédits Serper utilisés.']
    telegram('\n'.join(resume))
    print(f'Veille du {aujourdhui} : {credits} crédits Serper, {len(vus)} article(s)')


if __name__ == '__main__':
    main()
