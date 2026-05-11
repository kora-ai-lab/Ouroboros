# Ouroboros: The Sovereign AI Substrate of Kora Lab

L'Afrique a signé la Déclaration de Kigali en avril 2025, s'engageant à consacrer 60 milliards de dollars à sa souveraineté en intelligence artificielle. Pourtant, la plupart des outils utilisés sur le continent restent des dépendances directes d'infrastructures externes fermées. Ouroboros est la réponse technique de Kora Lab à cette tension. Ce n'est pas une simple interface de chat : c'est un substrat local conçu pour redonner le contrôle des données et de l'intelligence à l'utilisateur.

## La Vision de Kora Lab

Kora Lab est le laboratoire de recherche et de produit dédié à l'exécution technique de la souveraineté IA en Afrique. Nous opérons sur deux axes stratégiques majeurs. Le premier est la couche d'accessibilité, dont Ouroboros est la fondation, visant à rendre l'IA de pointe utilisable sans barrière technique et dans un respect total de la confidentialité. Le second est le laboratoire de modèles souverains, visant à construire des systèmes entraînés sur les connaissances et les langues africaines.

## Qu'est-ce que Ouroboros ?

Ouroboros est un noyau IA local, minimaliste et inspectable. Il sert de système d'exploitation pour agents personnels, capable d'évoluer de manière autonome en générant ses propres outils. Contrairement aux solutions cloud propriétaires, Ouroboros vit sur votre machine, utilise une mémoire SQLite locale et ne dépend d'aucune infrastructure payante pour son fonctionnement de base.

### Principes Fondamentaux

La souveraineté exige la transparence. Ouroboros est bâti sur trois piliers :
1. Inspectabilité : Le code est réduit à l'essentiel pour permettre une vérification complète par n'importe quel chercheur ou développeur.
2. Autonomie : Le système est capable de croître en installant de nouvelles capacités sans intervention manuelle complexe.
3. Sobriété : Conçu pour fonctionner dans des environnements à bande passante limitée et sans budget d'infrastructure massif, utilisant l'API Pollinations pour l'inférence.

## Architecture Technique

Le noyau d'Ouroboros repose sur une pile technologique moderne et légère :
*   Backend : Python 3.13 et FastAPI pour une exécution rapide et asynchrone.
*   Mémoire : SQLite pour une persistance locale robuste des sessions et des faits permanents.
*   Frontend : Interface statique HTML/JS, respectant une esthétique minimaliste et fonctionnelle.
*   Outils : Registre JSON dynamique permettant l'exécution de scripts Python isolés.

## Installation et Utilisation

Pour lancer le substrat local :

1. Assurez-vous d'avoir Python 3.13 installé.
2. Clonez le dépôt et installez les dépendances :
   ```bash
   pip install -r requirements.txt
   ```
3. Lancez le serveur :
   ```bash
   python nucleus/server.py
   ```
4. Accédez à l'interface via `http://127.0.0.1:8000`.

## Contribution et Souveraineté

Ce projet est une invitation à tous les chercheurs et développeurs qui croient que l'intelligence artificielle doit être un bien public géré par ceux qu'elle sert. Kora Lab ne cherche pas à concurrencer les institutions existantes, mais à compléter leur engagement par une exécution technique rigoureuse.

## Licence

Ce projet est distribué sous la licence Apache 2.0. Cette licence garantit la liberté d'utilisation tout en protégeant les contributeurs contre les enclosures propriétaires injustifiées.

---
*Kora Lab : Africa's Sovereign AI Research and Product Lab.*
