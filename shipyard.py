#!/usr/bin/env python3
"""Local companion for Shipyard.

Serves the dashboard and a /worktrees.json endpoint so the page can offer links that
open branches in your configured app or IDE, and watches your PRs on GitHub: it turns
what changed into named events, runs your coding agent on the ones worth a look, and
shows the result as a card in the dashboard's sidebar. You decide; it prepares.

Run it from the dashboard directory and open the printed URL:

    python3 shipyard.py                 # scan the folders set in the config file
    python3 shipyard.py ~/dev ~/work    # or override the roots on the command line

Binds to localhost only. Discovery is read-only; branch actions and card buttons you
choose can change local Git state or act on GitHub. The code lives in the shipyard/
package; see shipyard/config.py for every setting in shipyard.config.json.
"""
from shipyard.app import main

if __name__ == "__main__":
    main()
