#!/usr/bin/env bash
# Install or update the AI Quota plasmoid for the current user.
set -euo pipefail
cd "$(dirname "$0")"
ID=com.github.the-arma-of-owl.aiquota

if ! command -v kpackagetool6 >/dev/null; then
  echo "kpackagetool6 not found — is KDE Plasma 6 installed?" >&2
  exit 1
fi
if kpackagetool6 -t Plasma/Applet -l 2>/dev/null | grep -qx "$ID"; then
  kpackagetool6 -t Plasma/Applet -u package
else
  kpackagetool6 -t Plasma/Applet -i package
fi
chmod 755 "$HOME/.local/share/plasma/plasmoids/$ID/contents/code/ai_quota.py"

python3 -c "import cryptography" 2>/dev/null || \
  echo "note: python 'cryptography' is missing — only needed for Replit."

echo
echo "Done. Right-click your panel → Add Widgets… → search 'AI Quota'."
echo "If an older version was already on the panel: systemctl --user restart plasma-plasmashell"
