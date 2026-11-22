#!/usr/bin/env bash
set -eu
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_cmd="${PYTHON:-python3}"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
install_dir="$data_home/fieldroom"
bin_dir="$HOME/.local/bin"

if ! "$python_cmd" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "Fieldroom needs Python 3.10 or newer. Set PYTHON to the interpreter you want to use." >&2
    exit 1
fi
mkdir -p "$install_dir" "$bin_dir" "$data_home/applications"
if ! "$python_cmd" -m venv "$install_dir/venv"; then
    echo "Install your distribution's Python venv package and run this installer again." >&2
    exit 1
fi
"$install_dir/venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$source_dir[desktop]"
launcher="$install_dir/venv/bin/fieldroom-desktop"
if [[ ! -e "$bin_dir/fieldroom-desktop" && ! -L "$bin_dir/fieldroom" ]]; then
    ln -s "$launcher" "$bin_dir/fieldroom-desktop"
fi
cp "$source_dir/fieldroom/web/icon.svg" "$install_dir/icon.svg"
"$install_dir/venv/bin/python" - "$launcher" "$install_dir/icon.svg" "$data_home/applications/io.github.deltaboy1234.Fieldroom.desktop" <<'PY'
from pathlib import Path
import sys
exe, icon, target = sys.argv[1:]
for ch in ('\\', '"', '`', '$'):
    exe = exe.replace(ch, '\\' + ch)
exe = exe.replace('%', '%%').replace('\\', '\\\\')
Path(target).write_text(
    '[Desktop Entry]\nType=Application\nName=Fieldroom\nComment=Develop and crop photos in a folder\n'
    f'Exec="{exe}" %f\nIcon={icon}\nTerminal=false\nCategories=Graphics;Photography;\n',
    encoding='utf-8')
PY
printf '\nFieldroom is installed. Open Fieldroom from the applications menu, or run:\n  "%s"\n' "$launcher"
